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
            rooms[room] = {
                "members": 0,
                "sids": {},  # Store sid: name mapping for easier lookup
                "messages": [],
                "mode": mode,
                "game_active": False,
                "player_x_sid": None,
                "player_o_sid": None
            }
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
        rooms[room]["messages"].append(content)
        if len(rooms[room]["messages"]) > 8:  # Keep a few more than client displays
            rooms[room]["messages"] = rooms[room]["messages"][-8:]
    else:
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
    # Store SID to name mapping
    rooms[room]["sids"][request.sid] = name

    # Notify others in the room that a user has joined
    send({"name": name, "message": "has joined the room"}, to=room, include_self=False)
    # Also send a welcome message to the joining user themselves
    send({"name": "System", "message": f"Welcome to room {room}, {name}!"}, to=request.sid)

    rooms[room]["members"] += 1
    # Update user count for everyone in the room
    emit("user_count", rooms[room]["members"], to=room)

    # If this is the second user joining, we can potentially enable game start
    if rooms[room]["members"] >= 2 and not rooms[room]["game_active"]:
        emit("enable_game_start", room=room)
    elif rooms[room]["game_active"]:
        # Notify joining user that a game is active and who is playing
        player_x_name = rooms[room]["sids"].get(rooms[room]["player_x_sid"], "Player X")
        player_o_name = rooms[room]["sids"].get(rooms[room]["player_o_sid"], "Player O")
        emit("game_status", {"message": f"An XOX game is active with {player_x_name} (X) and {player_o_name} (O)."},
             room=request.sid)

    print(f"{name} joined room {room} (SID: {request.sid})")


@socketio.on("disconnect")
def disconnect():
    """Handles client disconnections from a room."""
    room = session.get("room")
    name = session.get("name")
    sid = request.sid

    if not room or room not in rooms:
        return

    leave_room(room)
    if sid in rooms[room]["sids"]:
        del rooms[room]["sids"][sid]  # Remove sid from mapping

    rooms[room]["members"] -= 1

    # Remove user from active video call if they were part of one
    if room in video_calls_in_room:
        if sid in video_calls_in_room[room]:
            video_calls_in_room[room].remove(sid)
            # If a call was 1:1 and one leaves, end for the other
            if len(video_calls_in_room[room]) == 1:
                remaining_sid = video_calls_in_room[room][0]
                emit("call_end", {"name": name}, room=remaining_sid)
            video_calls_in_room[room] = []  # Reset call state for the room after a participant leaves

    # Reset game state if a player leaves
    if rooms[room]["player_x_sid"] == sid or rooms[room]["player_o_sid"] == sid:
        rooms[room]["game_active"] = False
        rooms[room]["player_x_sid"] = None
        rooms[room]["player_o_sid"] = None
        emit("game_reset", {"reason": f"{name} left the game. Game reset."}, room=room)

    # Notify others in the room that a user has left
    send({"name": name, "message": "has left the room"}, to=room)

    # Update user count for everyone in the room
    if rooms[room]["members"] <= 0:
        del rooms[room]
        if room in video_calls_in_room:
            del video_calls_in_room[room]
    else:
        emit("user_count", rooms[room]["members"], to=room)
        # If user count drops below 2, disable game start unless a game is active with 2 players
        if rooms[room]["members"] < 2 and not rooms[room]["game_active"]:
            emit("disable_game_start", room=room)

    print(f"{name} has left the room {room} (SID: {sid})")


# --- WebRTC Signaling Handlers ---

