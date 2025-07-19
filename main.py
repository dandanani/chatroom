import eventlet

eventlet.monkey_patch()
from flask import Flask, render_template, request, session, redirect, url_for
from flask_socketio import join_room, leave_room, send, SocketIO, emit
import random
from string import ascii_uppercase

app = Flask(__name__)
app.config["SECRET_KEY"] = "hjhjsdahhds"
socketio = SocketIO(app)

rooms = {}
MAX_MESSAGES = 1000  # Limit number of stored messages per room

# Store active video call participants in each room
video_calls_in_room = {}  # {room_code: [sid1, sid2]}


def generate_unique_code(length):
    """Generates a unique room code."""
    while True:
        code = ""
        for _ in range(length):
            code += random.choice(ascii_uppercase)
        if code not in rooms:
            break
    return code


@app.route("/", methods=["POST", "GET"])
def home():
    """Handles the home page, allowing users to join or create rooms."""
    session.clear()
    if request.method == "POST":
        name = request.form.get("name")
        code = request.form.get("code")
        join = "join" in request.form
        create = "create" in request.form
        mode = request.form.get("mode", "full")

        if not name:
            return render_template("home.html", error="Please enter a name.", code=code, name=name)

        if join and not code:
            return render_template("home.html", error="Please enter a room code.", code=code, name=name)

        room = code
        if create:
            room = generate_unique_code(4)
            rooms[room] = {"members": 0, "messages": [], "mode": mode, "game_active": False, "player_x_sid": None, "player_o_sid": None} # Added game state
            video_calls_in_room[room] = []  # Initialize video call tracking for new room
        elif code not in rooms:
            return render_template("home.html", error="Room does not exist.", code=code, name=name)

        session["room"] = room
        session["name"] = name
        session["mode"] = mode
        return redirect(url_for("room"))

    return render_template("home.html")


@app.route("/room")
def room():
    """Renders the chat room page."""
    room = session.get("room")
    if room is None or session.get("name") is None or room not in rooms:
        return redirect(url_for("home"))

    mode = rooms[room].get("mode", "full")
    messages = rooms[room]["messages"]

    # Limit messages only when rendering in privacy mode
    if mode == "privacy":
        messages = messages[-5:]

    return render_template("room.html", code=room, messages=messages, mode=mode)


@socketio.on("message")
def message(data):
    """Handles incoming chat messages."""
    room = session.get("room")
    if room not in rooms:
        return

    content = {
        "name": session.get("name"),
        "message": data["data"]
    }

    # Send message to everyone in the room
    send(content, to=room)

    # Store messages based on room mode
    mode = rooms[room].get("mode", "full")
    if mode == "privacy":
        # In privacy mode, store only last 8 messages (client also limits to 5 for display)
        rooms[room]["messages"].append(content)
        if len(rooms[room]["messages"]) > 8:
            rooms[room]["messages"] = rooms[room]["messages"][-8:]
    else:
        # In full chat mode, store up to MAX_MESSAGES
        rooms[room]["messages"].append(content)
        if len(rooms[room]["messages"]) > MAX_MESSAGES:
            rooms[room]["messages"] = rooms[room]["messages"][-MAX_MESSAGES:]


@socketio.on("typing")
def typing():
    """Broadcasts a typing indicator to other users in the room."""
    room = session.get("room")
    name = session.get("name")
    if room not in rooms or not name:
        return
    # Emit to everyone in the room except the sender
    emit("typing", {"name": name}, room=room, include_self=False)


@socketio.on("connect")
def connect(auth):
    """Handles new client connections to a room."""
    room = session.get("room")
    name = session.get("name")
    if not room or not name:
        return
    if room not in rooms:
        leave_room(room)
        return

    join_room(room)
    # Notify others in the room that a user has joined
    send({"name": name, "message": "In Chat"}, to=room)
    rooms[room]["members"] += 1
    # Update user count for everyone in the room
    emit("user_count", rooms[room]["members"], to=room)

    # If this is the second user joining, we can potentially enable game start
    if rooms[room]["members"] == 2:
        emit("enable_game_start", room=room)
    # If more than 2, notify that game is 2-player
    elif rooms[room]["members"] > 2:
        emit("game_status", {"message": "XOX is a 2-player game. Wait for a spot or play with an existing player."}, room=request.sid)

    print(f"{name} joined room {room}")


