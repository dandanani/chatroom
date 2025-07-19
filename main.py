# main.py

# 1. IMMEDIATE MONKEY PATCHING
import eventlet

eventlet.monkey_patch()  # This MUST be the very first thing after importing eventlet

# 2. THEN, all other imports
import os
import random
import time
import logging
from collections import defaultdict
from string import ascii_uppercase, digits

from flask import Flask, render_template, request, session, redirect, url_for
from flask_socketio import join_room, leave_room, send, SocketIO, emit
from markupsafe import escape
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# --- Flask App Configuration ---
app = Flask(__name__)

# Load SECRET_KEY from environment variable for production.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY",
                                          "a_very_long_and_random_string_for_dev_only_replace_this_in_prod")

# Security headers for session cookies
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = 'Lax'

# --- Socket.IO Configuration ---
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=False)

# --- Rate Limiting Configuration ---
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

last_message_time = defaultdict(lambda: 0)
MESSAGE_COOLDOWN = 0.5

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Global Data Stores ---
rooms = {}
MAX_MESSAGES = 1000
video_calls_in_room = {}
CHARACTER_SET = ascii_uppercase + digits


# --- Helper Functions ---
def generate_unique_code(length):
    while True:
        code = "".join(random.choice(CHARACTER_SET) for _ in range(length))
        if code not in rooms:
            break
    return code


# --- Flask Routes ---
@app.route("/", methods=["POST", "GET"])
@limiter.limit("10 per minute", methods=["POST"])
def home():
    session.clear()

    if request.method == "POST":
        name = request.form.get("name")
        code = request.form.get("code")
        join = "join" in request.form
        create = "create" in request.form
        mode = request.form.get("mode", "full")

        sanitized_name = escape(name) if name else ""

        if not sanitized_name:
            logger.warning("Attempted join/create with no name.")
            return render_template("home.html", error="Please enter a name.", code=code, name=name)

        if join and not code:
            logger.warning(f"User {sanitized_name} attempted to join without a room code.")
            return render_template("home.html", error="Please enter a room code.", code=code, name=name)

        room = code
        if create:
            room = generate_unique_code(6)
            rooms[room] = {
                "members": 0,
                "sids": {},
                "messages": [],
                "mode": mode,
                "game_active": False,
                "player_x_sid": None,
                "player_o_sid": None
            }
            video_calls_in_room[room] = []
            logger.info(f"Room '{room}' created by {sanitized_name}.")
        elif code not in rooms:
            logger.warning(f"User {sanitized_name} attempted to join non-existent room '{code}'.")
            return render_template("home.html", error="Room does not exist.", code=code, name=name)

        session["room"] = room
        session["name"] = sanitized_name
        session["mode"] = mode
        logger.info(f"User {sanitized_name} redirecting to room {room}.")
        return redirect(url_for("room"))

    return render_template("home.html")


@app.route("/room")
def room():
    room = session.get("room")
    name = session.get("name")
    if room is None or name is None or room not in rooms:
        logger.warning(f"Unauthorized access attempt to /room. Session: {session.get('room')}, {session.get('name')}")
        return redirect(url_for("home"))

    mode = rooms[room].get("mode", "full")
    messages = rooms[room]["messages"]

    if mode == "privacy":
        messages = messages[-5:]

    logger.info(f"Rendering room {room} for user {name}.")
    return render_template("room.html", code=room, messages=messages, mode=mode)


