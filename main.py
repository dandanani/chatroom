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
        if not available_colors: # If all colors are in use, cycle from the beginning
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
    session.clear() # Clear all session data for a clean start

    if request.method == "POST":
        name = request.form.get("name") # Get name directly from form
        code = request.form.get("code")
        join = "join" in request.form
        create = "create" in request.form
        mode = request.form.get("mode", "full")

        if not name:
            flash("Please enter your name.", category="error")
            return render_template("home.html", code=code) # Pass code back if user entered it

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

    content = {
        "name": name,
        "message": sanitized_message,
        "color": user_color  # Include the user's assigned color
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
    rooms[room]["sids"][sid] = name

    # Assign color to the connecting user
    user_color = get_user_color(room, sid)
    rooms[room]["user_colors"][sid] = user_color  # Ensure SID-to-color mapping is stored for this session

    send({"name": "System", "message": f"{name} has joined the room."}, to=room, include_self=False)
    send({"name": "System", "message": f"Welcome to room {room}, {name}!"}, to=sid)
    rooms[room]["members"] += 1
    emit("user_count", rooms[room]["members"], to=room)

    # Send updated list of users to all clients for the call selection modal
    online_users = [{"sid": s, "name": rooms[room]["sids"][s]} for s in rooms[room]["sids"].keys()]
    emit("online_users_update", online_users, room=room)

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
    name = session.get("name")  # Get name before session is potentially cleared
    sid = request.sid

    if not room or room not in rooms:
        logger.info(f"Disconnected from invalid/non-existent room. SID: {sid}")
        return

    leave_room(room)

    disconnected_name = rooms[room]["sids"].pop(sid, "Unknown User")  # Remove SID and get the name, with fallback

    # Remove user's color mapping
    if sid in rooms[room].get("user_colors", {}):
        del rooms[room]["user_colors"][sid]

    rooms[room]["members"] -= 1

    # Handle video call cleanup
    if room in video_calls_in_room:
        if sid in video_calls_in_room[room]:
            # If the disconnected user was in a call, end it for the other peer
            other_peer_sids_in_call = [s for s in video_calls_in_room[room] if s != sid]
            video_calls_in_room[room] = []  # Clear call participants for this room
            if other_peer_sids_in_call:
                emit("call_end", {"name": disconnected_name}, room=other_peer_sids_in_call[0])
                logger.info(f"Video call in room {room} ended by {disconnected_name}.")

    # Handle XOX game cleanup if the disconnected player was active
    if rooms[room]["game_active"]:
        if rooms[room]["player_x_sid"] == sid or rooms[room]["player_o_sid"] == sid:
            rooms[room]["game_active"] = False
            rooms[room]["player_x_sid"] = None
            rooms[room]["player_o_sid"] = None
            emit("game_reset", {"reason": f"{disconnected_name} left, ending the game."}, room=room)
            logger.info(f"XOX game in room {room} ended because {disconnected_name} disconnected.")

    # Notify other users in the room about the disconnection
    if rooms[room]["members"] > 0:
        send({"name": "System", "message": f"{disconnected_name} has left the room."}, to=room)
        # Send updated list of users to all clients for the call selection modal
        online_users = [{"sid": s, "name": rooms[room]["sids"][s]} for s in rooms[room]["sids"].keys()]
        emit("online_users_update", online_users, room=room)

    # Update user count for all remaining members
    emit("user_count", rooms[room]["members"], to=room)

    # If less than 2 members, disable game start
    if rooms[room]["members"] < 2:
        emit("disable_game_start", room=room)
        rooms[room]["game_active"] = False  # Ensure game state is reset if players drop below 2
        rooms[room]["player_x_sid"] = None
        rooms[room]["player_o_sid"] = None
        emit("game_reset", {"reason": "Not enough players."}, room=room)  # Notify if game was active

    logger.info(
        f"User {disconnected_name} (SID: {sid}) disconnected from room {room}. Remaining members: {rooms[room]['members']}.")


@socketio.on("get_room_users")
def get_room_users():
    """Sends the list of online users in the current room to the requester."""
    room = session.get("room")
    sid = request.sid

    # Removed current_user.is_authenticated check
    if not room or room not in rooms:
        logger.warning(f"Attempt to get room users from invalid session. SID: {sid}")
        return

    # Prepare list of users (name and sid) excluding the requester
    online_users = []
    for user_sid, user_name in rooms[room]["sids"].items():
        if user_sid != sid:  # Exclude self
            online_users.append({"sid": user_sid, "name": user_name})
    emit("room_users_list", online_users, room=sid)
    logger.info(f"Sent room user list to SID: {sid} in room {room}.")


@socketio.on("call_request")
def handle_call_request(data):
    """
    Handles a request to start a video call.
    `data` can contain 'target_sid' for a direct call, or be empty for a general request.
    """
    room = session.get("room")
    name = session.get("name")
    requester_sid = request.sid
    target_sid = data.get("target_sid")  # The SID of the specific user to call

    # Removed current_user.is_authenticated check
    if not room or not name or room not in rooms:
        logger.warning(f"Call request from invalid session. SID: {requester_sid}")
        emit("error", {"message": "You are not authorized to make calls. Please join a room."}, room=requester_sid)
        return

    # If already in a call, reject new requests
    if requester_sid in video_calls_in_room.get(room, []):
        emit("call_rejected", {"from": "System", "reason": "You are already in a call."}, room=requester_sid)
        logger.warning(f"Requester {name} (SID: {requester_sid}) attempted call while already in one.")
        return

    # Determine the target peer(s)
    if target_sid:
        # Direct call to a specific user
        connected_sids = [target_sid]
    else:
        # General call: find an available peer (for 1:1 fallback)
        connected_sids = [sid for sid in rooms[room]["sids"].keys() if sid != requester_sid]

    target_peer_sid = None
    for sid in connected_sids:
        # Check if this SID is already part of an active call in this room
        is_in_any_call = False
        for call_sids_list in video_calls_in_room.values():
            if sid in call_sids_list:
                is_in_any_call = True
                break
        if not is_in_any_call:
            target_peer_sid = sid
            break

    if target_peer_sid:
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

    # Removed current_user.is_authenticated check
    if not room or not respondent_name or not requester_sid or action not in [
        "accept", "reject"] or room not in rooms:
        logger.warning(
            f"Invalid call response received from invalid session. SID: {respondent_sid}, Data: {data}")
        return

    requester_name = rooms[room]["sids"].get(requester_sid, "Caller")

    # Critical check: Ensure this response is for the currently active call involving these two SIDs
    # This prevents old responses or responses for different calls from interfering
    expected_call_sids = sorted([requester_sid, respondent_sid])
    current_call_sids = sorted(video_calls_in_room.get(room, []))

    if expected_call_sids != current_call_sids:
        emit("call_rejected", {"from": "System", "reason": "Call request expired or participant left."},
             room=respondent_sid)
        emit("call_rejected", {"from": "System", "reason": f"{respondent_name} could not join. Try again."},
             room=requester_sid)
        if room in video_calls_in_room:
            del video_calls_in_room[room]  # Clear the invalid call state
        logger.warning(
            f"Call response mismatch in room {room}. Expected {expected_call_sids}, got {current_call_sids}. Call reset.")
        return

    if action == "accept":
        emit("call_accepted", {"from": respondent_name, "respondent_sid": respondent_sid}, room=requester_sid)
        logger.info(
            f"Call from {requester_name} (SID: {requester_sid}) accepted by {respondent_name} (SID: {respondent_sid}) in room {room}.")
    else:  # action == "reject"
        emit("call_rejected", {"from": respondent_name, "reason": "declined"}, room=requester_sid)
        # Clear the call from tracking if rejected
        if room in video_calls_in_room:
            del video_calls_in_room[room]
        logger.info(
            f"Call from {requester_name} (SID: {requester_sid}) rejected by {respondent_name} (SID: {respondent_sid}) in room {room}.")


@socketio.on("offer")
def handle_offer(data):
    """Relays WebRTC SDP offer from one peer to the other."""
    room = session.get("room")
    name = session.get("name") # Added name for logging
    sid = request.sid # Added sid for logging
    if not room:
        logger.warning(f"Offer from invalid session (no room). SID: {sid}")
        return
    # Emit the offer to everyone in the room except the sender
    emit("offer", {"offer": data["offer"]}, room=room, include_self=False)
    logger.info(f"Offer from {name} (SID: {sid}) relayed in room {room}")


@socketio.on("answer")
def handle_answer(data):
    """Relays WebRTC SDP answer from one peer to the other."""
    room = session.get("room")
    name = session.get("name") # Added name for logging
    sid = request.sid # Added sid for logging
    if not room:
        logger.warning(f"Answer from invalid session (no room). SID: {sid}")
        return
    # Emit the answer to everyone in the room except the sender
    emit("answer", {"answer": data["answer"]}, room=room, include_self=False)
    logger.info(f"Answer from {name} (SID: {sid}) relayed in room {room}")


@socketio.on("ice_candidate")
def handle_ice_candidate(data):
    """Relays WebRTC ICE candidates between peers."""
    room = session.get("room")
    name = session.get("name") # Added name for logging
    sid = request.sid # Added sid for logging
    if not room:
        logger.warning(f"ICE candidate from invalid session (no room). SID: {sid}")
        return
    # Emit the ICE candidate to everyone in the room except the sender
    emit("ice_candidate", {"candidate": data["candidate"]}, room=room, include_self=False)
    logger.debug(f"ICE candidate from {name} (SID: {sid}) relayed in room {room}")


@socketio.on("call_end")
def handle_call_end():
    """
    Handles when a user ends a video call.
    Notifies the other peer and resets call state.
    """
    room = session.get("room")
    name = session.get("name")
    sid = request.sid # Added sid for logging
    if not room or not name:
        logger.warning(f"Call end request from invalid session (no room/name). SID: {sid}")
        return

    # Notify the other peer in the room that the call has ended
    emit("call_end", {"name": name}, room=room, include_self=False)

    # Clear the active call participants for this room
    if room in video_calls_in_room:
        del video_calls_in_room[room]
    logger.info(f"Video call in room {room} ended by {name} (SID: {sid}).")


# --- XOX Game Logic ---
# Add a simple game board for XOX
# {room_code: [' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ']}
xox_boards = {}
# To track whose turn it is
# {room_code: 'X' or 'O'}
xox_current_turn = {}


@socketio.on("start_xox_game")
def start_xox_game():
    room = session.get("room")
    sid = request.sid
    name = session.get("name") # Added name for logging

    # Removed current_user.is_authenticated check
    if not room or room not in rooms:
        logger.warning(f"Game start request from invalid session. SID: {sid}")
        emit("error", {"message": "Please join a room first to start a game."}, room=sid)
        return

    if rooms[room]["members"] < 2:
        emit("error", {"message": "Need at least two players to start a game."}, room=sid)
        logger.warning(f"Game start attempted in room {room} with less than 2 members by {name}.")
        return

    if rooms[room]["game_active"]:
        emit("error", {"message": "A game is already active in this room. Reset it to start a new one."}, room=sid)
        logger.warning(f"Game start attempted in room {room} by {name} while a game is already active.")
        return

    # Assign players X and O randomly
    sids_in_room = list(rooms[room]["sids"].keys())
    random.shuffle(sids_in_room)
    player_x_sid = sids_in_room[0]
    player_o_sid = sids_in_room[1]

    rooms[room]["player_x_sid"] = player_x_sid
    rooms[room]["player_o_sid"] = player_o_sid
    rooms[room]["game_active"] = True

    xox_boards[room] = [' '] * 9  # Reset board
    xox_current_turn[room] = 'X'  # X always starts

    player_x_name = rooms[room]["sids"].get(player_x_sid, "Player X")
    player_o_name = rooms[room]["sids"].get(player_o_sid, "Player O")

    # Notify all clients in the room that a game has started
    emit("xox_game_started", {
        "board": xox_boards[room],
        "turn": xox_current_turn[room],
        "player_x_name": player_x_name,
        "player_o_name": player_o_name,
        "player_x_sid": player_x_sid,
        "player_o_sid": player_o_sid,
        "message": f"An XOX game has started! {player_x_name} is X, {player_o_name} is O. It's {player_x_name}'s turn (X)."
    }, room=room)
    logger.info(f"XOX game started in room {room} by {name} with X: {player_x_name}, O: {player_o_name}.")


@socketio.on("xox_move")
def handle_xox_move(data):
    room = session.get("room")
    name = session.get("name")
    sid = request.sid
    position = data["position"]

    # Removed current_user.is_authenticated check
    if not room or not name or room not in rooms:
        logger.warning(f"XOX move from invalid session. SID: {sid}")
        emit("error", {"message": "Please join a room first to play."}, room=sid)
        return

    if not rooms[room]["game_active"]:
        emit("error", {"message": "No active XOX game in this room."}, room=sid)
        logger.warning(f"XOX move attempted in room {room} by {name} with no active game.")
        return

    board = xox_boards.get(room)
    current_turn = xox_current_turn.get(room)

    if not board or not current_turn:
        logger.error(f"XOX game state corrupted for room {room} by {name}.")
        return

    player_x_sid = rooms[room]["player_x_sid"]
    player_o_sid = rooms[room]["player_o_sid"]

    # Determine player's mark based on their SID
    player_mark = None
    if sid == player_x_sid:
        player_mark = 'X'
    elif sid == player_o_sid:
        player_mark = 'O'

    if not player_mark:
        emit("error", {"message": "You are not a player in this game."}, room=sid)
        logger.warning(f"Non-player {name} (SID: {sid}) attempted XOX move in room {room}.")
        return

    if player_mark != current_turn:
        emit("error", {"message": f"It's {current_turn}'s turn."}, room=sid)
        logger.warning(f"Player {name} (SID: {sid}) attempted move out of turn in room {room}.")
        return

    if not (0 <= position < 9) or board[position] != ' ':
        emit("error", {"message": "Invalid move. Choose an empty cell."}, room=sid)
        logger.warning(f"Invalid XOX move attempted by {name} (SID: {sid}) at position {position} in room {room}.")
        return

    board[position] = player_mark

    # Check for win or draw
    winner = check_xox_win(board)
    if winner:
        rooms[room]["game_active"] = False
        emit("xox_game_over", {"board": board, "winner": winner, "message": f"{winner} wins!"}, room=room)
        logger.info(f"XOX game in room {room} ended. Winner: {winner}.")
    elif ' ' not in board:
        rooms[room]["game_active"] = False
        emit("xox_game_over", {"board": board, "draw": True, "message": "It's a draw!"}, room=room)
        logger.info(f"XOX game in room {room} ended in a draw.")
    else:
        # Switch turn
        new_turn = 'O' if current_turn == 'X' else 'X'
        xox_current_turn[room] = new_turn
        next_player_sid = player_o_sid if new_turn == 'O' else player_x_sid
        next_player_name = rooms[room]["sids"].get(next_player_sid, "Next Player")
        emit("xox_board_update", {
            "board": board,
            "turn": new_turn,
            "message": f"It's {next_player_name}'s turn ({new_turn})."
        }, room=room)
        logger.info(f"XOX move by {name} (SID: {sid}) at {position} in room {room}. Next turn: {new_turn}.")


def check_xox_win(board):
    win_conditions = [
        # Rows
        [0, 1, 2], [3, 4, 5], [6, 7, 8],
        # Columns
        [0, 3, 6], [1, 4, 7], [2, 5, 8],
        # Diagonals
        [0, 4, 8], [2, 4, 6]
    ]
    for condition in win_conditions:
        if board[condition[0]] == board[condition[1]] == board[condition[2]] != ' ':
            return board[condition[0]]
    return None


@socketio.on("game_over")
def handle_game_over(data):
    room = session.get("room")
    sid = request.sid
    name = session.get("name") # Added name for logging
    # Removed current_user.is_authenticated check
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

    emit("game_reset", {"reason": f"{name} requested a new game."}, room=room)
    if rooms[room]["members"] >= 2:
        emit("enable_game_start", room=room)
    logger.info(f"XOX game in room {room} reset by {name}.")


# --- Main execution block ---
if __name__ == "__main__":
    # For production, replace debug=True with debug=False and rely on a WSGI server like Gunicorn
    # For development, run directly:
    socketio.run(app, debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))