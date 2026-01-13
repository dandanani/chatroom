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
from datetime import datetime  # ADDED: Import for message timestamps

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask import Flask, render_template, request, session, redirect, url_for, flash
from flask_socketio import join_room, leave_room, send, SocketIO, emit
from markupsafe import escape

# Flask-Dance for Google OAuth (COMMENTED OUT FOR NOW)
# from flask_dance.contrib.google import make_google_blueprint, google
# from flask_dance.consumer import oauth_authorized, oauth_error
# from sqlalchemy.orm.exc import NoResultFound
# from flask_login import (
#     LoginManager,
#     UserMixin,
#     login_user,
#     logout_user,
#     current_user,
# )

# --- Flask App Configuration ---
app = Flask(__name__)

# Load SECRET_KEY from environment variable for production.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY",
                                          "a_very_long_and_random_string_for_dev_only_replace_this_in_prod")

# Security headers for session cookies
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = False
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
video_calls_in_room = {}  # {room_code: [sid1, sid2]}
CHARACTER_SET = ascii_uppercase + digits

# Predefined list of distinct, light colors for chat bubbles
CHAT_COLORS = [
    "#E0F7FA",  # Light Cyan
    "#FFFDE7",  # Light Yellow
    "#FCE4EC",  # Light Pink
    "#E8F5E9",  # Light Green
    "#F3E5F5",  # Light Purple
    "#E3F2FD",  # Light Blue
    "#FFF3E0",  # Light Orange
    "#FBE9E7",  # Light Peach
    "#E1F5FE",  # Lighter Blue
    "#F1F8E9"  # Lighter Green
]


# --- Flask-Login Configuration (for Flask-Dance) (COMMENTED OUT FOR NOW) ---
# login_manager = LoginManager()
# login_manager.init_app(app)
# login_manager.login_view = "google.login"

# class User(UserMixin):
#     def __init__(self, id, name=None):
#         self.id = id
#         self.name = name

#     def get_id(self):
#         return str(self.id)

# # In a real application, you would load users from a database
# # For this example, we'll store them in a simple dictionary
# users_db = {}  # {google_id: User_object}

# @login_manager.user_loader
# def load_user(user_id):
#     return users_db.get(user_id)

# --- Flask-Dance Google OAuth Configuration (COMMENTED OUT FOR NOW) ---
# google_blueprint = make_google_blueprint(
#     client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID"),
#     client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"),
#     scope=["openid", "https://www.googleapis.com/auth/userinfo.email",
#            "https://www.googleapis.com/auth/userinfo.profile"],
#     redirect_url="/login/google/authorized"  # Explicitly set redirect_url to match console setup
# )
# app.register_blueprint(google_blueprint, url_prefix="/login")

# --- OAuth Callbacks (COMMENTED OUT FOR NOW) ---
# @oauth_authorized.connect_via(google_blueprint)
# def google_logged_in(blueprint, token):
#     if not token:
#         flash("Failed to log in with Google.", category="error")
#         return False

#     resp = blueprint.session.get("/oauth2/v1/userinfo")
#     if not resp.ok:
#         msg = "Failed to fetch user info from Google."
#         flash(msg, category="error")
#         logger.error(msg + f" Response: {resp.text}")
#         return False

#     google_info = resp.json()
#     google_user_id = google_info["id"]
#     google_user_email = google_info["email"]
#     google_user_name = google_info.get("name", google_info["email"].split('@')[0])

#     # Find or create user
#     user = users_db.get(google_user_id)
#     if not user:
#         user = User(google_user_id, name=google_user_name)
#         users_db[google_user_id] = user
#         logger.info(f"New user created: {google_user_name} ({google_user_email})")
#     else:
#         # Update name if it changed (e.g., user updated Google profile name)
#         user.name = google_user_name
#         logger.info(f"Existing user logged in: {google_user_name} ({google_user_email})")

#     login_user(user)
#     flash(f"Successfully signed in with Google as {google_user_name}.", category="success")
#     session["name"] = google_user_name  # Store name in session for existing chat logic
#     return redirect(url_for("home"))

# @oauth_error.connect_via(google_blueprint)
# def google_error(blueprint, message):
#     flash(f"OAuth error from Google: {message}", category="error")
#     logger.error(f"OAuth error from Google: {message}")


# --- Helper Functions ---
def generate_unique_code(length):
    while True:
        code = "".join(random.choice(CHARACTER_SET) for _ in range(length))
        if code not in rooms:
            break
    return code


