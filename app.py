import os
import re
from datetime import datetime, timedelta
from functools import wraps

import boto3
import pymysql
from botocore.exceptions import ClientError
from flask import (
    Flask, flash, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# App setup & AWS Configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = "eventhub-secret-key-change-in-production"

# AWS S3 Configuration
S3_BUCKET_NAME = "your-s3-bucket-name"
s3_client = boto3.client('s3')

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
ALLOWED_DEPARTMENTS = {"CSE", "ME", "CE", "EC", "AH", "EEE"}

# Invite expiry & cooldown durations (seconds)
INVITE_EXPIRY_SECONDS = 180       # 3 minutes
DECLINE_COOLDOWN_SECONDS = 120    # 2 minutes

def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# AWS RDS Configuration (pymysql)
DB_HOST = "your-rds-endpoint.amazonaws.com"
DB_USER = "admin"
DB_PASS = "yourpassword"
DB_NAME = "eventhub_db"

def get_db_connection():
    """Establish and return a connection to the RDS MySQL database."""
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor
    )

# ---------------------------------------------------------------------------
# Initialize Tables (Run once to set up RDS)
# ---------------------------------------------------------------------------
def init_db():
    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(120) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                student_id VARCHAR(100) NOT NULL,
                department VARCHAR(10) NOT NULL,
                student_id_file VARCHAR(255)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id VARCHAR(50) PRIMARY KEY,
                title VARCHAR(150) NOT NULL,
                description TEXT,
                date VARCHAR(50),
                category VARCHAR(50),
                location VARCHAR(150),
                capacity INT,
                team_event BOOLEAN DEFAULT FALSE,
                min_members INT DEFAULT 1,
                max_members INT DEFAULT 1
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS registrations (
                id INT AUTO_INCREMENT PRIMARY KEY,
                email VARCHAR(120) NOT NULL,
                event_id VARCHAR(50) NOT NULL,
                registered_at DATETIME NOT NULL,
                file_uploaded VARCHAR(255),
                is_team_registration BOOLEAN DEFAULT FALSE,
                team_id INT DEFAULT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id INT AUTO_INCREMENT PRIMARY KEY,
                event_id VARCHAR(50) NOT NULL,
                leader_email VARCHAR(120) NOT NULL,
                created_at DATETIME NOT NULL,
                is_complete BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS team_members (
                id INT AUTO_INCREMENT PRIMARY KEY,
                team_id INT NOT NULL,
                email VARCHAR(120) NOT NULL,
                status ENUM('pending', 'accepted', 'declined') DEFAULT 'pending',
                invited_at DATETIME NOT NULL,
                responded_at DATETIME DEFAULT NULL,
                FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS invite_cooldowns (
                id INT AUTO_INCREMENT PRIMARY KEY,
                leader_email VARCHAR(120) NOT NULL,
                invitee_email VARCHAR(120) NOT NULL,
                event_id VARCHAR(50) NOT NULL,
                cooldown_until DATETIME NOT NULL
            )
        """)

        # ----- ALTER existing tables for upgrades (safe to run multiple times) -----
        # Add team columns to events if they don't exist
        try:
            cursor.execute("ALTER TABLE events ADD COLUMN team_event BOOLEAN DEFAULT FALSE")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE events ADD COLUMN min_members INT DEFAULT 1")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE events ADD COLUMN max_members INT DEFAULT 1")
        except Exception:
            pass
        # Add team columns to registrations if they don't exist
        try:
            cursor.execute("ALTER TABLE registrations ADD COLUMN is_team_registration BOOLEAN DEFAULT FALSE")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE registrations ADD COLUMN team_id INT DEFAULT NULL")
        except Exception:
            pass
    conn.commit()
    conn.close()

# Initialize tables when app starts
init_db()

# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            flash("Please log in to access the dashboard.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_pending_invite_count(email):
    """Return the number of pending, non-expired invites for a user."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cutoff = datetime.now() - timedelta(seconds=INVITE_EXPIRY_SECONDS)
            cursor.execute("""
                SELECT COUNT(*) AS cnt
                FROM team_members tm
                JOIN teams t ON tm.team_id = t.id
                WHERE tm.email = %s
                  AND tm.status = 'pending'
                  AND tm.invited_at > %s
                  AND t.is_complete = FALSE
            """, (email, cutoff))
            return cursor.fetchone()["cnt"]
    finally:
        conn.close()


def _expire_old_invites(cursor):
    """Mark expired pending invites as 'declined' without triggering cooldown."""
    cutoff = datetime.now() - timedelta(seconds=INVITE_EXPIRY_SECONDS)
    cursor.execute("""
        UPDATE team_members
        SET status = 'declined', responded_at = %s
        WHERE status = 'pending'
          AND invited_at <= %s
    """, (datetime.now(), cutoff))


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Please fill in all fields.", "danger")
            return redirect(url_for("login"))

        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s AND password = %s", (email, password))
            user = cursor.fetchone()
        conn.close()

        if user:
            session["user"] = {
                "name": user["name"],
                "email": user["email"],
                "student_id": user["student_id"],
                "department": user["department"],
            }
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid email or password.", "danger")
            return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if "user" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        student_id = request.form.get("student_id", "").strip()
        department = request.form.get("department", "").strip().upper()

        if not name or not email or not password or not student_id or not department:
            flash("All fields are required.", "danger")
            return redirect(url_for("register"))

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            flash("Please enter a valid email address.", "danger")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return redirect(url_for("register"))

        if department not in ALLOWED_DEPARTMENTS:
            flash("Please select a valid department.", "danger")
            return redirect(url_for("register"))

        # Handle Student ID file upload to S3
        student_id_filename = None
        file = request.files.get("student_id_file")

        if file and file.filename:
            if _allowed_file(file.filename):
                original_fname = secure_filename(file.filename)
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                student_id_filename = f"student_ids/{timestamp}_{original_fname}"

                try:
                    s3_client.upload_fileobj(
                        file,
                        S3_BUCKET_NAME,
                        student_id_filename,
                        ExtraArgs={'ContentType': file.content_type}
                    )
                except ClientError as e:
                    print(f"S3 Upload Error: {e}")
                    flash("There was an error uploading your Student ID. Please try again.", "danger")
                    return redirect(url_for("register"))
            else:
                flash("Invalid file type. Only PDF, PNG, and JPG are allowed.", "danger")
                return redirect(url_for("register"))

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
                if cursor.fetchone():
                    flash("An account with that email already exists.", "danger")
                    return redirect(url_for("register"))

                cursor.execute(
                    "INSERT INTO users (name, email, password, student_id, department, student_id_file) VALUES (%s, %s, %s, %s, %s, %s)",
                    (name, email, password, student_id, department, student_id_filename)
                )
                conn.commit()
        finally:
            conn.close()

        flash("Account created successfully! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", departments=sorted(ALLOWED_DEPARTMENTS))

@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    user_email = session["user"]["email"]

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM events")
        events = cursor.fetchall()

        cursor.execute("SELECT event_id FROM registrations WHERE email = %s", (user_email,))
        user_reg_ids = [row["event_id"] for row in cursor.fetchall()]

        # Check which registrations are team-based (so we can disable Leave)
        cursor.execute(
            "SELECT event_id FROM registrations WHERE email = %s AND is_team_registration = TRUE",
            (user_email,)
        )
        team_reg_ids = [row["event_id"] for row in cursor.fetchall()]
    conn.close()

    for ev in events:
        ev["is_registered"] = ev["id"] in user_reg_ids
        ev["is_team_reg"] = ev["id"] in team_reg_ids

    total_events = len(events)
    registered_count = len(user_reg_ids)
    upcoming_count = total_events - registered_count

    my_events = [ev for ev in events if ev["is_registered"]]
    available_events = [ev for ev in events if not ev["is_registered"]]

    pending_invite_count = _get_pending_invite_count(user_email)

    return render_template(
        "dashboard.html",
        events=events,
        my_events=my_events,
        available_events=available_events,
        total_events=total_events,
        registered_count=registered_count,
        upcoming_count=upcoming_count,
        pending_invite_count=pending_invite_count,
    )

@app.route("/register-event", methods=["POST"])
@login_required
def register_event():
    user_email = session["user"]["email"]
    event_id = request.form.get("event_id", "").strip()

    if not event_id:
        flash("Please select an event.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Check if this is a team event — redirect to team flow
            cursor.execute("SELECT * FROM events WHERE id = %s", (event_id,))
            event = cursor.fetchone()
            if event and event.get("team_event"):
                return redirect(url_for("team_register", event_id=event_id))

            cursor.execute("SELECT * FROM registrations WHERE email = %s AND event_id = %s", (user_email, event_id))
            if cursor.fetchone():
                flash("You are already registered for this event.", "warning")
                return redirect(url_for("dashboard"))

            cursor.execute(
                "INSERT INTO registrations (email, event_id, registered_at) VALUES (%s, %s, %s)",
                (user_email, event_id, datetime.now())
            )
            conn.commit()

        event_title = event["title"] if event else "the event"
        flash(f"Successfully registered for {event_title}!", "success")
        return redirect(url_for("dashboard"))
    finally:
        conn.close()

@app.route("/leave/<event_id>", methods=["POST"])
@login_required
def leave_event(event_id):
    user_email = session["user"]["email"]

    conn = get_db_connection()
    with conn.cursor() as cursor:
        # Prevent leaving team events
        cursor.execute(
            "SELECT * FROM registrations WHERE email = %s AND event_id = %s",
            (user_email, event_id)
        )
        reg_to_delete = cursor.fetchone()

        if not reg_to_delete:
            flash("You are not registered for that event.", "warning")
        elif reg_to_delete.get("is_team_registration"):
            flash("You cannot leave a team event after registration.", "danger")
        else:
            cursor.execute("DELETE FROM registrations WHERE email = %s AND event_id = %s", (user_email, event_id))
            cursor.execute("SELECT title FROM events WHERE id = %s", (event_id,))
            event = cursor.fetchone()
            conn.commit()

            event_title = event["title"] if event else "the event"
            flash(f"You have left {event_title}.", "info")
    conn.close()

    return redirect(url_for("dashboard"))


# ===========================================================================
# TEAM UP — Routes
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. GET /event/<event_id>/team-register — Team registration page
# ---------------------------------------------------------------------------
@app.route("/event/<event_id>/team-register")
@login_required
def team_register(event_id):
    user_email = session["user"]["email"]

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM events WHERE id = %s", (event_id,))
            event = cursor.fetchone()
            if not event:
                flash("Event not found.", "danger")
                return redirect(url_for("dashboard"))
            if not event.get("team_event"):
                flash("This is not a team event.", "warning")
                return redirect(url_for("dashboard"))

            # Check if user is already registered
            cursor.execute(
                "SELECT * FROM registrations WHERE email = %s AND event_id = %s",
                (user_email, event_id)
            )
            if cursor.fetchone():
                flash("You are already registered for this event.", "warning")
                return redirect(url_for("dashboard"))

            # Check if user already has an incomplete team for this event
            cursor.execute(
                "SELECT * FROM teams WHERE leader_email = %s AND event_id = %s AND is_complete = FALSE",
                (user_email, event_id)
            )
            existing_team = cursor.fetchone()

            if existing_team:
                team = existing_team
            else:
                team = None  # Will be created via POST /event/<event_id>/create-team

    finally:
        conn.close()

    pending_invite_count = _get_pending_invite_count(user_email)

    return render_template(
        "team_register.html",
        event=event,
        team=team,
        pending_invite_count=pending_invite_count,
    )


# ---------------------------------------------------------------------------
# 2. POST /event/<event_id>/create-team — Create a new team
# ---------------------------------------------------------------------------
@app.route("/event/<event_id>/create-team", methods=["POST"])
@login_required
def create_team(event_id):
    user_email = session["user"]["email"]

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM events WHERE id = %s", (event_id,))
            event = cursor.fetchone()
            if not event or not event.get("team_event"):
                return jsonify({"error": "Invalid team event."}), 400

            # Check not already registered
            cursor.execute(
                "SELECT * FROM registrations WHERE email = %s AND event_id = %s",
                (user_email, event_id)
            )
            if cursor.fetchone():
                return jsonify({"error": "Already registered for this event."}), 400

            # Check no existing incomplete team
            cursor.execute(
                "SELECT * FROM teams WHERE leader_email = %s AND event_id = %s AND is_complete = FALSE",
                (user_email, event_id)
            )
            if cursor.fetchone():
                return jsonify({"error": "You already have a team for this event."}), 400

            now = datetime.now()
            cursor.execute(
                "INSERT INTO teams (event_id, leader_email, created_at, is_complete) VALUES (%s, %s, %s, FALSE)",
                (event_id, user_email, now)
            )
            conn.commit()
            team_id = cursor.lastrowid

        return jsonify({"team_id": team_id, "message": "Team created successfully."}), 201
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. POST /team/<team_id>/invite — Invite a user to the team
# ---------------------------------------------------------------------------
@app.route("/team/<int:team_id>/invite", methods=["POST"])
@login_required
def invite_to_team(team_id):
    user_email = session["user"]["email"]
    data = request.get_json() or {}
    invitee_email = data.get("email", "").strip().lower()

    if not invitee_email:
        return jsonify({"error": "Invitee email is required."}), 400

    if invitee_email == user_email:
        return jsonify({"error": "You cannot invite yourself."}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Expire old invites first
            _expire_old_invites(cursor)

            # Validate team ownership
            cursor.execute("SELECT * FROM teams WHERE id = %s AND leader_email = %s", (team_id, user_email))
            team = cursor.fetchone()
            if not team:
                return jsonify({"error": "Team not found or you are not the leader."}), 404
            if team["is_complete"]:
                return jsonify({"error": "Team is already confirmed."}), 400

            event_id = team["event_id"]

            # Check invitee exists
            cursor.execute("SELECT * FROM users WHERE email = %s", (invitee_email,))
            invitee = cursor.fetchone()
            if not invitee:
                return jsonify({"error": "User not found."}), 404

            # Check invitee is not already registered for this event
            cursor.execute(
                "SELECT * FROM registrations WHERE email = %s AND event_id = %s",
                (invitee_email, event_id)
            )
            if cursor.fetchone():
                return jsonify({"error": "This user is already registered for the event."}), 400

            # Check invitee doesn't already have a pending/accepted invite for this team
            cursor.execute(
                "SELECT * FROM team_members WHERE team_id = %s AND email = %s AND status IN ('pending', 'accepted')",
                (team_id, invitee_email)
            )
            if cursor.fetchone():
                return jsonify({"error": "User already has an active invite for this team."}), 400

            # Check invitee hasn't accepted another team for the same event
            cursor.execute("""
                SELECT tm.* FROM team_members tm
                JOIN teams t ON tm.team_id = t.id
                WHERE tm.email = %s AND t.event_id = %s AND tm.status = 'accepted'
            """, (invitee_email, event_id))
            if cursor.fetchone():
                return jsonify({"error": "User has already accepted another team for this event."}), 400

            # Check max members not exceeded
            cursor.execute("SELECT * FROM events WHERE id = %s", (event_id,))
            event = cursor.fetchone()
            max_members = event["max_members"]

            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM team_members WHERE team_id = %s AND status IN ('pending', 'accepted')",
                (team_id,)
            )
            current_count = cursor.fetchone()["cnt"]
            if current_count >= max_members - 1:  # -1 because leader is slot 1
                return jsonify({"error": "Team is already at maximum capacity."}), 400

            # Check decline cooldown
            now = datetime.now()
            cursor.execute("""
                SELECT * FROM invite_cooldowns
                WHERE leader_email = %s AND invitee_email = %s AND event_id = %s
                  AND cooldown_until > %s
            """, (user_email, invitee_email, event_id, now))
            cooldown = cursor.fetchone()
            if cooldown:
                remaining = (cooldown["cooldown_until"] - now).total_seconds()
                return jsonify({
                    "error": f"Cooldown active. You can re-invite this user in {int(remaining)} seconds.",
                    "cooldown_remaining": int(remaining)
                }), 429

            # All checks pass — create invite
            cursor.execute(
                "INSERT INTO team_members (team_id, email, status, invited_at) VALUES (%s, %s, 'pending', %s)",
                (team_id, invitee_email, now)
            )
            conn.commit()
            invite_id = cursor.lastrowid

        return jsonify({
            "invite_id": invite_id,
            "message": f"Invite sent to {invitee_email}.",
            "expires_at": (now + timedelta(seconds=INVITE_EXPIRY_SECONDS)).isoformat()
        }), 201
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. GET /team/<team_id>/status — Team status (polled by leader)
# ---------------------------------------------------------------------------
@app.route("/team/<int:team_id>/status")
@login_required
def team_status(team_id):
    user_email = session["user"]["email"]

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            _expire_old_invites(cursor)
            conn.commit()

            cursor.execute("SELECT * FROM teams WHERE id = %s", (team_id,))
            team = cursor.fetchone()
            if not team:
                return jsonify({"error": "Team not found."}), 404

            event_id = team["event_id"]
            cursor.execute("SELECT * FROM events WHERE id = %s", (event_id,))
            event = cursor.fetchone()

            cursor.execute("""
                SELECT tm.id, tm.email, tm.status, tm.invited_at, tm.responded_at,
                       u.name, u.student_id, u.department
                FROM team_members tm
                LEFT JOIN users u ON tm.email = u.email
                WHERE tm.team_id = %s
                ORDER BY tm.invited_at ASC
            """, (team_id,))
            members = cursor.fetchall()

            # Compute expiry info
            now = datetime.now()
            for m in members:
                if m["status"] == "pending":
                    expires_at = m["invited_at"] + timedelta(seconds=INVITE_EXPIRY_SECONDS)
                    m["expires_at"] = expires_at.isoformat()
                    m["seconds_remaining"] = max(0, int((expires_at - now).total_seconds()))
                else:
                    m["expires_at"] = None
                    m["seconds_remaining"] = 0
                # Convert datetimes to ISO strings for JSON
                m["invited_at"] = m["invited_at"].isoformat() if m["invited_at"] else None
                m["responded_at"] = m["responded_at"].isoformat() if m["responded_at"] else None

            accepted_count = sum(1 for m in members if m["status"] == "accepted")
            min_met = accepted_count >= (event["min_members"] - 1)

        return jsonify({
            "team_id": team_id,
            "event_id": event_id,
            "leader_email": team["leader_email"],
            "is_complete": team["is_complete"],
            "members": members,
            "accepted_count": accepted_count,
            "min_members": event["min_members"],
            "max_members": event["max_members"],
            "min_met": min_met,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. POST /team/<team_id>/confirm — Confirm team registration
# ---------------------------------------------------------------------------
@app.route("/team/<int:team_id>/confirm", methods=["POST"])
@login_required
def confirm_team(team_id):
    user_email = session["user"]["email"]

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            _expire_old_invites(cursor)

            cursor.execute("SELECT * FROM teams WHERE id = %s AND leader_email = %s", (team_id, user_email))
            team = cursor.fetchone()
            if not team:
                return jsonify({"error": "Team not found or you are not the leader."}), 404
            if team["is_complete"]:
                return jsonify({"error": "Team is already confirmed."}), 400

            event_id = team["event_id"]
            cursor.execute("SELECT * FROM events WHERE id = %s", (event_id,))
            event = cursor.fetchone()

            # Count accepted members
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM team_members WHERE team_id = %s AND status = 'accepted'",
                (team_id,)
            )
            accepted_count = cursor.fetchone()["cnt"]

            if accepted_count < event["min_members"] - 1:
                return jsonify({
                    "error": f"Need at least {event['min_members'] - 1} accepted members. Currently have {accepted_count}."
                }), 400

            # Mark team as complete
            now = datetime.now()
            cursor.execute("UPDATE teams SET is_complete = TRUE WHERE id = %s", (team_id,))

            # Register the leader
            cursor.execute(
                "INSERT INTO registrations (email, event_id, registered_at, is_team_registration, team_id) VALUES (%s, %s, %s, TRUE, %s)",
                (user_email, event_id, now, team_id)
            )

            # Register all accepted members
            cursor.execute(
                "SELECT email FROM team_members WHERE team_id = %s AND status = 'accepted'",
                (team_id,)
            )
            accepted_members = cursor.fetchall()
            for member in accepted_members:
                cursor.execute(
                    "INSERT INTO registrations (email, event_id, registered_at, is_team_registration, team_id) VALUES (%s, %s, %s, TRUE, %s)",
                    (member["email"], event_id, now, team_id)
                )

            # Dismiss remaining pending invites
            cursor.execute(
                "UPDATE team_members SET status = 'declined', responded_at = %s WHERE team_id = %s AND status = 'pending'",
                (now, team_id)
            )

            conn.commit()

        return jsonify({"message": "Team registration confirmed!", "redirect": url_for("dashboard")})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. POST /team/<team_id>/cancel — Cancel (delete) incomplete team
# ---------------------------------------------------------------------------
@app.route("/team/<int:team_id>/cancel", methods=["POST"])
@login_required
def cancel_team(team_id):
    user_email = session["user"]["email"]

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM teams WHERE id = %s AND leader_email = %s", (team_id, user_email))
            team = cursor.fetchone()
            if not team:
                return jsonify({"error": "Team not found or you are not the leader."}), 404
            if team["is_complete"]:
                return jsonify({"error": "Cannot cancel a confirmed team."}), 400

            # Delete members first (FK cascade should handle it, but be explicit)
            cursor.execute("DELETE FROM team_members WHERE team_id = %s", (team_id,))
            cursor.execute("DELETE FROM teams WHERE id = %s", (team_id,))
            conn.commit()

        return jsonify({"message": "Team cancelled.", "redirect": url_for("dashboard")})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. GET /invites/pending — List pending invites for the logged-in user
# ---------------------------------------------------------------------------
@app.route("/invites/pending")
@login_required
def pending_invites():
    user_email = session["user"]["email"]

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            _expire_old_invites(cursor)
            conn.commit()

            cutoff = datetime.now() - timedelta(seconds=INVITE_EXPIRY_SECONDS)
            now = datetime.now()

            cursor.execute("""
                SELECT tm.id, tm.team_id, tm.status, tm.invited_at,
                       t.event_id, t.leader_email, t.is_complete,
                       e.title AS event_title, e.date AS event_date,
                       e.location AS event_location, e.category AS event_category,
                       u.name AS leader_name
                FROM team_members tm
                JOIN teams t ON tm.team_id = t.id
                JOIN events e ON t.event_id = e.id
                LEFT JOIN users u ON t.leader_email = u.email
                WHERE tm.email = %s
                  AND tm.status = 'pending'
                  AND tm.invited_at > %s
                  AND t.is_complete = FALSE
                ORDER BY tm.invited_at DESC
            """, (user_email, cutoff))
            invites = cursor.fetchall()

            for inv in invites:
                expires_at = inv["invited_at"] + timedelta(seconds=INVITE_EXPIRY_SECONDS)
                inv["expires_at"] = expires_at.isoformat()
                inv["seconds_remaining"] = max(0, int((expires_at - now).total_seconds()))
                inv["invited_at"] = inv["invited_at"].isoformat() if inv["invited_at"] else None

    finally:
        conn.close()

    # Return JSON if requested via AJAX, otherwise render template
    if request.headers.get("Accept", "").startswith("application/json") or request.args.get("format") == "json":
        return jsonify({"invites": invites, "count": len(invites)})

    pending_invite_count = len(invites)
    return render_template("invitations.html", invites=invites, pending_invite_count=pending_invite_count)


# ---------------------------------------------------------------------------
# 8. POST /invite/<invite_id>/respond — Accept or decline an invite
# ---------------------------------------------------------------------------
@app.route("/invite/<int:invite_id>/respond", methods=["POST"])
@login_required
def respond_to_invite(invite_id):
    user_email = session["user"]["email"]
    data = request.get_json() or {}
    response_action = data.get("response", "").strip().lower()

    if response_action not in ("accepted", "declined"):
        return jsonify({"error": "Response must be 'accepted' or 'declined'."}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            _expire_old_invites(cursor)

            cursor.execute(
                "SELECT * FROM team_members WHERE id = %s AND email = %s",
                (invite_id, user_email)
            )
            invite = cursor.fetchone()
            if not invite:
                return jsonify({"error": "Invite not found."}), 404
            if invite["status"] != "pending":
                return jsonify({"error": "This invite is no longer pending."}), 400

            # Check if expired
            expires_at = invite["invited_at"] + timedelta(seconds=INVITE_EXPIRY_SECONDS)
            now = datetime.now()
            if now > expires_at:
                cursor.execute(
                    "UPDATE team_members SET status = 'declined', responded_at = %s WHERE id = %s",
                    (now, invite_id)
                )
                conn.commit()
                return jsonify({"error": "This invite has expired."}), 400

            team_id = invite["team_id"]

            # Get the team to find event_id
            cursor.execute("SELECT * FROM teams WHERE id = %s", (team_id,))
            team = cursor.fetchone()
            if not team or team["is_complete"]:
                return jsonify({"error": "Team is no longer available."}), 400

            event_id = team["event_id"]

            if response_action == "accepted":
                # Check user hasn't already accepted another team for same event
                cursor.execute("""
                    SELECT tm.* FROM team_members tm
                    JOIN teams t ON tm.team_id = t.id
                    WHERE tm.email = %s AND t.event_id = %s AND tm.status = 'accepted'
                      AND tm.id != %s
                """, (user_email, event_id, invite_id))
                if cursor.fetchone():
                    return jsonify({"error": "You have already accepted another team for this event."}), 400

                # Accept this invite
                cursor.execute(
                    "UPDATE team_members SET status = 'accepted', responded_at = %s WHERE id = %s",
                    (now, invite_id)
                )

                # Silently mark other pending invites for the SAME event as non-actionable (declined)
                cursor.execute("""
                    UPDATE team_members tm
                    JOIN teams t ON tm.team_id = t.id
                    SET tm.status = 'declined', tm.responded_at = %s
                    WHERE tm.email = %s
                      AND t.event_id = %s
                      AND tm.status = 'pending'
                      AND tm.id != %s
                """, (now, user_email, event_id, invite_id))

            elif response_action == "declined":
                cursor.execute(
                    "UPDATE team_members SET status = 'declined', responded_at = %s WHERE id = %s",
                    (now, invite_id)
                )

                # Set 2-minute cooldown for the leader to re-invite this user
                leader_email = team["leader_email"]
                cooldown_until = now + timedelta(seconds=DECLINE_COOLDOWN_SECONDS)
                cursor.execute(
                    "INSERT INTO invite_cooldowns (leader_email, invitee_email, event_id, cooldown_until) VALUES (%s, %s, %s, %s)",
                    (leader_email, user_email, event_id, cooldown_until)
                )

            conn.commit()

        return jsonify({"message": f"Invite {response_action}.", "status": response_action})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 9. GET /users/search — Search users by name or student ID
# ---------------------------------------------------------------------------
@app.route("/users/search")
@login_required
def search_users():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify({"users": []})

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            search_term = f"%{query}%"
            cursor.execute("""
                SELECT name, email, student_id, department
                FROM users
                WHERE (name LIKE %s OR student_id LIKE %s)
                  AND email != %s
                ORDER BY name ASC
                LIMIT 10
            """, (search_term, search_term, session["user"]["email"]))
            users = cursor.fetchall()

        return jsonify({"users": users})
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True, port=5000)