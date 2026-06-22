import os
import re
from datetime import datetime
from functools import wraps

import boto3
from botocore.exceptions import ClientError
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# App setup & AWS Configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = "eventhub-secret-key-change-in-production"

# AWS RDS Configuration
# Format: mysql+pymysql://<username>:<password>@<rds-endpoint>:<port>/<dbname>
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://admin:yourpassword@your-rds-endpoint.amazonaws.com:3306/eventhub_db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# AWS S3 Configuration
S3_BUCKET_NAME = "your-s3-bucket-name"
s3_client = boto3.client('s3')

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ---------------------------------------------------------------------------
# Database Models
# ---------------------------------------------------------------------------
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

class Event(db.Model):
    __tablename__ = 'events'
    id = db.Column(db.String(50), primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    date = db.Column(db.String(50), nullable=True)

class Registration(db.Model):
    __tablename__ = 'registrations'
    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(120), db.ForeignKey('users.email'), nullable=False)
    event_id = db.Column(db.String(50), db.ForeignKey('events.id'), nullable=False)
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)
    file_uploaded = db.Column(db.String(255), nullable=True) # Stores the S3 Object Key

# Create tables if they don't exist
with app.app_context():
    db.create_all()

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

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            session["user"] = {"name": user.name, "email": user.email}
            flash(f"Welcome back, {user.name}!", "success")
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

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("An account with that email already exists.", "danger")
            return redirect(url_for("register"))

        hashed_pw = generate_password_hash(password)
        new_user = User(name=name, email=email, password_hash=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        
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
    
    events = Event.query.all()
    user_regs = Registration.query.filter_by(user_email=user_email).all()
    user_reg_ids = [r.event_id for r in user_regs]

    event_list = []
    for ev in events:
        event_list.append({
            "id": ev.id,
            "title": ev.title,
            "description": ev.description,
            "date": ev.date,
            "is_registered": ev.id in user_reg_ids
        })

    total_events = len(event_list)
    registered_count = len(user_reg_ids)
    upcoming_count = total_events - registered_count

    my_events = [ev for ev in event_list if ev["is_registered"]]
    available_events = [ev for ev in event_list if not ev["is_registered"]]

    return render_template(
        "dashboard.html",
        events=event_list,
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

    existing_reg = Registration.query.filter_by(user_email=user_email, event_id=event_id).first()
    if existing_reg:
        flash("You are already registered for this event.", "warning")
        return redirect(url_for("dashboard"))

    file_name = None
    file = request.files.get("student_id")
    
    # AWS S3 Upload Logic
    if file and file.filename:
        if _allowed_file(file.filename):
            original_fname = secure_filename(file.filename)
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            file_name = f"{timestamp}_{original_fname}"
            
            try:
                # Upload directly to S3 Bucket
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

    # Save to RDS
    new_reg = Registration(user_email=user_email, event_id=event_id, file_uploaded=file_name)
    db.session.add(new_reg)
    db.session.commit()

    event = Event.query.get(event_id)
    event_title = event.title if event else "the event"
    flash(f"Successfully registered for {event_title}!", "success")
    return redirect(url_for("dashboard"))

@app.route("/leave/<event_id>", methods=["POST"])
@login_required
def leave_event(event_id):
    user_email = session["user"]["email"]
    
    reg_to_delete = Registration.query.filter_by(user_email=user_email, event_id=event_id).first()

    if not reg_to_delete:
        flash("You are not registered for that event.", "warning")
    else:
        # Optional: You could also delete the file from S3 here if you want to save storage space
        # if reg_to_delete.file_uploaded:
        #     s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=reg_to_delete.file_uploaded)

        db.session.delete(reg_to_delete)
        db.session.commit()
        
        event = Event.query.get(event_id)
        event_title = event.title if event else "the event"
        flash(f"You have left {event_title}.", "info")

    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    app.run(debug=True, port=5000)