def get_user_color(room_code, user_sid):
    """Assigns and returns a consistent color for a user within a room."""
    room_data = rooms.get(room_code)
    if not room_data:
        return "#FFFFFF"  # Default white if room doesn't exist

    # Ensure 'user_colors' dictionary exists in room data
    if "user_colors" not in room_data:
        room_data["user_colors"] = {}

    if user_sid not in room_data["user_colors"]:
        # Assign a random color from the predefined list
        # Ensure distinct colors are used if possible, cycle through list
        # If all colors are used, it will reuse colors, which is fine for a small list
        available_colors = [c for c in CHAT_COLORS if c not in room_data["user_colors"].values()]
        if not available_colors:  # If all colors are in use, cycle from the beginning
            assigned_color = random.choice(CHAT_COLORS)
        else:
            assigned_color = random.choice(available_colors)
        room_data["user_colors"][user_sid] = assigned_color

    return room_data["user_colors"][user_sid]


# --- Flask Routes ---
@app.route("/", methods=["POST", "GET"])
@limiter.limit("10 per minute", methods=["POST"])
def home():
    # Clear session for fresh start, but preserve Flask-Login's current_user
    # (Removed Flask-Login specific session pops as it's commented out)
    session.clear()  # Clear all session data for a clean start

    if request.method == "POST":
        name = request.form.get("name")  # Get name directly from form
        code = request.form.get("code")
        join = "join" in request.form
        create = "create" in request.form
        mode = request.form.get("mode", "full")

        if not name:
            flash("Please enter your name.", category="error")
            return render_template("home.html", code=code)  # Pass code back if user entered it

        if join and not code:
            logger.warning(f"User {name} attempted to join without a room code.")
            flash("Please enter a room code.", category="error")
            return render_template("home.html", name=name, code=code)

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
                "player_o_sid": None,
                "user_colors": {}  # Initialize user colors for the room
            }
            video_calls_in_room[room] = []
            logger.info(f"Room '{room}' created by {name}.")
        elif code not in rooms:
            logger.warning(f"User {name} attempted to join non-existent room '{code}'.")
            flash("Room does not exist.", category="error")
            return render_template("home.html", name=name, code=code)

        session["room"] = room
        session["name"] = name  # Store name from form
        session["mode"] = mode
        logger.info(f"User {name} redirecting to room {room}.")
        return redirect(url_for("room"))

    # GET request: Render the home page with name if already in session (e.g., after a redirect)
    # Or empty if starting fresh
    return render_template("home.html", name=session.get("name", ""))


@app.route("/logout")
def logout():
    session.clear()  # Clear all session data
    # logout_user() # COMMENTED OUT FOR NOW
    flash("You have been logged out.", category="info")
    logger.info("User logged out.")
    return redirect(url_for("home"))


@app.route("/room")
def room():
    room = session.get("room")
    name = session.get("name")  # This name comes from the session

    # Check if room and name are in session
    if room is None or name is None or room not in rooms:
        logger.warning(
            f"Unauthorized access attempt to /room. Session: {session.get('room')}, {session.get('name')}")
        flash("Please join or create a room first.", category="error")
        return redirect(url_for("home"))

    mode = rooms[room].get("mode", "full")
    messages = rooms[room]["messages"]
    if mode == "privacy":
        messages = messages[-5:]

    logger.info(f"Rendering room {room} for user {name}.")

    # NEW: Ensure messages have a timestamp field before rendering (for older messages)
    for msg in messages:
        if "timestamp" not in msg:
            msg["timestamp"] = datetime.now().strftime("%I:%M %p")  # Use current time as fallback

    return render_template("room.html", code=room, messages=messages, mode=mode, name=name)