@socketio.on("call_request")
def handle_call_request():
    """
    Handles a request to start a video call.
    Emits 'call_request' to an *available* peer in the room.
    """
    room = session.get("room")
    name = session.get("name")
    requester_sid = request.sid
    if not room or not name:
        return

    # Get all SIDs currently in the room
    connected_sids = [sid for sid in rooms[room]["sids"].keys() if sid != requester_sid]

    # Find a peer who is not already in a call or being called
    target_peer_sid = None
    for sid in connected_sids:
        # Check if this SID is already part of an active call in this room
        if not any(sid in call_sids for call_sids in video_calls_in_room.values() if
                   room in call_sids):  # More robust check
            target_peer_sid = sid
            break

    if target_peer_sid:
        # Check if the requester is already in a call (shouldn't happen, but safety)
        if requester_sid in video_calls_in_room.get(room, []):
            emit("call_rejected", {"from": "System", "reason": "You are already in a call."}, room=requester_sid)
            return

        # Initialize or update the call participants for this room
        video_calls_in_room[room] = [requester_sid, target_peer_sid]

        # Emit the call request to the target peer
        emit("call_request", {"from": name, "requester_sid": requester_sid}, room=target_peer_sid)
        # Inform the requester that the call request has been sent
        emit("call_status", {"message": f"Calling {rooms[room]['sids'].get(target_peer_sid, 'another user')}..."},
             room=requester_sid)
        print(
            f"{name} (SID: {requester_sid}) requested a call to {rooms[room]['sids'].get(target_peer_sid, 'unknown')} (SID: {target_peer_sid}) in room {room}")
    else:
        emit("call_rejected", {"from": "System", "reason": "No available peer for a video call."}, room=requester_sid)
        print(f"{name} tried to start a call in room {room} but no available peer.")


@socketio.on("call_response")
def handle_call_response(data):
    """
    Handles a response (accept/reject) to a video call request.
    `data` contains 'action' (accept/reject) and 'requester_sid'.
    """
    room = session.get("room")
    respondent_name = session.get("name")
    respondent_sid = request.sid
    requester_sid = data.get("requester_sid")
    action = data.get("action")

    if not room or not respondent_name or not requester_sid or action not in ["accept", "reject"]:
        return

    requester_name = rooms[room]["sids"].get(requester_sid, "Caller")

    if action == "accept":
        # Confirm call participants for server-side tracking
        if requester_sid not in video_calls_in_room.get(room, []) or respondent_sid not in video_calls_in_room.get(room,
                                                                                                                   []):
            # This means the call state might have been reset or user left
            emit("call_rejected", {"from": "System", "reason": "Call request expired or participant left."},
                 room=respondent_sid)
            emit("call_rejected", {"from": "System", "reason": f"{respondent_name} could not join. Try again."},
                 room=requester_sid)
            if room in video_calls_in_room: del video_calls_in_room[room]  # Clean up
            return

        emit("call_accepted", {"from": respondent_name, "accepted_sid": respondent_sid}, room=requester_sid)
        emit("call_status", {"message": f"You accepted the call from {requester_name}."}, room=respondent_sid)
        print(f"{respondent_name} accepted call from {requester_name} in room {room}")

    elif action == "reject":
        emit("call_rejected", {"from": respondent_name, "reason": "rejected your call."}, room=requester_sid)
        emit("call_status", {"message": f"You rejected the call from {requester_name}."}, room=respondent_sid)
        # Clean up call state if rejected
        if room in video_calls_in_room and sorted(video_calls_in_room[room]) == sorted([requester_sid, respondent_sid]):
            video_calls_in_room[room] = []
        print(f"{respondent_name} rejected call from {requester_name} in room {room}")


@socketio.on("offer")
def handle_offer(data):
    """Relays WebRTC SDP offer from one peer to the other."""
    room = session.get("room")
    sender_sid = request.sid
    if not room:
        return

    # Find the other participant in the call for this room
    if room in video_calls_in_room and sender_sid in video_calls_in_room[room]:
        other_peer_sid = [sid for sid in video_calls_in_room[room] if sid != sender_sid]
        if other_peer_sid:
            emit("offer", {"offer": data["offer"]}, room=other_peer_sid[0])
            print(
                f"Offer from {session.get('name')} relayed in room {room} to {rooms[room]['sids'].get(other_peer_sid[0], 'unknown')}")
        else:
            print(f"Offer from {session.get('name')} in room {room} but no other peer found in call.")
    else:
        print(f"Offer from {session.get('name')} in room {room} but not in an active call state.")