# --- Socket.IO Event Handlers ---
@socketio.on("message")
def message(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid

    if not room or not name or room not in rooms:
        logger.warning(f"Message attempt from unauthenticated/invalid session SID: {sid}")
        return

    current_time = time.time()
    if current_time - last_message_time[sid] < MESSAGE_COOLDOWN:
        emit("error", {"message": "Please slow down! You are sending messages too fast."}, room=sid)
        logger.warning(f"Rate limit hit for message from {name} (SID: {sid}) in room {room}.")
        return
    last_message_time[sid] = current_time

    sanitized_message = escape(data.get("data", ""))

    content = {
        "name": name,
        "message": sanitized_message
    }

    mode = rooms[room].get("mode", "full")
    if mode == "privacy":
        rooms[room]["messages"].append(content)
        if len(rooms[room]["messages"]) > 8:
            rooms[room]["messages"] = rooms[room]["messages"][-8:]
    else:
        rooms[room]["messages"].append(content)
        if len(rooms[room]["messages"]) > MAX_MESSAGES:
            rooms[room]["messages"] = rooms[room]["messages"][-MAX_MESSAGES:]

    send(content, to=room)
    logger.info(f"Message from {name} in room {room}: {sanitized_message}")


@socketio.on("typing")
def typing():
    room = session.get("room")
    name = session.get("name")
    sid = request.sid

    if not room or not name or room not in rooms:
        return

    emit("typing", {"name": name}, room=room, include_self=False)


@socketio.on("connect")
def connect(auth):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid

    if not room or not name or room not in rooms:
        logger.warning(f"Connection attempt from invalid session. SID: {sid}")
        return False

    join_room(room)
    rooms[room]["sids"][sid] = name

    send({"name": "System", "message": f"{name} has joined the room."}, to=room, include_self=False)
    send({"name": "System", "message": f"Welcome to room {room}, {name}!"}, to=sid)

    rooms[room]["members"] += 1
    emit("user_count", rooms[room]["members"], to=room)

    if rooms[room]["members"] >= 2 and not rooms[room]["game_active"]:
        emit("enable_game_start", room=room)
    elif rooms[room]["game_active"]:
        player_x_name = rooms[room]["sids"].get(rooms[room]["player_x_sid"], "Player X")
        player_o_name = rooms[room]["sids"].get(rooms[room]["player_o_sid"], "Player O")
        emit("game_status", {"message": f"An XOX game is active with {player_x_name} (X) and {player_o_name} (O)."},
             room=sid)

    logger.info(f"User {name} (SID: {sid}) connected to room {room}. Total members: {rooms[room]['members']}.")


@socketio.on("disconnect")
def disconnect():
    room = session.get("room")
    name = session.get("name")
    sid = request.sid

    if not room or room not in rooms:
        logger.info(f"Disconnected from invalid/non-existent room. SID: {sid}")
        return

    leave_room(room)
    if sid in rooms[room]["sids"]:
        del rooms[room]["sids"][sid]

    rooms[room]["members"] -= 1

    if room in video_calls_in_room:
        if sid in video_calls_in_room[room]:
            other_peer_sids_in_call = [s for s in video_calls_in_room[room] if s != sid]
            video_calls_in_room[room] = []

            if other_peer_sids_in_call:
                emit("call_end", {"name": name}, room=other_peer_sids_in_call[0])
                logger.info(
                    f"User {name} (SID: {sid}) disconnected, ending call for {rooms[room]['sids'].get(other_peer_sids_in_call[0], 'unknown')}.")
            else:
                logger.info(f"User {name} (SID: {sid}) disconnected, call ended (no other peer).")

    if rooms[room]["player_x_sid"] == sid or rooms[room]["player_o_sid"] == sid:
        rooms[room]["game_active"] = False
        rooms[room]["player_x_sid"] = None
        rooms[room]["player_o_sid"] = None
        emit("game_reset", {"reason": f"{name} left the game. Game reset."}, room=room)
        logger.info(f"User {name} (SID: {sid}) disconnected, XOX game in room {room} reset.")

    send({"name": "System", "message": f"{name} has left the room."}, to=room)

    if rooms[room]["members"] <= 0:
        del rooms[room]
        if room in video_calls_in_room:
            del video_calls_in_room[room]
        logger.info(f"Room {room} is now empty and has been deleted.")
    else:
        emit("user_count", rooms[room]["members"], to=room)
        if rooms[room]["members"] < 2 and not rooms[room]["game_active"]:
            emit("disable_game_start", room=room)
        logger.info(
            f"User {name} (SID: {sid}) disconnected from room {room}. Remaining members: {rooms[room]['members']}.")


# --- WebRTC Signaling Handlers ---
@socketio.on("call_request")
def handle_call_request():
    room = session.get("room")
    name = session.get("name")
    requester_sid = request.sid
    if not room or not name or room not in rooms:
        logger.warning(f"Call request from invalid session. SID: {requester_sid}")
        return

    connected_sids = [sid for sid in rooms[room]["sids"].keys() if sid != requester_sid]

    target_peer_sid = None
    for sid in connected_sids:
        is_in_any_call = False
        for call_sids_list in video_calls_in_room.values():
            if sid in call_sids_list:
                is_in_any_call = True
                break
        if not is_in_any_call:
            target_peer_sid = sid
            break

    if target_peer_sid:
        if requester_sid in video_calls_in_room.get(room, []):
            emit("call_rejected", {"from": "System", "reason": "You are already in a call."}, room=requester_sid)
            logger.warning(f"Requester {name} (SID: {requester_sid}) attempted call while already in one.")
            return

        video_calls_in_room[room] = [requester_sid, target_peer_sid]

        target_peer_name = rooms[room]["sids"].get(target_peer_sid, "another user")
        emit("call_request", {"from": name, "requester_sid": requester_sid}, room=target_peer_sid)
        emit("call_status", {"message": f"Calling {target_peer_name}..."}, room=requester_sid)
        logger.info(
            f"{name} (SID: {requester_sid}) requested a call to {target_peer_name} (SID: {target_peer_sid}) in room {room}.")
    else:
        emit("call_rejected", {"from": "System", "reason": "No available peer for a video call."}, room=requester_sid)
        logger.info(f"{name} (SID: {requester_sid}) tried to start a call in room {room} but no available peer.")


@socketio.on("call_response")
def handle_call_response(data):
    room = session.get("room")
    respondent_name = session.get("name")
    respondent_sid = request.sid
    requester_sid = data.get("requester_sid")
    action = data.get("action")

    if not room or not respondent_name or not requester_sid or action not in ["accept", "reject"] or room not in rooms:
        logger.warning(f"Invalid call response received. SID: {respondent_sid}, Data: {data}")
        return

    requester_name = rooms[room]["sids"].get(requester_sid, "Caller")

    expected_call_sids = sorted([requester_sid, respondent_sid])
    current_call_sids = sorted(video_calls_in_room.get(room, []))

    if expected_call_sids != current_call_sids:
        emit("call_rejected", {"from": "System", "reason": "Call request expired or participant left."},
             room=respondent_sid)
        emit("call_rejected", {"from": "System", "reason": f"{respondent_name} could not join. Try again."},
             room=requester_sid)
        if room in video_calls_in_room: del video_calls_in_room[room]
        logger.warning(
            f"Call response mismatch for room {room}. Expected {expected_call_sids}, got {current_call_sids}.")
        return

    if action == "accept":
        emit("call_accepted", {"from": respondent_name, "accepted_sid": respondent_sid}, room=requester_sid)
        emit("call_status", {"message": f"You accepted the call from {requester_name}."}, room=respondent_sid)
        logger.info(
            f"{respondent_name} (SID: {respondent_sid}) accepted call from {requester_name} (SID: {requester_sid}) in room {room}.")
    elif action == "reject":
        emit("call_rejected", {"from": respondent_name, "reason": "rejected your call."}, room=requester_sid)
        emit("call_status", {"message": f"You rejected the call from {requester_name}."}, room=respondent_sid)
        if room in video_calls_in_room:
            video_calls_in_room[room] = []
        logger.info(
            f"{respondent_name} (SID: {respondent_sid}) rejected call from {requester_name} (SID: {requester_sid}) in room {room}.")


@socketio.on("offer")
def handle_offer(data):
    room = session.get("room")
    sender_sid = request.sid
    if not room or room not in rooms:
        logger.warning(f"Offer from invalid session. SID: {sender_sid}")
        return

    if room in video_calls_in_room and sender_sid in video_calls_in_room[room]:
        other_peer_sid = [sid for sid in video_calls_in_room[room] if sid != sender_sid]
        if other_peer_sid:
            emit("offer", {"offer": data["offer"]}, room=other_peer_sid[0])
            logger.info(
                f"Offer from {session.get('name')} (SID: {sender_sid}) relayed in room {room} to {rooms[room]['sids'].get(other_peer_sid[0], 'unknown')}.")
        else:
            logger.warning(
                f"Offer from {session.get('name')} (SID: {sender_sid}) in room {room} but no other peer found in active call.")
    else:
        logger.warning(
            f"Offer from {session.get('name')} (SID: {sender_sid}) in room {room} but not in an active call state.")


@socketio.on("answer")
def handle_answer(data):
    room = session.get("room")
    sender_sid = request.sid
    if not room or room not in rooms:
        logger.warning(f"Answer from invalid session. SID: {sender_sid}")
        return

    if room in video_calls_in_room and sender_sid in video_calls_in_room[room]:
        other_peer_sid = [sid for sid in video_calls_in_room[room] if sid != sender_sid]
        if other_peer_sid:
            emit("answer", {"answer": data["answer"]}, room=other_peer_sid[0])
            logger.info(
                f"Answer from {session.get('name')} (SID: {sender_sid}) relayed in room {room} to {rooms[room]['sids'].get(other_peer_sid[0], 'unknown')}.")
        else:
            logger.warning(
                f"Answer from {session.get('name')} (SID: {sender_sid}) in room {room} but no other peer found in active call.")
    else:
        logger.warning(
            f"Answer from {session.get('name')} (SID: {sender_sid}) in room {room} but not in an active call state.")


@socketio.on("ice_candidate")
def handle_ice_candidate(data):
    room = session.get("room")
    sender_sid = request.sid
    if not room or room not in rooms:
        logger.warning(f"ICE candidate from invalid session. SID: {sender_sid}")
        return

    if room in video_calls_in_room and sender_sid in video_calls_in_room[room]:
        other_peer_sid = [sid for sid in video_calls_in_room[room] if sid != sender_sid]
        if other_peer_sid:
            emit("ice_candidate", {"candidate": data["candidate"]}, room=other_peer_sid[0])
            logger.debug(
                f"ICE candidate from {session.get('name')} (SID: {sender_sid}) relayed in room {room} to {rooms[room]['sids'].get(other_peer_sid[0], 'unknown')}.")
        else:
            logger.warning(
                f"ICE candidate from {session.get('name')} (SID: {sender_sid}) in room {room} but no other peer found in active call.")
    else:
        logger.warning(
            f"ICE candidate from {session.get('name')} (SID: {sender_sid}) in room {room} but not in an active call state.")


@socketio.on("call_end")
def handle_call_end():
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    if not room or not name or room not in rooms:
        logger.warning(f"Call end request from invalid session. SID: {sid}")
        return

    if room in video_calls_in_room and sid in video_calls_in_room[room]:
        other_peer_sids = [s for s in video_calls_in_room[room] if s != sid]
        video_calls_in_room[room] = []

        if other_peer_sids:
            emit("call_end", {"name": name}, room=other_peer_sids[0])
            logger.info(
                f"User {name} (SID: {sid}) ended the call. Notified {rooms[room]['sids'].get(other_peer_sids[0], 'unknown')} in room {room}.")
        else:
            logger.info(f"User {name} (SID: {sid}) ended the call in room {room} (no other peer to notify).")
    else:
        logger.warning(
            f"User {name} (SID: {sid}) tried to end a call in room {room}, but was not in an active call state.")


# --- XOX Game Handlers ---
@socketio.on("game_start_request")
def handle_game_start_request():
    room = session.get("room")
    name = session.get("name")
    requester_sid = request.sid
    if not room or not name or room not in rooms:
        logger.warning(f"Game start request from invalid session. SID: {requester_sid}")
        return

    if rooms[room]["members"] < 2:
        emit("game_status", {"message": "Need 2 players to start XOX."}, room=requester_sid)
        logger.info(f"Game start denied for {name} in room {room}: Not enough players.")
        return
    if rooms[room]["game_active"]:
        emit("game_status", {"message": "A game is already active. Please wait or ask players to reset."},
             room=requester_sid)
        logger.info(f"Game start denied for {name} in room {room}: Game already active.")
        return

    available_sids = [sid for sid in rooms[room]["sids"].keys() if sid != requester_sid]
    if not available_sids:
        emit("game_status", {"message": "No other player available to start XOX."}, room=requester_sid)
        logger.warning(f"Game start denied for {name} in room {room}: No other players available.")
        return

    other_player_sid = random.choice(available_sids)

    player_sids = [requester_sid, other_player_sid]
    random.shuffle(player_sids)
    player_x_sid, player_o_sid = player_sids[0], player_sids[1]

    rooms[room]["player_x_sid"] = player_x_sid
    rooms[room]["player_o_sid"] = player_o_sid
    rooms[room]["game_active"] = True

    player_x_name = rooms[room]["sids"].get(player_x_sid, "Player X")
    player_o_name = rooms[room]["sids"].get(player_o_sid, "Player O")

    emit("game_start", {
        "player_x_name": player_x_name,
        "player_o_name": player_o_name,
        "your_symbol": "X",
        "is_your_turn": True
    }, room=player_x_sid)

    emit("game_start", {
        "player_x_name": player_x_name,
        "player_o_name": player_o_name,
        "your_symbol": "O",
        "is_your_turn": False
    }, room=player_o_sid)

    spectator_sids = [sid for sid in rooms[room]["sids"].keys() if sid not in [player_x_sid, player_o_sid]]
    for sid in spectator_sids:
        emit("game_status", {"message": f"XOX game started! {player_x_name} (X) vs {player_o_name} (O)."}, room=sid)

    send({"name": "System", "message": f"XOX game started! {player_x_name} (X) vs {player_o_name} (O)."}, to=room)
    logger.info(f"XOX game started in room {room} between {player_x_name} (X) and {player_o_name} (O).")


@socketio.on("game_move")
def handle_game_move(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    if not room or not name or room not in rooms:
        logger.warning(f"Game move from invalid session. SID: {sid}")
        return

    if not rooms[room]["game_active"]:
        emit("game_status", {"message": "Game not active."}, room=sid)
        logger.warning(f"Game move denied for {name} in room {room}: Game not active.")
        return

    expected_symbol = None
    if sid == rooms[room]["player_x_sid"]:
        expected_symbol = "X"
    elif sid == rooms[room]["player_o_sid"]:
        expected_symbol = "O"

    if data["symbol"] != expected_symbol:
        emit("game_status", {"message": "It's not your turn or you are not an active player in this game."}, room=sid)
        logger.warning(f"Game move denied for {name} (SID: {sid}) in room {room}: Invalid turn or not player.")
        return

    index = data.get("index")
    symbol = data.get("symbol")
    if not isinstance(index, int) or not (0 <= index < 9) or symbol not in ['X', 'O']:
        emit("game_status", {"message": "Invalid move data received."}, room=sid)
        logger.warning(f"Game move denied for {name} (SID: {sid}) in room {room}: Invalid move data {data}.")
        return

    next_turn_symbol = "X" if symbol == "O" else "O"
    next_turn_sid = rooms[room]["player_x_sid"] if next_turn_symbol == "X" else rooms[room]["player_o_sid"]

    emit("game_update", {
        "index": index,
        "symbol": symbol,
        "next_turn_symbol": next_turn_symbol,
        "player_name": name,
        "board_state": data["board_state"],
        "current_turn_sid": next_turn_sid
    }, room=room)
    logger.info(f"Game move from {name} (SID: {sid}) in room {room}: index {index}, symbol {symbol}.")


@socketio.on("game_over")
def handle_game_over(data):
    room = session.get("room")
    sid = request.sid
    if not room or room not in rooms:
        logger.warning(f"Game over request from invalid session. SID: {sid}")
        return

    rooms[room]["game_active"] = False
    rooms[room]["player_x_sid"] = None
    rooms[room]["player_o_sid"] = None

    emit("game_result", {
        "winner": data.get("winner"),
        "draw": data.get("draw"),
        "message": data["message"]
    }, room=room)
    if rooms[room]["members"] >= 2:
        emit("enable_game_start", room=room)
    logger.info(f"XOX game ended in room {room}. Result: {data.get('message')}.")


@socketio.on("game_reset_request")
def handle_game_reset_request():
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    if not room or room not in rooms:
        logger.warning(f"Game reset request from invalid session. SID: {sid}")
        return

    rooms[room]["game_active"] = False
    rooms[room]["player_x_sid"] = None
    rooms[room]["player_o_sid"] = None

    emit("game_reset", {"reason": f"{name} requested a new game."}, room=room)
    if rooms[room]["members"] >= 2:
        emit("enable_game_start", room=room)
    logger.info(f"XOX game in room {room} reset by {name}.")


# --- Main execution block ---
if __name__ == "__main__":
    # For production, replace debug=True with debug=False and rely on a WSGI server like Gunicorn.
    # The allow_unsafe_werkzeug=True is also for development to avoid issues with Werkzeug's reloader.
    # For Render, you'll typically use Gunicorn via the Procfile, so these specific run arguments
    # are less relevant for deployment but crucial for local testing.
    socketio.run(app, debug=True, host='0.0.0.0', allow_unsafe_werkzeug=True)