# --- Socket.IO Event Handlers ---
@socketio.on("message")
def message(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    # Removed current_user.is_authenticated check
    if not room or not name or room not in rooms:
        logger.warning(f"Message attempt from invalid session SID: {sid}")
        return

    current_time = time.time()
    if current_time - last_message_time[sid] < MESSAGE_COOLDOWN:
        emit("error", {"message": "Please slow down! You are sending messages too fast."}, room=sid)
        logger.warning(f"Rate limit hit for message from {name} (SID: {sid}) in room {room}.")
        return

    last_message_time[sid] = current_time
    sanitized_message = escape(data.get("data", ""))
    user_color = get_user_color(room, sid)  # Get the color for the sender

    timestamp = datetime.now().strftime("%I:%M %p")  # NEW: Generate timestamp

    content = {
        "name": name,
        "message": sanitized_message,
        "color": user_color,
        "timestamp": timestamp  # NEW: Include the timestamp
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
    # Removed current_user.is_authenticated check
    if not room or not name or room not in rooms:
        return

    emit("typing", {"name": name}, room=room, include_self=False)


@socketio.on("connect")
def connect(auth):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    # Removed current_user.is_authenticated check
    if not room or not name or room not in rooms:
        logger.warning(
            f"Connection attempt from invalid session. SID: {sid}.")
        return False

    join_room(room)
    rooms[room]["sids"][sid] = name  # Store SID to name mapping
    user_color = get_user_color(room, sid)
    rooms[room]["user_colors"][sid] = user_color  # Ensure SID-to-color mapping is stored for this session

    timestamp = datetime.now().strftime("%I:%M %p")  # NEW: Timestamp for system messages

    send({"name": "System", "message": f"{name} has joined the room.", "timestamp": timestamp}, to=room,
         include_self=False)  # ADDED timestamp
    send({"name": "System", "message": f"Welcome to room {room}, {name}!", "timestamp": timestamp},
         to=sid)  # ADDED timestamp
    rooms[room]["members"] += 1

    # Update game state if necessary (e.g., enable the game button)
    if rooms[room]["members"] >= 2:
        emit("enable_game_start", room=room)
    else:
        emit("disable_game_start", room=room)

    emit("user_count", rooms[room]["members"], to=room)
    emit("room_users_list", {"users": list(rooms[room]["sids"].values()), "sids": list(rooms[room]["sids"].keys())},
         to=room)  # Send full user list

    # Send any pending messages to the newly connected user
    mode = rooms[room].get("mode", "full")
    messages = rooms[room]["messages"]
    if mode == "privacy":
        messages = messages[-5:]

    for msg in messages:
        # Ensure older messages have a timestamp field before sending
        if "timestamp" not in msg:
            msg["timestamp"] = datetime.now().strftime("%I:%M %p")  # Use current time as fallback

        send(msg, to=sid)

    logger.info(f"User {name} connected to room {room}. SID: {sid}")


@socketio.on("disconnect")
def disconnect():
    room = session.get("room")
    name = session.get("name")
    sid = request.sid

    # Removed current_user.is_authenticated check
    if room is None or name is None or room not in rooms or sid not in rooms[room]["sids"]:
        logger.warning(f"Disconnect event from invalid session or unknown SID: {sid}")
        return

    # Handle game state cleanup if the leaving user was a player
    if rooms[room].get("player_x_sid") == sid:
        rooms[room]["game_active"] = False
        rooms[room]["player_x_sid"] = None
        send({"name": "System", "message": f"{name} (Player X) has left the room. The XOX game has ended."}, to=room)

    if rooms[room].get("player_o_sid") == sid:
        rooms[room]["game_active"] = False
        rooms[room]["player_o_sid"] = None
        send({"name": "System", "message": f"{name} (Player O) has left the room. The XOX game has ended."}, to=room)

    # Handle video call cleanup if the leaving user was in a call
    if room in video_calls_in_room and sid in video_calls_in_room[room]:
        video_calls_in_room[room].remove(sid)
        # Notify the other person in the call (if any)
        if video_calls_in_room[room]:
            remaining_sid = video_calls_in_room[room][0]
            emit("end_video_call", {"reason": f"{name} left the call."}, room=remaining_sid)
        del video_calls_in_room[room]

    rooms[room]["members"] -= 1
    del rooms[room]["sids"][sid]
    # Remove user color from room data
    if sid in rooms[room]["user_colors"]:
        del rooms[room]["user_colors"][sid]

    timestamp = datetime.now().strftime("%I:%M %p")  # NEW: Timestamp for system messages

    send({"name": "System", "message": f"{name} has left the room.", "timestamp": timestamp}, to=room,
         include_self=False)  # ADDED timestamp
    emit("user_count", rooms[room]["members"], to=room)

    # Update game state (disable button if count drops below 2)
    if rooms[room]["members"] < 2:
        emit("disable_game_start", room=room)

    emit("room_users_list", {"users": list(rooms[room]["sids"].values()), "sids": list(rooms[room]["sids"].keys())},
         to=room)  # Send full user list

    # Remove room if empty
    if rooms[room]["members"] <= 0:
        del rooms[room]
        if room in video_calls_in_room:
            del video_calls_in_room[room]
        logger.info(f"Room {room} deleted as it is now empty.")

    leave_room(room)
    logger.info(f"User {name} disconnected from room {room}. SID: {sid}")


@socketio.on("start_xox_game")
def start_xox_game(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    # Removed current_user.is_authenticated check
    if not room or not name or room not in rooms:
        emit("error", {"message": "You are not authorized to start games. Please join a room."}, room=sid)
        return

    if rooms[room]["members"] < 2:
        emit("error", {"message": "Need at least 2 players to start XOX."}, room=sid)
        return

    if rooms[room]["game_active"]:
        emit("error", {"message": "A game is already in progress."}, room=sid)
        return

    # Select two random players for X and O
    all_sids = list(rooms[room]["sids"].keys())
    if len(all_sids) < 2:
        emit("error", {"message": "Two active players are required to start the game."}, room=sid)
        return

    # Randomly pick two distinct players
    players = random.sample(all_sids, 2)
    player_x_sid = players[0]
    player_o_sid = players[1]

    player_x_name = rooms[room]["sids"][player_x_sid]
    player_o_name = rooms[room]["sids"][player_o_sid]

    rooms[room]["game_active"] = True
    rooms[room]["player_x_sid"] = player_x_sid
    rooms[room]["player_o_sid"] = player_o_sid

    timestamp = datetime.now().strftime("%I:%M %p")  # NEW: Timestamp for system messages

    # Notify the room and the players
    send({"name": "System",
          "message": f"XOX Game Started! {player_x_name} is X, {player_o_name} is O. It is {player_x_name}'s turn (X).",
          "timestamp": timestamp}, to=room)
    emit("xox_game_started", {
        "player_x_sid": player_x_sid,
        "player_o_sid": player_o_sid,
        "player_x_name": player_x_name,
        "player_o_name": player_o_name,
        "current_turn_sid": player_x_sid,  # X always starts
        "board": ["", "", "", "", "", "", "", "", ""]
    }, room=room)

    logger.info(f"XOX game started in room {room}. X: {player_x_name}, O: {player_o_name}")


@socketio.on("xox_move")
def xox_move(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    # Removed current_user.is_authenticated check
    if not room or not name or room not in rooms:
        emit("error", {"message": "Invalid session for XOX move."}, room=sid)
        return

    # Validate move data
    if "index" not in data or "board" not in data or "next_turn_sid" not in data or "current_marker" not in data:
        emit("error", {"message": "Invalid move data received."}, room=sid)
        return

    # Security/logic check: Ensure the sender is one of the active players
    is_player_x = rooms[room].get("player_x_sid") == sid
    is_player_o = rooms[room].get("player_o_sid") == sid

    if not is_player_x and not is_player_o:
        emit("error", {"message": "You are not an active player in this game."}, room=sid)
        return

    # Logic check: Ensure the move is for the player whose turn it is
    if data["current_marker"] == 'X' and not is_player_x:
        emit("error", {"message": "It is not your turn (Player O)."}, room=sid)
        return

    if data["current_marker"] == 'O' and not is_player_o:
        emit("error", {"message": "It is not your turn (Player X)."}, room=sid)
        return

    # Broadcast the move and the next turn
    emit("xox_move_made", {
        "index": data["index"],
        "marker": data["current_marker"],
        "next_turn_sid": data["next_turn_sid"],
        "next_turn_name": rooms[room]["sids"].get(data["next_turn_sid"], "Unknown Player")
    }, room=room, include_self=True)

    logger.info(f"XOX move made by {name} in room {room}: Index {data['index']}, Marker {data['current_marker']}")


@socketio.on("game_over")
def game_over(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    # Removed current_user.is_authenticated check
    if not room or not name or room not in rooms:
        emit("error", {"message": "You are not authorized to end games. Please join a room."}, room=sid)
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

    logger.info(f"XOX game ended in room {room} by {name}. Result: {data.get('message')}.")


@socketio.on("game_reset_request")
def handle_game_reset_request():
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    # Removed current_user.is_authenticated check
    if not room or not name or room not in rooms:
        logger.warning(f"Game reset request from invalid session. SID: {sid}")
        emit("error", {"message": "You are not authorized to reset games. Please join a room."}, room=sid)
        return

    rooms[room]["game_active"] = False
    rooms[room]["player_x_sid"] = None
    rooms[room]["player_o_sid"] = None

    timestamp = datetime.now().strftime("%I:%M %p")  # NEW: Timestamp for system messages

    emit("game_reset", {"reason": f"{name} requested a new game.", "timestamp": timestamp},
         room=room)  # ADDED timestamp

    if rooms[room]["members"] >= 2:
        emit("enable_game_start", room=room)

    logger.info(f"XOX game reset in room {room} by {name}.")


# --- WebRTC Signaling Handlers (VIDEO CALL FIXES) ---

@socketio.on("request_video_call")
def request_video_call(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    recipient_sid = data.get("recipient_sid")

    if not room or not name or room not in rooms or recipient_sid not in rooms[room]["sids"]:
        emit("error", {"message": "Invalid call request."}, room=sid)
        return

    if rooms[room]["members"] != 2:
        emit("error", {"message": "Video calling is currently supported for exactly two users in a room only."},
             room=sid)
        return

    # Check if a call is already in progress in this room
    current_call_sids = video_calls_in_room.get(room, [])
    if len(current_call_sids) >= 2:
        emit("error", {"message": "A video call is already active in this room."}, room=sid)
        return

    # Simple 1:1 signaling - send offer to recipient
    emit("video_offer", {
        "offer": data["offer"],
        "sender_sid": sid,
        "sender_name": name
    }, room=recipient_sid)
    logger.info(
        f"Video call offer sent from {name} (SID: {sid}) to {rooms[room]['sids'][recipient_sid]} (SID: {recipient_sid}) in room {room}.")

# -------- WEBRTC SIGNALING --------

@socketio.on("call_request")
def handle_call_request(data):
    room = session.get("room")
    caller_sid = request.sid
    caller_name = session.get("name")
    target_sid = data.get("target_sid")

    if not room or room not in rooms:
        return

    # Auto-pick other user if only 2 users
    if not target_sid:
        for sid in rooms[room]["sids"]:
            if sid != caller_sid:
                target_sid = sid
                break

    if not target_sid or target_sid not in rooms[room]["sids"]:
        emit("call_rejected", {
            "from": "System",
            "reason": "User not available"
        }, to=caller_sid)
        return

    emit("call_request", {
        "from": caller_name,
        "requester_sid": caller_sid
    }, to=target_sid)



@socketio.on("call_response")
def handle_call_response(data):
    action = data.get("action")
    requester_sid = data.get("requester_sid")
    responder_sid = request.sid
    responder_name = session.get("name")

    if action == "accept":
        emit("call_accepted", {
            "from": responder_name,
            "requester_sid": responder_sid
        }, to=requester_sid)

    else:
        emit("call_rejected", {
            "from": responder_name,
            "reason": "rejected the call"
        }, to=requester_sid)



@socketio.on("offer")
def handle_offer(data):
    emit("offer", {
        "offer": data["offer"],
        "from_sid": request.sid
    }, to=data["target_sid"])



@socketio.on("answer")
def handle_answer(data):
    emit("answer", {
        "answer": data["answer"]
    }, to=data["target_sid"])


@socketio.on("ice_candidate")
def handle_ice_candidate(data):
    emit("ice_candidate", {
        "candidate": data["candidate"]
    }, to=data["target_sid"])


@socketio.on("call_end")
def handle_call_end():
    room = session.get("room")
    sid = request.sid

    if room and room in rooms:
        for other_sid in rooms[room]["sids"]:
            if other_sid != sid:
                emit("call_end", {"name": session.get("name")}, to=other_sid)

@socketio.on("video_answer")
def video_answer(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    recipient_sid = data.get("recipient_sid")

    if not room or not name or room not in rooms or recipient_sid not in rooms[room]["sids"]:
        return

    # Send the answer back to the initiator
    emit("video_answer", {
        "answer": data["answer"],
        "sender_sid": sid,
        "sender_name": name
    }, room=recipient_sid)

    # Establish the call state
    video_calls_in_room[room] = [sid, recipient_sid]
    logger.info(
        f"Video call answer sent from {name} (SID: {sid}) to {rooms[room]['sids'][recipient_sid]} (SID: {recipient_sid}) in room {room}.")


@socketio.on("ice_candidate")
def ice_candidate(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    recipient_sid = data.get("recipient_sid")

    if not room or not name or room not in rooms or recipient_sid not in rooms[room]["sids"]:
        return

    # Forward the ICE candidate to the peer
    emit("ice_candidate", {
        "candidate": data["candidate"],
        "sender_sid": sid
    }, room=recipient_sid)


@socketio.on("end_video_call")
def end_video_call(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid

    if room not in video_calls_in_room:
        return

    # Determine the peer's SID
    call_sids = video_calls_in_room.get(room, [])

    if sid in call_sids:
        peer_sid = [s for s in call_sids if s != sid]

        # Notify the peer if they exist
        if peer_sid:
            emit("end_video_call", {"reason": f"{name} ended the call."}, room=peer_sid[0])
            logger.info(f"Video call ended in room {room} by {name}.")

        del video_calls_in_room[room]


if __name__ == "__main__":
    logger.info("Starting Flask application...")
    # Use the eventlet server for SocketIO in production/deployment
    # socketio.run(app, host="0.0.0.0", port=int(os.environ.get('PORT', 5000)), debug=True, log_output=True)
    # For local development with Flask's default server (less efficient for concurrent connections):
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get('PORT', 5000)), debug=True)