@socketio.on("answer")
def handle_answer(data):
    """Relays WebRTC SDP answer from one peer to the other."""
    room = session.get("room")
    sender_sid = request.sid
    if not room:
        return

    if room in video_calls_in_room and sender_sid in video_calls_in_room[room]:
        other_peer_sid = [sid for sid in video_calls_in_room[room] if sid != sender_sid]
        if other_peer_sid:
            emit("answer", {"answer": data["answer"]}, room=other_peer_sid[0])
            print(
                f"Answer from {session.get('name')} relayed in room {room} to {rooms[room]['sids'].get(other_peer_sid[0], 'unknown')}")
        else:
            print(f"Answer from {session.get('name')} in room {room} but no other peer found in call.")
    else:
        print(f"Answer from {session.get('name')} in room {room} but not in an active call state.")


@socketio.on("ice_candidate")
def handle_ice_candidate(data):
    """Relays WebRTC ICE candidates between peers."""
    room = session.get("room")
    sender_sid = request.sid
    if not room:
        return

    if room in video_calls_in_room and sender_sid in video_calls_in_room[room]:
        other_peer_sid = [sid for sid in video_calls_in_room[room] if sid != sender_sid]
        if other_peer_sid:
            emit("ice_candidate", {"candidate": data["candidate"]}, room=other_peer_sid[0])
            print(
                f"ICE candidate from {session.get('name')} relayed in room {room} to {rooms[room]['sids'].get(other_peer_sid[0], 'unknown')}")
        else:
            print(f"ICE candidate from {session.get('name')} in room {room} but no other peer found in call.")
    else:
        print(f"ICE candidate from {session.get('name')} in room {room} but not in an active call state.")


@socketio.on("call_end")
def handle_call_end():
    """
    Handles when a user ends a video call.
    Notifies the other peer and resets call state.
    """
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    if not room or not name:
        return

    # Clear the active call participants for this room if 'sid' was part of it
    if room in video_calls_in_room and sid in video_calls_in_room[room]:
        other_peer_sids = [s for s in video_calls_in_room[room] if s != sid]
        video_calls_in_room[room] = []  # Reset for this room

        if other_peer_sids:
            # Notify the other peer in the room that the call has ended
            emit("call_end", {"name": name}, room=other_peer_sids[0])
            print(
                f"{name} ended the call. Notified {rooms[room]['sids'].get(other_peer_sids[0], 'unknown')} in room {room}")
        else:
            print(f"{name} ended the call in room {room} (no other peer to notify).")
    else:
        print(f"{name} tried to end a call in room {room}, but was not in an active call state.")


