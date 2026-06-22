import os
import re
from datetime import datetime
from functools import wraps

import boto3
import pymysql
from botocore.exceptions import ClientError
from flask import Flask, flash, redirect, render_template, request, session, url_for
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
                password VARCHAR(255) NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id VARCHAR(50) PRIMARY KEY,
                title VARCHAR(150) NOT NULL,
                description TEXT,
                date VARCHAR(50)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS registrations (
                id INT AUTO_INCREMENT PRIMARY KEY,
                email VARCHAR(120) NOT NULL,
                event_id VARCHAR(50) NOT NULL,
                registered_at DATETIME NOT NULL,
                file_uploaded VARCHAR(255)
            )
        """)
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
            session["user"] = {"name": user["name"], "email": user["email"]}
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

        if not name or not email or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for("register"))

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            flash("Please enter a valid email address.", "danger")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return redirect(url_for("register"))

        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                flash("An account with that email already exists.", "danger")
                conn.close()
                return redirect(url_for("register"))

            cursor.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)", (name, email, password))
            conn.commit()
        conn.close()
        
        flash("Account created successfully! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")

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
    conn.close()

    for ev in events:
        ev["is_registered"] = ev["id"] in user_reg_ids

    total_events = len(events)
    registered_count = len(user_reg_ids)
    upcoming_count = total_events - registered_count

    my_events = [ev for ev in events if ev["is_registered"]]
    available_events = [ev for ev in events if not ev["is_registered"]]

    return render_template(
        "dashboard.html",
        events=events,
        my_events=my_events,
        available_events=available_events,
        total_events=total_events,
        registered_count=registered_count,
        upcoming_count=upcoming_count,
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
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM registrations WHERE email = %s AND event_id = %s", (user_email, event_id))
        if cursor.fetchone():
            flash("You are already registered for this event.", "warning")
            conn.close()
            return redirect(url_for("dashboard"))

    file_name = None
    file = request.files.get("student_id")
    
    if file and file.filename:
        if _allowed_file(file.filename):
            original_fname = secure_filename(file.filename)
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            file_name = f"{timestamp}_{original_fname}"
            
            try:
                s3_client.upload_fileobj(
                    file,
                    S3_BUCKET_NAME,
                    file_name,
                    ExtraArgs={'ContentType': file.content_type}
                )
            except ClientError as e:
                print(f"S3 Upload Error: {e}")
                flash("There was an error uploading your file. Please try again.", "danger")
                return redirect(url_for("dashboard"))
        else:
            flash("Invalid file type. Only PDF, PNG, and JPG are allowed.", "danger")
            return redirect(url_for("dashboard"))

    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO registrations (email, event_id, registered_at, file_uploaded) VALUES (%s, %s, %s, %s)",
            (user_email, event_id, datetime.now(), file_name)
        )
        cursor.execute("SELECT title FROM events WHERE id = %s", (event_id,))
        event = cursor.fetchone()
        conn.commit()
    conn.close()

    event_title = event["title"] if event else "the event"
    flash(f"Successfully registered for {event_title}!", "success")
    return redirect(url_for("dashboard"))

@app.route("/leave/<event_id>", methods=["POST"])
@login_required
def leave_event(event_id):
    user_email = session["user"]["email"]
    
    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM registrations WHERE email = %s AND event_id = %s", (user_email, event_id))
        reg_to_delete = cursor.fetchone()

        if not reg_to_delete:
            flash("You are not registered for that event.", "warning")
        else:
            cursor.execute("DELETE FROM registrations WHERE email = %s AND event_id = %s", (user_email, event_id))
            cursor.execute("SELECT title FROM events WHERE id = %s", (event_id,))
            event = cursor.fetchone()
            conn.commit()
            
            event_title = event["title"] if event else "the event"
            flash(f"You have left {event_title}.", "info")
    conn.close()

    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    app.run(debug=True, port=5000)