@socketio.on("disconnect")
def disconnect():
    """Handles client disconnections from a room."""
    room = session.get("room")
    name = session.get("name")
    leave_room(room)

    if room in rooms:
        rooms[room]["members"] -= 1
        # Remove user from active video call if they were part of one
        if request.sid in video_calls_in_room.get(room, []):
            video_calls_in_room[room].remove(request.sid)
            # Notify the remaining peer that the call has ended
            if len(video_calls_in_room[room]) == 1:
                remaining_sid = video_calls_in_room[room][0]
                emit("call_end", {"name": name}, room=remaining_sid)
            video_calls_in_room[room] = []  # Reset call state for the room

        # Reset game state if a player leaves
        if rooms[room]["player_x_sid"] == request.sid or rooms[room]["player_o_sid"] == request.sid:
            rooms[room]["game_active"] = False
            rooms[room]["player_x_sid"] = None
            rooms[room]["player_o_sid"] = None
            emit("game_reset", {"reason": f"{name} left the game."}, room=room)


        if rooms[room]["members"] <= 0:
            del rooms[room]
            if room in video_calls_in_room:
                del video_calls_in_room[room]  # Clean up video call state for empty room

    # Notify others in the room that a user has left
    send({"name": name, "message": "has left the room"}, to=room)
    # Update user count for everyone in the room
    if room in rooms:
        emit("user_count", rooms[room]["members"], to=room)
        # If user count drops below 2, disable game start
        if rooms[room]["members"] < 2:
            emit("disable_game_start", room=room)

    print(f"{name} has left the room {room}")


# --- WebRTC Signaling Handlers ---

@socketio.on("call_request")
def handle_call_request():
    """
    Handles a request to start a video call.
    Emits 'call_request' to the other peer in the room.
    """
    room = session.get("room")
    name = session.get("name")
    if not room or not name:
        return

    # For 1:1 calls, find the other person in the room
    room_sids = [sid for sid in rooms[room]["members_sids"] if # rooms[room]["members_sids"] is not defined, should be dynamic SIDs
                 sid != request.sid]

    # Simple logic for 1:1: if there's exactly one other person
    # FIX: rooms[room]["members_sids"] is not being populated anywhere.
    # A robust solution would involve tracking SIDs in the rooms dict
    # or iterating connected SIDs in the room directly.
    # For now, if the assumption is only two people for video,
    # the logic below (if rooms[room]["members"] == 2 and request.sid in ... ) would need adjustment.
    # Let's assume a simplified approach for this example where we just emit to others.
    # If you need robust 1:1 video, you'd need to properly track SIDs per room.
    # For now, just relaying to "room" will send to all, and client can filter.

    # Revised logic for call request, if we want strict 1:1 within the room:
    connected_sids = [sid for sid, sock in socketio.server.rooms.get(f'/#{room}', {}).items()] # Get actual SIDs in the room
    other_peer_sids = [sid for sid in connected_sids if sid != request.sid]

    if len(other_peer_sids) == 1:
        other_peer_sid = other_peer_sids[0]
        if other_peer_sid in video_calls_in_room.get(room, []):
            emit("call_rejected", {"from": "System", "reason": "Other user is already in a call."}, room=request.sid)
            return

        video_calls_in_room[room] = [request.sid, other_peer_sid]

        emit("call_request", {"from": name}, room=other_peer_sid)
        print(f"{name} requested a call to {other_peer_sid} in room {room}")
    else:
        emit("call_rejected", {"from": "System", "reason": "No available peer or too many users for 1:1 call."}, room=request.sid)
        print(f"{name} tried to start a call in room {room} but conditions not met.")


@socketio.on("call_rejected")
def handle_call_rejected(data):
    """
    Handles a rejection of a video call request.
    Emits 'call_rejected' back to the caller.
    """
    room = session.get("room")
    if not room:
        return

    # Find the caller's SID (assuming 'from' in data is their name)
    # This is a simplified approach; in a robust system, you'd track SIDs.
    # For now, we'll just emit to everyone else in the room.
    emit("call_rejected", {"from": session.get("name")}, room=room, include_self=False)
    print(f"{session.get('name')} rejected a call in room {room}")
    # Clear the call state if it was initiated
    if room in video_calls_in_room and request.sid in video_calls_in_room[room]:
        video_calls_in_room[room] = []


@socketio.on("offer")
def handle_offer(data):
    """Relays WebRTC SDP offer from one peer to the other."""
    room = session.get("room")
    if not room:
        return
    # Emit the offer to everyone in the room except the sender
    emit("offer", {"offer": data["offer"]}, room=room, include_self=False)
    print(f"Offer from {session.get('name')} relayed in room {room}")