# --- XOX Game Handlers ---
@socketio.on("game_start_request")
def handle_game_start_request():
    room = session.get("room")
    name = session.get("name")
    requester_sid = request.sid
    if not room or not name or room not in rooms:
        return

    if rooms[room]["members"] < 2:
        emit("game_status", {"message": "Need 2 players to start XOX."}, room=requester_sid)
        return
    if rooms[room]["game_active"]:
        emit("game_status", {"message": "A game is already active. Please wait or ask players to reset."},
             room=requester_sid)
        return

    # Find two available players for the game.
    # Prioritize the requester as one of the players.
    available_sids = [sid for sid in rooms[room]["sids"].keys() if sid != requester_sid]
    if not available_sids:
        emit("game_status", {"message": "No other player available to start XOX."}, room=requester_sid)
        return

    # Select the other player randomly from available ones
    other_player_sid = random.choice(available_sids)

    # Assign players X and O
    player_x_sid, player_o_sid = random.sample([requester_sid, other_player_sid], 2)

    rooms[room]["player_x_sid"] = player_x_sid
    rooms[room]["player_o_sid"] = player_o_sid
    rooms[room]["game_active"] = True

    player_x_name = rooms[room]["sids"].get(player_x_sid, "Player X")
    player_o_name = rooms[room]["sids"].get(player_o_sid, "Player O")

    # Inform player X
    emit("game_start", {
        "player_x_name": player_x_name,
        "player_o_name": player_o_name,
        "your_symbol": "X",
        "is_your_turn": True  # X starts
    }, room=player_x_sid)

    # Inform player O
    emit("game_start", {
        "player_x_name": player_x_name,
        "player_o_name": player_o_name,
        "your_symbol": "O",
        "is_your_turn": False
    }, room=player_o_sid)

    # Inform all other users (spectators) in the room
    spectator_sids = [sid for sid in rooms[room]["sids"].keys() if sid not in [player_x_sid, player_o_sid]]
    for sid in spectator_sids:
        emit("game_status", {"message": f"XOX game started! {player_x_name} (X) vs {player_o_name} (O)."}, room=sid)

    # Send a general chat message about the game starting
    send({"name": "System", "message": f"XOX game started! {player_x_name} (X) vs {player_o_name} (O)."}, to=room)


@socketio.on("game_move")
def handle_game_move(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    if not room or not name or room not in rooms:
        return

    if not rooms[room]["game_active"]:
        emit("game_status", {"message": "Game not active."}, room=sid)
        return

    # Verify it's the current player's turn
    expected_symbol = "X" if sid == rooms[room]["player_x_sid"] else "O"
    if data["symbol"] != expected_symbol:
        emit("game_status", {"message": "It's not your turn or you are not an active player."}, room=sid)
        return

    # Basic server-side validation: check if index is valid and cell is empty
    index = data["index"]
    board_state = data["board_state"]  # Trust client for simplicity but could validate here

    if not (0 <= index < 9) or board_state[index] != data["symbol"]:  # Check if client's move is valid on their state
        emit("game_status", {"message": "Invalid move received."}, room=sid)
        return

    # Relays the move to all players in the room
    # We now also send who's turn it is next so clients can update correctly
    next_turn_sid = rooms[room]["player_x_sid"] if data["next_turn_symbol"] == "X" else rooms[room]["player_o_sid"]

    emit("game_update", {
        "index": data["index"],
        "symbol": data["symbol"],
        "next_turn_symbol": data["next_turn_symbol"],
        "player_name": name,
        "board_state": data["board_state"],
        "current_turn_sid": next_turn_sid  # Pass the SID of the player whose turn it is next
    }, room=room)


@socketio.on("game_over")
def handle_game_over(data):
    room = session.get("room")
    if not room or room not in rooms:
        return

    # Ensure the game state is reset on the server
    rooms[room]["game_active"] = False
    rooms[room]["player_x_sid"] = None
    rooms[room]["player_o_sid"] = None

    emit("game_result", {
        "winner": data.get("winner"),
        "draw": data.get("draw"),
        "message": data["message"]
    }, room=room)
    # Re-enable game start for relevant users
    if rooms[room]["members"] >= 2:
        emit("enable_game_start", room=room)


@socketio.on("game_reset_request")
def handle_game_reset_request():
    room = session.get("room")
    name = session.get("name")
    if not room or room not in rooms:
        return

    # Reset game state
    rooms[room]["game_active"] = False
    rooms[room]["player_x_sid"] = None
    rooms[room]["player_o_sid"] = None

    emit("game_reset", {"reason": f"{name} requested a new game."}, room=room)
    # After reset, enable game start if there are enough members
    if rooms[room]["members"] >= 2:
        emit("enable_game_start", room=room)


if __name__ == "__main__":
    socketio.run(app, debug=True, host='0.0.0.0', allow_unsafe_werkzeug=True)