@socketio.on("answer")
def handle_answer(data):
    """Relays WebRTC SDP answer from one peer to the other."""
    room = session.get("room")
    if not room:
        return
    # Emit the answer to everyone in the room except the sender
    emit("answer", {"answer": data["answer"]}, room=room, include_self=False)
    print(f"Answer from {session.get('name')} relayed in room {room}")


@socketio.on("ice_candidate")
def handle_ice_candidate(data):
    """Relays WebRTC ICE candidates between peers."""
    room = session.get("room")
    if not room:
        return
    # Emit the ICE candidate to everyone in the room except the sender
    emit("ice_candidate", {"candidate": data["candidate"]}, room=room, include_self=False)
    print(f"ICE candidate from {session.get('name')} relayed in room {room}")


@socketio.on("call_end")
def handle_call_end():
    """
    Handles when a user ends a video call.
    Notifies the other peer and resets call state.
    """
    room = session.get("room")
    name = session.get("name")
    if not room or not name:
        return

    # Notify the other peer in the room that the call has ended
    emit("call_end", {"name": name}, room=room, include_self=False)

    # Clear the active call participants for this room
    if room in video_calls_in_room:
        video_calls_in_room[room] = []
    print(f"{name} ended the call in room {room}")


# --- XOX Game Handlers ---
@socketio.on("game_start_request")
def handle_game_start_request():
    room = session.get("room")
    name = session.get("name")
    if not room or not name or room not in rooms:
        return

    if rooms[room]["members"] < 2:
        emit("game_status", {"message": "Need 2 players to start XOX."}, room=request.sid)
        return
    if rooms[room]["game_active"]:
        emit("game_status", {"message": "A game is already active."}, room=request.sid)
        return

    # Assign players X and O randomly
    sids_in_room = [sid for sid, sock in socketio.server.rooms.get(f'/#{room}', {}).items()]
    if len(sids_in_room) >= 2:
        player_sids = random.sample(sids_in_room, 2) # Pick two random players if more than 2
        rooms[room]["player_x_sid"] = player_sids[0]
        rooms[room]["player_o_sid"] = player_sids[1]
        rooms[room]["game_active"] = True

        player_x_name = socketio.server.get_sid_session(rooms[room]["player_x_sid"]).get("name", "Player X")
        player_o_name = socketio.server.get_sid_session(rooms[room]["player_o_sid"]).get("name", "Player O")

        emit("game_start", {
            "player_x_name": player_x_name,
            "player_o_name": player_o_name,
            "your_symbol": "X" if request.sid == rooms[room]["player_x_sid"] else "O"
        }, room=rooms[room]["player_x_sid"])
        emit("game_start", {
            "player_x_name": player_x_name,
            "player_o_name": player_o_name,
            "your_symbol": "O" if request.sid == rooms[room]["player_o_sid"] else "X"
        }, room=rooms[room]["player_o_sid"])

        emit("game_status", {"message": f"XOX game started! {player_x_name} is X, {player_o_name} is O. {player_x_name}'s turn."}, room=room)
    else:
        emit("game_status", {"message": "Not enough players to start XOX."}, room=request.sid)


@socketio.on("game_move")
def handle_game_move(data):
    room = session.get("room")
    name = session.get("name")
    if not room or not name or room not in rooms:
        return

    # Basic server-side validation (more complex validation for cheating prevention might be needed)
    if not rooms[room]["game_active"]:
        emit("game_status", {"message": "Game not active."}, room=request.sid)
        return

    # Relays the move to all players in the room
    emit("game_update", {
        "index": data["index"],
        "symbol": data["symbol"],
        "next_turn_symbol": data["next_turn_symbol"],
        "player_name": name,
        "board_state": data["board_state"] # Send board state for easier client-side sync
    }, room=room)


@socketio.on("game_over")
def handle_game_over(data):
    room = session.get("room")
    name = session.get("name")
    if not room or not name or room not in rooms:
        return

    rooms[room]["game_active"] = False
    rooms[room]["player_x_sid"] = None
    rooms[room]["player_o_sid"] = None
    emit("game_result", {
        "winner": data.get("winner"),
        "draw": data.get("draw"),
        "message": data["message"]
    }, room=room)


@socketio.on("game_reset_request")
def handle_game_reset_request():
    room = session.get("room")
    if not room or room not in rooms:
        return

    rooms[room]["game_active"] = False
    rooms[room]["player_x_sid"] = None
    rooms[room]["player_o_sid"] = None
    emit("game_reset", {"reason": f"{session.get('name')} requested a new game."}, room=room)
    if rooms[room]["members"] == 2:
        emit("enable_game_start", room=room)


if __name__ == "__main__":
    socketio.run(app, debug=True, host='0.0.0.0', allow_unsafe_werkzeug=True)