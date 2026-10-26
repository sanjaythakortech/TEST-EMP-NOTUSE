import os
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, inspect, select, text
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Static assets live under /public so Vercel's CDN can serve them directly
# (per Vercel's Flask guidance, app.static_folder is not used in production).
# Flask still serves the same folder locally so `python app.py` keeps working.
app = Flask(__name__, static_folder=str(BASE_DIR / "public" / "static"), static_url_path="/static")

# Vercel terminates HTTPS in front of the function; trust its forwarded headers
# so e-mail links are generated as https://your-domain/... and not http://.
if os.getenv("VERCEL", "").lower() == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Collected at import time instead of raised, so a missing/incorrect
# environment variable shows a clear in-app setup page instead of a bare
# Vercel "FUNCTION_INVOCATION_FAILED" crash on every request.
CONFIG_ERRORS = []

# Vercel/serverless deployments must use a persistent PostgreSQL database.
# Local development can continue to use SQLite when DATABASE_URL is not set.
is_vercel = os.getenv("VERCEL", "").lower() == "1"
secret_key = os.getenv("SECRET_KEY", "").strip()
if is_vercel and not secret_key:
    CONFIG_ERRORS.append("SECRET_KEY is not set. Add it in Vercel → Project → Settings → Environment Variables.")
app.config["SECRET_KEY"] = secret_key or "local-development-only-change-me"

db_url = os.getenv("DATABASE_URL", "").strip()
if not db_url:
    if is_vercel:
        CONFIG_ERRORS.append("DATABASE_URL is not set. Add a PostgreSQL connection string in Vercel → Project → Settings → Environment Variables.")
        db_url = "sqlite:///:memory:"  # placeholder only, so the app can boot and show the setup page
    else:
        db_url = f"sqlite:///{BASE_DIR / 'instance' / 'office_tasks.db'}"

# SQLAlchemy's PostgreSQL default driver is psycopg2, while this project
# installs psycopg (v3). Normalize common hosted-Postgres URLs accordingly.
if db_url.startswith("postgres://"):
    db_url = "postgresql+psycopg://" + db_url[len("postgres://"):]
elif db_url.startswith("postgresql://"):
    db_url = "postgresql+psycopg://" + db_url[len("postgresql://"):]

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Serverless functions open fresh connections on every cold start. Hosted
# Postgres providers (Neon, Supabase, Vercel Postgres) close idle connections
# after a few minutes, which otherwise surfaces as random 500s. pool_pre_ping
# detects a dead connection before use, pool_recycle avoids reusing an old
# one, and a small pool avoids exhausting the provider's connection limit.
is_postgres = db_url.startswith("postgresql")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    # pool_size/max_overflow only apply to QueuePool-based dialects
    # (Postgres); SQLite's pool classes reject these keyword arguments.
    **({"pool_size": 1, "max_overflow": 2} if is_vercel and is_postgres else {}),
}
app.config["WTF_CSRF_TIME_LIMIT"] = None
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = is_vercel

# Extensions
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "auth_login"
login_manager.login_message = "Please sign in to continue."


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(40), unique=True, nullable=True, index=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(30), default="")
    designation = db.Column(db.String(100), default="Employee")
    password_hash = db.Column(db.String(255), nullable=False)
    company = db.Column(db.String(160), default="")
    # role: "admin" (platform owner) | "manager" (self sign-up, owns a team) | "employee"
    role = db.Column(db.String(20), nullable=False, default="employee")
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    # Managers only: "pending" (waiting for admin) | "approved" | "rejected". Everyone else is "approved".
    approval = db.Column(db.String(20), default="approved", nullable=False, index=True)
    last_email_at = db.Column(db.DateTime, nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    manager = db.relationship("User", remote_side=[id], backref=db.backref("team", lazy=True))
    tasks_assigned = db.relationship("Task", foreign_keys="Task.assigned_to_id", backref="assignee", lazy=True)
    tasks_created = db.relationship("Task", foreign_keys="Task.created_by_id", backref="creator", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    task_code = db.Column(db.String(30), unique=True, index=True)
    title = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, default="")
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    start_date = db.Column(db.Date, nullable=True)
    due_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(30), default="Pending", nullable=False, index=True)
    priority = db.Column(db.String(30), default="Medium", nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TaskHistory(db.Model):
    __tablename__ = "task_history"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.String(500), default="")
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    task = db.relationship("Task", backref=db.backref("history", lazy=True, cascade="all, delete-orphan"))
    changed_by = db.relationship("User", foreign_keys=[changed_by_id])


class AppSetting(db.Model):
    """Small key/value table so the admin can change switches live (Vercel env vars cannot)."""
    __tablename__ = "app_settings"
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(200), nullable=False, default="")


SETTING_DEFAULTS = {"signup_open": "1", "require_approval": "1"}


def get_setting(key):
    try:
        row = db.session.get(AppSetting, key)
    except Exception:
        db.session.rollback()
        row = None
    return row.value if row else SETTING_DEFAULTS.get(key, "")


def setting_on(key):
    return get_setting(key) == "1"


def set_setting(key, value):
    row = db.session.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.session.add(AppSetting(key=key, value=value))
    db.session.commit()


app.jinja_env.globals["setting_on"] = setting_on


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def account_problem(user):
    """Return a message if this account may not use the app right now, else None."""
    if not user.active:
        return "This account is inactive. Contact your administrator.", "danger"
    if user.role == "manager":
        if user.approval == "pending":
            return "Your account is waiting for admin approval. We will email you as soon as it is approved.", "warning"
        if user.approval == "rejected":
            return "Your sign-up request was not approved. Please contact the administrator.", "danger"
    if user.role == "employee" and user.manager is not None:
        if not user.manager.active or user.manager.approval != "approved":
            return "Your manager's account is inactive. Please contact your manager.", "danger"
    return None


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            problem = account_problem(current_user)
            if problem:
                logout_user()
                flash(problem[0], problem[1])
                return redirect(url_for("auth_login"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


# ---------------- TEAM SCOPING (multi-tenant isolation) ----------------
def team_query():
    """Employees that belong to the logged-in manager."""
    return User.query.filter_by(role="employee", manager_id=current_user.id)


def team_ids_select():
    return select(User.id).where(User.role == "employee", User.manager_id == current_user.id)


def scoped_tasks():
    """Only tasks assigned to the logged-in manager's own employees."""
    return Task.query.filter(Task.assigned_to_id.in_(team_ids_select()))


def get_team_task(task_id):
    task = db.session.get(Task, task_id)
    if not task or not task.assignee or task.assignee.manager_id != current_user.id:
        abort(404)
    return task


def get_team_employee(employee_id):
    emp = db.session.get(User, employee_id)
    if not emp or emp.role != "employee" or emp.manager_id != current_user.id:
        abort(404)
    return emp


def active_employees():
    return team_query().filter_by(active=True).order_by(User.name.asc()).all()


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def make_task_code(task):
    return f"TSK-{1000 + task.id}"


def make_employee_id(user):
    prefix = {"admin": "ADM", "manager": "MGR"}.get(user.role, "EMP")
    return f"{prefix}-{1000 + user.id}"


def assign_employee_id(user):
    """Give the user a unique auto ID (never collides with manually typed ones)."""
    base = make_employee_id(user)
    candidate, n = base, 1
    while User.query.filter(User.id != user.id, db.func.lower(User.employee_id) == candidate.lower()).first():
        n += 1
        candidate = f"{base}-{n}"
    user.employee_id = candidate


def log_history(task, action, details=""):
    db.session.add(TaskHistory(task_id=task.id, action=action, details=details, changed_by_id=current_user.id))


def ensure_schema():
    """Create tables and safely add newer columns to older SQLite/Postgres databases."""
    db.create_all()
    inspector = inspect(db.engine)
    user_columns = {c["name"] for c in inspector.get_columns("users")}
    new_columns = [
        ("employee_id", "VARCHAR(40)"),
        ("company", "VARCHAR(160)"),
        ("manager_id", "INTEGER REFERENCES users(id)"),
        ("email_verified", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("approval", "VARCHAR(20) NOT NULL DEFAULT 'approved'"),
        ("last_email_at", "TIMESTAMP"),
    ]
    for name, ddl in new_columns:
        if name in user_columns:
            continue
        try:
            with db.engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
                if name == "email_verified":
                    # Accounts that existed before e-mail verification was introduced stay usable.
                    conn.execute(text("UPDATE users SET email_verified = TRUE"))
            db.session.expire_all()
        except Exception:
            # Two serverless cold starts can race to add the same column.
            db.session.rollback()
    users = User.query.filter((User.employee_id.is_(None)) | (User.employee_id == "")).all()
    changed = False
    for user in users:
        assign_employee_id(user)
        changed = True
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def bootstrap_admin():
    """Create the first admin from environment variables (useful for Vercel)."""
    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        return
    if len(password) < 6:
        raise RuntimeError("ADMIN_PASSWORD must be at least 6 characters.")

    existing = User.query.filter(db.func.lower(User.email) == email).first()
    if existing:
        return

    employee_id = os.getenv("ADMIN_EMPLOYEE_ID", "ADM-1000").strip() or "ADM-1000"
    if User.query.filter_by(employee_id=employee_id).first():
        employee_id = None

    admin = User(
        employee_id=employee_id,
        name=os.getenv("ADMIN_NAME", "Administrator").strip() or "Administrator",
        email=email,
        designation="Administrator",
        role="admin",
        email_verified=True,
        active=True,
    )
    admin.set_password(password)
    db.session.add(admin)
    try:
        db.session.commit()
    except Exception:
        # Another cold start may have created the same admin a moment ago.
        db.session.rollback()


@app.before_request
def block_until_configured():
    """Show a clear setup page instead of a bare 500 when required
    environment variables are missing or startup failed."""
    if CONFIG_ERRORS and request.endpoint not in ("static", "health"):
        return render_template("config_error.html", errors=CONFIG_ERRORS), 503


@app.context_processor
def inject_globals():
    pending_managers = 0
    if current_user.is_authenticated and current_user.role == "admin":
        try:
            pending_managers = User.query.filter_by(role="manager", approval="pending").count()
        except Exception:
            db.session.rollback()
    return {
        "pending_managers": pending_managers,
        "now": datetime.utcnow(),
        "status_options": ["Pending", "In Progress", "Completed"],
        "priority_options": ["Low", "Medium", "High", "Urgent"],
    }


# ---------------- E-MAIL (SMTP) + SIGNED LINKS ----------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TOKEN_MAX_AGE = {"verify": 3 * 86400, "invite": 7 * 86400, "reset": 3600}
RESEND_COOLDOWN_SECONDS = 60


def email_configured():
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip())


def email_ready():
    """On Vercel real SMTP is required. Locally, mails are printed to the console."""
    return email_configured() or not is_vercel


def send_email(to, subject, text_body, html_body=None):
    if not email_configured():
        if is_vercel:
            return False
        print(f"\n=== DEV E-MAIL (SMTP not configured) ===\nTo: {to}\nSubject: {subject}\n{text_body}\n========================================\n", flush=True)
        return True
    host = os.getenv("SMTP_HOST").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM").strip()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=12, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=12)
            server.starttls(context=ssl.create_default_context())
        with server:
            if user:
                server.login(user, password)
            server.send_message(msg)
        return True
    except Exception as exc:  # network / auth problems must not crash the request
        app.logger.error("E-mail sending failed: %s", exc)
        return False


def _serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="officeflow-mail-link")


def make_token(user, kind):
    return _serializer().dumps({"u": user.id, "k": kind, "e": user.email, "p": (user.password_hash or "")[-12:]})


def read_token(token, *kinds):
    """Return (user, kind) for a valid, unexpired and unused link, else (None, None)."""
    for kind in kinds:
        try:
            data = _serializer().loads(token, max_age=TOKEN_MAX_AGE[kind])
        except (BadSignature, SignatureExpired):
            continue
        if data.get("k") != kind:
            continue
        user = db.session.get(User, data.get("u"))
        if not user or user.email != data.get("e"):
            continue
        # Invite / reset links die as soon as the password changes (single use).
        if kind in ("invite", "reset") and (user.password_hash or "")[-12:] != data.get("p"):
            continue
        return user, kind
    return None, None


def abs_url(endpoint, **values):
    base = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    if base:
        return base + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)


def cooldown_left(user):
    if not user.last_email_at:
        return 0
    left = RESEND_COOLDOWN_SECONDS - (datetime.utcnow() - user.last_email_at).total_seconds()
    return max(0, int(left))


def _mail_html(title, intro, button, link):
    return (f'<div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px;border:1px solid #e3e9f2;border-radius:14px">'
            f'<h2 style="color:#0f2147;margin:0 0 12px">{title}</h2><p style="color:#44546a;line-height:1.6">{intro}</p>'
            f'<p style="margin:22px 0"><a href="{link}" style="background:#2d6cdf;color:#fff;text-decoration:none;padding:12px 20px;border-radius:10px;font-weight:bold">{button}</a></p>'
            f'<p style="color:#7b8aa0;font-size:12px">Button not working? Copy this link:<br>{link}</p></div>')


def send_link_mail(user, kind):
    """Send a verification / invite / reset mail. Returns (sent_ok, link)."""
    token = make_token(user, kind)
    if kind == "verify":
        link = abs_url("verify_email", token=token)
        subject, title, intro, button = ("Verify your OfficeFlow email", "Verify your email",
            f"Hi {user.name}, confirm this email address to activate your manager account. The link is valid for 3 days.", "Verify email")
    elif kind == "invite":
        link = abs_url("set_password", token=token)
        inviter = user.manager.name if user.manager else "Your manager"
        subject, title, intro, button = ("You have been added to OfficeFlow", "Welcome to OfficeFlow",
            f"Hi {user.name}, {inviter} added you to OfficeFlow. Set your password to activate your account (this also verifies your email). The link is valid for 7 days.", "Set my password")
    else:
        link = abs_url("set_password", token=token)
        subject, title, intro, button = ("Reset your OfficeFlow password", "Reset your password",
            f"Hi {user.name}, use the button below to choose a new password. The link is valid for 1 hour. If you did not ask for this, ignore this email.", "Reset password")
    ok = send_email(user.email, subject, f"{intro}\n\n{link}\n", _mail_html(title, intro, button, link))
    if ok:
        user.last_email_at = datetime.utcnow()
        db.session.commit()
    return ok, link


def notify_admins_new_manager(manager):
    """Tell every admin that a verified manager is waiting for approval (best effort)."""
    link = abs_url("platform_manager_detail", manager_id=manager.id)
    who = f"{manager.name} ({manager.email})" + (f" from {manager.company}" if manager.company else "")
    intro = f"{who} verified their email and is waiting for your approval."
    for admin in User.query.filter_by(role="admin", active=True).all():
        send_email(admin.email, "New manager waiting for approval", f"{intro}\n\n{link}\n",
                   _mail_html("New manager request", intro, "Review request", link))


def notify_manager_decision(manager, approved):
    link = abs_url("auth_login")
    if approved:
        subject, intro, button = ("Your OfficeFlow account is approved", f"Hi {manager.name}, your manager account has been approved. You can sign in and start adding your employees.", "Sign in")
    else:
        subject, intro, button = ("Your OfficeFlow sign-up was not approved", f"Hi {manager.name}, your manager sign-up request was not approved. Please contact the administrator if you think this is a mistake.", "Open OfficeFlow")
    send_email(manager.email, subject, f"{intro}\n\n{link}\n", _mail_html(subject, intro, button, link))


# ---------------- AUTH ----------------
@app.route("/")
def home():
    if current_user.is_authenticated:
        if current_user.role == "admin":
            return redirect(url_for("platform_dashboard"))
        if current_user.role == "manager":
            return redirect(url_for("manager_dashboard"))
        return redirect(url_for("employee_dashboard"))
    return redirect(url_for("auth_login"))


@app.route("/login", methods=["GET", "POST"])
def auth_login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        if not identifier or not password:
            flash("Enter your login ID and password.", "danger")
            return render_template("login.html")
        normalized = identifier.lower()
        # Names are not unique across managers, so sign-in is by email or Employee ID only.
        user = User.query.filter(
            db.or_(db.func.lower(User.email) == normalized, db.func.lower(User.employee_id) == normalized)
        ).first()
        if user and user.check_password(password):
            if not user.email_verified:
                flash("Please verify your email first. Check your inbox or request a new link.", "warning")
                return redirect(url_for("auth_resend"))
            problem = account_problem(user)
            if problem:
                flash(problem[0], problem[1])
                return render_template("login.html")
            login_user(user)
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(url_for("home"))
        flash("Invalid login ID or password.", "danger")
    return render_template("login.html")


@app.post("/logout")
@login_required
def auth_logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth_login"))


@app.route("/register", methods=["GET", "POST"])
def auth_register():
    """Managers create their own account. Employees are added by managers, not here."""
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    ready = email_ready()
    closed = not setting_on("signup_open")
    needs_approval = setting_on("require_approval")
    form = {}
    ctx = dict(ready=ready, closed=closed, needs_approval=needs_approval)
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in ("name", "company", "email")}
        form["email"] = form["email"].lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        error = None
        if closed:
            error = "New sign-ups are currently closed."
        elif not ready:
            error = "Sign-up is unavailable right now because email is not configured."
        elif not form["name"] or not form["email"] or not password:
            error = "Name, email and password are required."
        elif not EMAIL_RE.match(form["email"]):
            error = "Enter a valid email address."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif User.query.filter(db.func.lower(User.email) == form["email"]).first():
            error = "An account with this email already exists. Sign in or reset your password."
        if error:
            flash(error, "danger")
            return render_template("register.html", form=form, **ctx)
        manager = User(name=form["name"], company=form["company"], email=form["email"], designation="Manager",
                       role="manager", active=True, email_verified=False,
                       approval="pending" if needs_approval else "approved")
        manager.set_password(password)
        db.session.add(manager)
        db.session.flush()
        assign_employee_id(manager)
        db.session.commit()
        sent, _ = send_link_mail(manager, "verify")
        if sent:
            flash("Account created! We sent a verification link to your email. Verify it, then sign in.", "success")
        else:
            flash("Account created, but the verification email could not be sent. Use 'Resend verification email'.", "warning")
        return redirect(url_for("auth_login"))
    return render_template("register.html", form=form, **ctx)


@app.route("/verify/<token>")
def verify_email(token):
    user, _ = read_token(token, "verify")
    if not user:
        flash("This verification link is invalid or has expired. Request a new one.", "danger")
        return redirect(url_for("auth_resend"))
    first_time = not user.email_verified
    if first_time:
        user.email_verified = True
        db.session.commit()
    if user.role == "manager" and user.approval == "pending":
        flash("Email verified! Your account is now waiting for admin approval. We will email you once it is approved.", "warning")
        if first_time:
            notify_admins_new_manager(user)
    else:
        flash("Email verified! You can sign in now.", "success")
    return redirect(url_for("auth_login"))


@app.route("/resend-verification", methods=["GET", "POST"])
def auth_resend():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter(db.func.lower(User.email) == email).first() if email else None
        if user and not user.email_verified and user.active:
            if cooldown_left(user):
                flash(f"Please wait {cooldown_left(user)} seconds before requesting another email.", "warning")
                return render_template("resend_verification.html")
            # Managers verify via the signup link; invited employees get a set-password link.
            kind = "verify" if user.role == "manager" else "invite"
            send_link_mail(user, kind)
        flash("If that account exists and is not verified yet, a new email has been sent.", "success")
        return redirect(url_for("auth_login"))
    return render_template("resend_verification.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def auth_forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter(db.func.lower(User.email) == email).first() if email else None
        if user and user.active:
            if cooldown_left(user):
                flash(f"Please wait {cooldown_left(user)} seconds before requesting another email.", "warning")
                return render_template("forgot_password.html")
            send_link_mail(user, "reset")
        flash("If an account exists for that email, a password reset link has been sent.", "success")
        return redirect(url_for("auth_login"))
    return render_template("forgot_password.html")


@app.route("/set-password/<token>", methods=["GET", "POST"])
def set_password(token):
    """Used by employee invites and by 'forgot password'. Proves the person owns the email."""
    user, kind = read_token(token, "invite", "reset")
    if not user:
        flash("This link is invalid, expired or already used. Request a new one.", "danger")
        return redirect(url_for("auth_forgot"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
        elif password != confirm:
            flash("Passwords do not match.", "danger")
        else:
            user.set_password(password)
            user.email_verified = True
            db.session.commit()
            flash("Password saved. You can sign in now.", "success")
            return redirect(url_for("auth_login"))
    return render_template("set_password.html", user=user, kind=kind)


# ---------------- COMMON ACCOUNT PAGES ----------------
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name:
            flash("Name is required.", "danger")
            return render_template("profile.html")
        current_user.name = name
        current_user.phone = phone
        if current_user.role == "manager":
            current_user.company = request.form.get("company", "").strip()
        db.session.commit()
        flash("Profile updated successfully.", "success")
        return redirect(url_for("profile"))
    return render_template("profile.html")


@app.route("/profile/password", methods=["GET", "POST"])
@login_required
def profile_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not current_user.check_password(current_password):
            flash("Current password is incorrect.", "danger")
        elif len(new_password) < 6:
            flash("New password must be at least 6 characters.", "danger")
        elif new_password != confirm_password:
            flash("New passwords do not match.", "danger")
        else:
            current_user.set_password(new_password)
            db.session.commit()
            flash("Your password was updated successfully.", "success")
            return redirect(url_for("profile"))
    return render_template("profile_password.html")


@app.route("/notifications")
@login_required
def notifications():
    base = TaskHistory.query.join(Task, TaskHistory.task_id == Task.id)
    if current_user.role == "manager":
        items = base.filter(Task.assigned_to_id.in_(team_ids_select())).order_by(TaskHistory.created_at.desc()).limit(30).all()
    elif current_user.role == "employee":
        items = base.filter(Task.assigned_to_id == current_user.id).order_by(TaskHistory.created_at.desc()).limit(30).all()
    else:
        items = []  # the platform admin does not see managers' task details
    return render_template("notifications.html", items=items)


# ---------------- MANAGER (own team only) ----------------
@app.route("/manager/dashboard")
@role_required("manager")
def manager_dashboard():
    base = scoped_tasks()
    total = base.count()
    pending = base.filter(Task.status == "Pending").count()
    in_progress = base.filter(Task.status == "In Progress").count()
    completed = base.filter(Task.status == "Completed").count()
    employees = team_query().filter_by(active=True).count()
    urgent = base.filter(Task.priority == "Urgent", Task.status != "Completed").count()
    recent = base.order_by(Task.updated_at.desc()).limit(8).all()
    return render_template("manager/dashboard.html", total=total, pending=pending, in_progress=in_progress,
                           completed=completed, employees=employees, urgent=urgent, recent=recent)


def filtered_tasks(query):
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "").strip()
    priority = request.args.get("priority", "").strip()
    employee_id = request.args.get("employee", "").strip()
    due = request.args.get("due", "").strip()

    q = scoped_tasks()
    if search:
        like = f"%{search}%"
        q = q.filter(db.or_(Task.task_code.ilike(like), Task.title.ilike(like), Task.description.ilike(like)))
    if status:
        q = q.filter(Task.status == status)
    if priority:
        q = q.filter(Task.priority == priority)
    if employee_id.isdigit():
        q = q.filter(Task.assigned_to_id == int(employee_id))
    if due == "overdue":
        q = q.filter(Task.due_date < datetime.utcnow().date(), Task.status != "Completed")
    elif due == "today":
        q = q.filter(Task.due_date == datetime.utcnow().date())

    # Preserve task list sorting in the URL so it survives page actions.
    sort = request.args.get("sort", "default").strip()
    direction = request.args.get("direction", "asc").strip().lower()
    if sort == "task_id":
        q = q.order_by(Task.task_code.desc() if direction == "desc" else Task.task_code.asc())
    else:
        q = q.order_by(Task.due_date.asc().nullslast(), Task.updated_at.desc())
    return q


@app.route("/manager/tasks")
@role_required("manager")
def manager_tasks():
    tasks = filtered_tasks(Task.query).all()
    return render_template("manager/tasks.html", tasks=tasks, employees=active_employees())


@app.route("/manager/tasks/print")
@role_required("manager")
def manager_tasks_print():
    tasks = filtered_tasks(Task.query).all()
    return render_template("manager/tasks_print.html", tasks=tasks)


def manager_task_form(task=None):
    employees = active_employees()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Task title is required.", "danger")
            return render_template("manager/task_form.html", task=task, employees=employees)
        assigned = request.form.get("assigned_to", "")
        assignee = db.session.get(User, int(assigned)) if assigned.isdigit() else None
        if not assignee or assignee.role != "employee" or not assignee.active or assignee.manager_id != current_user.id:
            flash("Please select an active employee.", "danger")
            return render_template("manager/task_form.html", task=task, employees=employees)
        start = parse_date(request.form.get("start_date"))
        due = parse_date(request.form.get("due_date"))
        if start and due and due < start:
            flash("Due date cannot be before start date.", "danger")
            return render_template("manager/task_form.html", task=task, employees=employees)
        if task is None:
            task = Task(title=title, description=request.form.get("description", "").strip(),
                        assigned_to_id=assignee.id, created_by_id=current_user.id, start_date=start, due_date=due,
                        status=request.form.get("status", "Pending"), priority=request.form.get("priority", "Medium"))
            db.session.add(task)
            db.session.flush()
            task.task_code = make_task_code(task)
            log_history(task, "Created", f"Assigned to {assignee.name}")
            db.session.commit()
            flash(f"{task.task_code} created successfully.", "success")
        else:
            old_assignee = task.assignee.name
            task.title = title
            task.description = request.form.get("description", "").strip()
            task.assigned_to_id = assignee.id
            task.start_date = start
            task.due_date = due
            task.status = request.form.get("status", "Pending")
            task.priority = request.form.get("priority", "Medium")
            if old_assignee != assignee.name:
                log_history(task, "Transferred", f"{old_assignee} → {assignee.name}")
            else:
                log_history(task, "Updated", "Task details updated")
            db.session.commit()
            flash(f"{task.task_code} updated successfully.", "success")
        return redirect(url_for(
            "manager_tasks",
            search=request.form.get("return_search", ""),
            status=request.form.get("return_status", ""),
            priority=request.form.get("return_priority", ""),
            employee=request.form.get("return_employee", ""),
            due=request.form.get("return_due", ""),
            sort=request.form.get("return_sort", "default"),
            direction=request.form.get("return_direction", "asc")
        ))
    return render_template("manager/task_form.html", task=task, employees=employees)


@app.route("/manager/tasks/new", methods=["GET", "POST"])
@role_required("manager")
def manager_task_new():
    return manager_task_form()


@app.route("/manager/tasks/<int:task_id>/edit", methods=["GET", "POST"])
@role_required("manager")
def manager_task_edit(task_id):
    task = get_team_task(task_id)
    return manager_task_form(task)


@app.post("/manager/tasks/<int:task_id>/delete")
@role_required("manager")
def manager_task_delete(task_id):
    task = get_team_task(task_id)
    code = task.task_code
    db.session.delete(task)
    db.session.commit()
    flash(f"{code} deleted.", "success")
    return redirect(url_for(
        "manager_tasks",
        search=request.form.get("search", ""),
        status=request.form.get("status", ""),
        priority=request.form.get("priority", ""),
        employee=request.form.get("employee", ""),
        due=request.form.get("due", ""),
        sort=request.form.get("sort", "default"),
        direction=request.form.get("direction", "asc")
    ))


@app.post("/manager/tasks/<int:task_id>/transfer")
@role_required("manager")
def manager_task_transfer(task_id):
    task = get_team_task(task_id)
    employee_id = request.form.get("employee_id", "")
    employee = db.session.get(User, int(employee_id)) if employee_id.isdigit() else None
    if not employee or employee.role != "employee" or not employee.active or employee.manager_id != current_user.id:
        abort(400)
    old_name = task.assignee.name
    task.assigned_to_id = employee.id
    log_history(task, "Transferred", f"{old_name} → {employee.name}")
    db.session.commit()
    flash(f"{task.task_code} transferred to {employee.name}.", "success")
    return redirect(url_for(
        "manager_tasks",
        search=request.form.get("search", ""),
        status=request.form.get("status", ""),
        priority=request.form.get("priority", ""),
        employee=request.form.get("employee", ""),
        due=request.form.get("due", ""),
        sort=request.form.get("sort", "default"),
        direction=request.form.get("direction", "asc")
    ))


@app.route("/manager/employees")
@role_required("manager")
def manager_employees():
    employees = team_query().order_by(User.active.desc(), User.name.asc()).all()
    ids = [e.id for e in employees]
    task_counts = dict(db.session.query(Task.assigned_to_id, func.count(Task.id)).filter(Task.assigned_to_id.in_(ids)).group_by(Task.assigned_to_id).all()) if ids else {}
    return render_template("manager/employees.html", employees=employees, task_counts=task_counts)


@app.route("/manager/employees/new", methods=["GET", "POST"])
@role_required("manager")
def manager_employee_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        employee_id = request.form.get("employee_id", "").strip()
        email = request.form.get("email", "").strip().lower()
        error = None
        if not name or not email:
            error = "Name and email are required."
        elif not EMAIL_RE.match(email):
            error = "Enter a valid email address."
        elif User.query.filter(db.func.lower(User.email) == email).first():
            error = "An account with this email already exists."
        elif employee_id and User.query.filter(db.func.lower(User.employee_id) == employee_id.lower()).first():
            error = "That Employee ID already exists."
        elif not email_ready():
            error = "Email is not configured, so the invite cannot be sent."
        if error:
            flash(error, "danger")
            return render_template("manager/employee_form.html", employee=None)
        employee = User(employee_id=employee_id or None, name=name, email=email, phone=request.form.get("phone", "").strip(),
                        designation=request.form.get("designation", "Employee").strip() or "Employee",
                        role="employee", manager_id=current_user.id, active=True, email_verified=False)
        # Random placeholder; the employee chooses a real password from the e-mailed invite link.
        employee.set_password(secrets.token_urlsafe(24))
        db.session.add(employee)
        db.session.flush()
        if not employee.employee_id:
            assign_employee_id(employee)
        db.session.commit()
        sent, link = send_link_mail(employee, "invite")
        if sent:
            flash(f"{employee.name} added. An invite email was sent to {employee.email}.", "success")
        else:
            flash(f"{employee.name} added, but the email could not be sent. Share this link with them manually: {link}", "warning")
        return redirect(url_for("manager_employees"))
    return render_template("manager/employee_form.html", employee=None)


@app.route("/manager/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@role_required("manager")
def manager_employee_edit(employee_id):
    employee = get_team_employee(employee_id)
    if request.method == "POST":
        emp_code = request.form.get("employee_id", "").strip()
        name = request.form.get("name", "").strip()
        if not name or not emp_code:
            flash("Name and Employee ID are required.", "danger")
            return render_template("manager/employee_form.html", employee=employee)
        duplicate_id = User.query.filter(User.id != employee.id, db.func.lower(User.employee_id) == emp_code.lower()).first()
        if duplicate_id:
            flash("That Employee ID is already in use.", "danger")
            return render_template("manager/employee_form.html", employee=employee)
        # Email is the verified identity and cannot be edited here.
        employee.employee_id = emp_code
        employee.name = name
        employee.phone = request.form.get("phone", "").strip()
        employee.designation = request.form.get("designation", "Employee").strip() or "Employee"
        db.session.commit()
        flash(f"{employee.name} updated.", "success")
        return redirect(url_for("manager_employees"))
    return render_template("manager/employee_form.html", employee=employee)


@app.post("/manager/employees/<int:employee_id>/invite")
@role_required("manager")
def manager_employee_invite(employee_id):
    """Resend the invite (not yet verified) or send a password-reset link (already active)."""
    employee = get_team_employee(employee_id)
    if cooldown_left(employee):
        flash(f"Please wait {cooldown_left(employee)} seconds before sending another email.", "warning")
        return redirect(url_for("manager_employees"))
    kind = "reset" if employee.email_verified else "invite"
    sent, link = send_link_mail(employee, kind)
    if sent:
        flash(f"Email sent to {employee.email}.", "success")
    else:
        flash(f"Email could not be sent. Share this link manually: {link}", "warning")
    return redirect(url_for("manager_employees"))


@app.post("/manager/employees/<int:employee_id>/toggle")
@role_required("manager")
def manager_employee_toggle(employee_id):
    employee = get_team_employee(employee_id)
    employee.active = not employee.active
    db.session.commit()
    flash(f"{employee.name} is now {'active' if employee.active else 'inactive'}.", "success")
    return redirect(url_for("manager_employees"))


@app.route("/manager/reports")
@role_required("manager")
def manager_reports():
    employees = team_query().order_by(User.name.asc()).all()
    report_rows = []
    for employee in employees:
        base = Task.query.filter_by(assigned_to_id=employee.id)
        report_rows.append({
            "employee": employee,
            "total": base.count(),
            "pending": base.filter_by(status="Pending").count(),
            "in_progress": base.filter_by(status="In Progress").count(),
            "completed": base.filter_by(status="Completed").count(),
        })
    return render_template("manager/reports.html", rows=report_rows)


@app.route("/manager/settings")
@role_required("manager")
def manager_settings():
    return render_template("settings.html", manager_settings=True)


# ---------------- PLATFORM ADMIN (owner of the whole system) ----------------
MANAGER_FILTERS = ["all", "pending", "active", "blocked", "unverified", "rejected"]


def _get_manager(manager_id):
    m = db.session.get(User, manager_id)
    if not m or m.role != "manager":
        abort(404)
    return m


def _back(default_endpoint="platform_managers", **kw):
    """Return to the page the admin came from (only internal /admin/ paths are allowed)."""
    nxt = request.form.get("next", "")
    if nxt.startswith("/admin/") and "//" not in nxt:
        return redirect(nxt)
    return redirect(url_for(default_endpoint, **kw))


def delete_manager_cascade(manager):
    """Remove a manager together with their employees, tasks and task history."""
    emp_ids = [i for (i,) in db.session.query(User.id).filter(User.manager_id == manager.id).all()]
    user_ids = emp_ids + [manager.id]
    task_ids = [i for (i,) in db.session.query(Task.id).filter(
        db.or_(Task.assigned_to_id.in_(user_ids), Task.created_by_id.in_(user_ids))).all()]
    if task_ids:
        TaskHistory.query.filter(TaskHistory.task_id.in_(task_ids)).delete(synchronize_session=False)
    TaskHistory.query.filter(TaskHistory.changed_by_id.in_(user_ids)).delete(synchronize_session=False)
    if task_ids:
        Task.query.filter(Task.id.in_(task_ids)).delete(synchronize_session=False)
    if emp_ids:
        User.query.filter(User.id.in_(emp_ids)).delete(synchronize_session=False)
    User.query.filter(User.id == manager.id).delete(synchronize_session=False)
    db.session.commit()


@app.route("/admin/dashboard")
@role_required("admin")
def platform_dashboard():
    managers = User.query.filter_by(role="manager")
    stats = {
        "managers": managers.count(),
        "pending": managers.filter_by(approval="pending").count(),
        "active": managers.filter_by(approval="approved", active=True).count(),
        "employees": User.query.filter_by(role="employee").count(),
        "tasks": Task.query.count(),
    }
    awaiting = managers.filter_by(approval="pending").order_by(User.created_at.asc()).limit(10).all()
    recent = managers.order_by(User.created_at.desc()).limit(6).all()
    return render_template("admin/dashboard.html", stats=stats, awaiting=awaiting, recent=recent)


@app.route("/admin/managers")
@role_required("admin")
def platform_managers():
    status = request.args.get("status", "all")
    if status not in MANAGER_FILTERS:
        status = "all"
    rows = db.session.query(User.approval, User.active, User.email_verified).filter(User.role == "manager").all()
    counts = {
        "all": len(rows),
        "pending": sum(1 for a, _, _ in rows if a == "pending"),
        "rejected": sum(1 for a, _, _ in rows if a == "rejected"),
        "blocked": sum(1 for _, act, _ in rows if not act),
        "unverified": sum(1 for _, _, v in rows if not v),
        "active": sum(1 for a, act, v in rows if a == "approved" and act and v),
    }
    q = User.query.filter_by(role="manager")
    if status == "pending":
        q = q.filter(User.approval == "pending")
    elif status == "rejected":
        q = q.filter(User.approval == "rejected")
    elif status == "blocked":
        q = q.filter(User.active.is_(False))
    elif status == "unverified":
        q = q.filter(User.email_verified.is_(False))
    elif status == "active":
        q = q.filter(User.approval == "approved", User.active.is_(True), User.email_verified.is_(True))
    managers = q.order_by(User.created_at.desc()).all()
    emp_counts = dict(db.session.query(User.manager_id, func.count(User.id)).filter(User.role == "employee").group_by(User.manager_id).all())
    task_counts = dict(db.session.query(User.manager_id, func.count(Task.id)).join(Task, Task.assigned_to_id == User.id)
                       .filter(User.role == "employee").group_by(User.manager_id).all())
    return render_template("admin/managers.html", managers=managers, emp_counts=emp_counts, task_counts=task_counts,
                           counts=counts, status=status)


@app.route("/admin/managers/<int:manager_id>")
@role_required("admin")
def platform_manager_detail(manager_id):
    m = _get_manager(manager_id)
    employees = User.query.filter_by(role="employee", manager_id=m.id).order_by(User.name.asc()).all()
    ids = [e.id for e in employees]
    per_emp = {e.id: {"total": 0, "Pending": 0, "In Progress": 0, "Completed": 0} for e in employees}
    if ids:
        for emp_id, status, n in db.session.query(Task.assigned_to_id, Task.status, func.count(Task.id)) \
                .filter(Task.assigned_to_id.in_(ids)).group_by(Task.assigned_to_id, Task.status).all():
            per_emp[emp_id]["total"] += n
            per_emp[emp_id][status] = per_emp[emp_id].get(status, 0) + n
    totals = {"employees": len(employees), "active": sum(1 for e in employees if e.active and e.email_verified),
              "tasks": sum(v["total"] for v in per_emp.values()), "done": sum(v["Completed"] for v in per_emp.values())}
    return render_template("admin/manager_detail.html", m=m, employees=employees, per_emp=per_emp, totals=totals)


@app.post("/admin/managers/<int:manager_id>/approve")
@role_required("admin")
def platform_manager_approve(manager_id):
    m = _get_manager(manager_id)
    m.approval = "approved"
    db.session.commit()
    notify_manager_decision(m, True)
    flash(f"{m.name} approved." + ("" if m.email_verified else " They still need to verify their email."), "success")
    return _back()


@app.post("/admin/managers/<int:manager_id>/reject")
@role_required("admin")
def platform_manager_reject(manager_id):
    m = _get_manager(manager_id)
    m.approval = "rejected"
    db.session.commit()
    notify_manager_decision(m, False)
    flash(f"{m.name}'s request was rejected.", "success")
    return _back()


@app.post("/admin/managers/<int:manager_id>/toggle")
@role_required("admin")
def platform_manager_toggle(manager_id):
    m = _get_manager(manager_id)
    m.active = not m.active
    db.session.commit()
    flash(f"{m.name} (and their team) is now {'active' if m.active else 'blocked'}.", "success")
    return _back()


@app.post("/admin/managers/<int:manager_id>/verify")
@role_required("admin")
def platform_manager_verify(manager_id):
    m = _get_manager(manager_id)
    m.email_verified = True
    db.session.commit()
    flash(f"{m.name}'s email marked as verified.", "success")
    return _back()


@app.post("/admin/managers/<int:manager_id>/resend")
@role_required("admin")
def platform_manager_resend(manager_id):
    m = _get_manager(manager_id)
    if m.email_verified:
        flash("This manager is already verified.", "info")
    elif cooldown_left(m):
        flash(f"Please wait {cooldown_left(m)} seconds before sending another email.", "warning")
    else:
        sent, link = send_link_mail(m, "verify")
        flash("Verification email sent." if sent else f"Email could not be sent. Link: {link}", "success" if sent else "warning")
    return _back()


@app.post("/admin/managers/<int:manager_id>/reset-link")
@role_required("admin")
def platform_manager_reset_link(manager_id):
    m = _get_manager(manager_id)
    if cooldown_left(m):
        flash(f"Please wait {cooldown_left(m)} seconds before sending another email.", "warning")
    else:
        sent, link = send_link_mail(m, "reset")
        flash(f"Password reset link sent to {m.email}." if sent else f"Email could not be sent. Link: {link}", "success" if sent else "warning")
    return _back()


@app.post("/admin/managers/<int:manager_id>/edit")
@role_required("admin")
def platform_manager_edit(manager_id):
    m = _get_manager(manager_id)
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    if not name or not EMAIL_RE.match(email):
        flash("A name and a valid email are required.", "danger")
        return redirect(url_for("platform_manager_detail", manager_id=m.id))
    if User.query.filter(User.id != m.id, db.func.lower(User.email) == email).first():
        flash("That email is already used by another account.", "danger")
        return redirect(url_for("platform_manager_detail", manager_id=m.id))
    changed = email != m.email
    m.name = name
    m.company = request.form.get("company", "").strip()
    m.phone = request.form.get("phone", "").strip()
    if changed:
        m.email = email
        m.email_verified = bool(request.form.get("mark_verified"))
    db.session.commit()
    if changed and not m.email_verified:
        sent, link = send_link_mail(m, "verify")
        flash("Details saved. A verification email was sent to the new address." if sent
              else f"Details saved, but the email could not be sent. Verification link: {link}", "success" if sent else "warning")
    else:
        flash("Manager details updated.", "success")
    return redirect(url_for("platform_manager_detail", manager_id=m.id))


@app.post("/admin/managers/<int:manager_id>/password")
@role_required("admin")
def platform_manager_password(manager_id):
    m = _get_manager(manager_id)
    password = request.form.get("password", "")
    if len(password) < 6:
        flash("Password must be at least 6 characters.", "danger")
    else:
        m.set_password(password)
        db.session.commit()
        flash(f"Password changed for {m.name}. Share it with them securely.", "success")
    return redirect(url_for("platform_manager_detail", manager_id=m.id))


@app.post("/admin/managers/<int:manager_id>/delete")
@role_required("admin")
def platform_manager_delete(manager_id):
    m = _get_manager(manager_id)
    if request.form.get("confirm_email", "").strip().lower() != m.email.lower():
        flash("Delete cancelled: the confirmation email did not match.", "danger")
        return redirect(url_for("platform_manager_detail", manager_id=m.id))
    name = m.name
    delete_manager_cascade(m)
    flash(f"{name} and all of their employees and tasks were deleted.", "success")
    return redirect(url_for("platform_managers"))


@app.route("/admin/settings")
@role_required("admin")
def platform_settings():
    return render_template("admin/settings.html", email_ok=email_configured(),
                           signup_open=setting_on("signup_open"), require_approval=setting_on("require_approval"))


@app.post("/admin/settings/toggle")
@role_required("admin")
def platform_setting_toggle():
    key = request.form.get("key", "")
    if key not in SETTING_DEFAULTS:
        abort(400)
    new_value = "0" if setting_on(key) else "1"
    set_setting(key, new_value)
    labels = {"signup_open": "New manager sign-ups", "require_approval": "Admin approval for new managers"}
    flash(f"{labels[key]} turned {'ON' if new_value == '1' else 'OFF'}.", "success")
    return redirect(url_for("platform_settings"))


@app.post("/admin/settings/test-email")
@role_required("admin")
def platform_test_email():
    ok = send_email(current_user.email, "OfficeFlow test email", "Email is working. You can ignore this message.")
    flash(f"Test email sent to {current_user.email}." if ok else "Test email failed. Check your SMTP_* variables.", "success" if ok else "danger")
    return redirect(url_for("platform_settings"))


# ---------------- EMPLOYEE ----------------
@app.route("/employee/dashboard")
@role_required("employee")
def employee_dashboard():
    base = Task.query.filter_by(assigned_to_id=current_user.id)
    total = base.count()
    pending = base.filter_by(status="Pending").count()
    in_progress = base.filter_by(status="In Progress").count()
    completed = base.filter_by(status="Completed").count()
    urgent = base.filter_by(priority="Urgent").filter(Task.status != "Completed").count()
    recent = base.order_by(Task.updated_at.desc()).limit(8).all()
    return render_template("employee/dashboard.html", total=total, pending=pending, in_progress=in_progress,
                           completed=completed, urgent=urgent, recent=recent)


@app.route("/employee/tasks")
@role_required("employee")
def employee_tasks():
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "").strip()
    priority = request.args.get("priority", "").strip()
    sort = request.args.get("sort", "default").strip()
    direction = request.args.get("direction", "asc").strip().lower()

    q = Task.query.filter_by(assigned_to_id=current_user.id)
    if search:
        like = f"%{search}%"
        q = q.filter(db.or_(Task.task_code.ilike(like), Task.title.ilike(like), Task.description.ilike(like)))
    if status:
        q = q.filter_by(status=status)
    if priority:
        q = q.filter_by(priority=priority)

    if sort == "task_id":
        q = q.order_by(Task.task_code.desc() if direction == "desc" else Task.task_code.asc())
    else:
        q = q.order_by(Task.due_date.asc().nullslast(), Task.updated_at.desc())

    tasks = q.all()
    return render_template("employee/tasks.html", tasks=tasks)


@app.route("/employee/tasks/new", methods=["GET", "POST"])
@role_required("employee")
def employee_task_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        start = parse_date(request.form.get("start_date"))
        due = parse_date(request.form.get("due_date"))
        if not title:
            flash("Task title is required.", "danger")
        elif start and due and due < start:
            flash("Due date cannot be before start date.", "danger")
        else:
            task = Task(title=title, description=request.form.get("description", "").strip(),
                        assigned_to_id=current_user.id, created_by_id=current_user.id, start_date=start, due_date=due,
                        status=request.form.get("status", "Pending"), priority=request.form.get("priority", "Medium"))
            db.session.add(task)
            db.session.flush()
            task.task_code = make_task_code(task)
            log_history(task, "Created", "Employee created task")
            db.session.commit()
            flash(f"{task.task_code} created successfully.", "success")
            return redirect(url_for(
                "employee_tasks",
                search=request.form.get("return_search", ""),
                status=request.form.get("return_status", ""),
                priority=request.form.get("return_priority", ""),
                sort=request.form.get("return_sort", "default"),
                direction=request.form.get("return_direction", "asc")
            ))
    return render_template("employee/task_form.html", task=None)


@app.route("/employee/tasks/<int:task_id>/edit", methods=["GET", "POST"])
@role_required("employee")
def employee_task_edit(task_id):
    task = db.session.get(Task, task_id)
    if not task or task.assigned_to_id != current_user.id:
        abort(404)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        start = parse_date(request.form.get("start_date"))
        due = parse_date(request.form.get("due_date"))
        if not title:
            flash("Task title is required.", "danger")
        elif start and due and due < start:
            flash("Due date cannot be before start date.", "danger")
        else:
            task.title = title
            task.description = request.form.get("description", "").strip()
            task.start_date = start
            task.due_date = due
            task.priority = request.form.get("priority", "Medium")
            task.status = request.form.get("status", task.status)
            log_history(task, "Updated", "Employee updated own task")
            db.session.commit()
            flash(f"{task.task_code} updated.", "success")
            return redirect(url_for(
                "employee_tasks",
                search=request.form.get("return_search", ""),
                status=request.form.get("return_status", ""),
                priority=request.form.get("return_priority", ""),
                sort=request.form.get("return_sort", "default"),
                direction=request.form.get("return_direction", "asc")
            ))
    return render_template("employee/task_form.html", task=task)


@app.post("/employee/tasks/<int:task_id>/status")
@role_required("employee")
def employee_task_status(task_id):
    task = db.session.get(Task, task_id)
    if not task or task.assigned_to_id != current_user.id:
        abort(404)
    status = request.form.get("status", "")
    if status not in ["Pending", "In Progress", "Completed"]:
        abort(400)
    old = task.status
    task.status = status
    log_history(task, "Status changed", f"{old} → {status}")
    db.session.commit()
    flash(f"{task.task_code} status changed to {status}.", "success")
    return redirect(url_for(
        "employee_tasks",
        search=request.form.get("return_search", ""),
        status=request.form.get("return_status", ""),
        priority=request.form.get("return_priority", ""),
        sort=request.form.get("return_sort", "default"),
        direction=request.form.get("return_direction", "asc")
    ))


@app.route("/employee/reports")
@role_required("employee")
def employee_reports():
    base = Task.query.filter_by(assigned_to_id=current_user.id)
    summary = {
        "total": base.count(),
        "pending": base.filter_by(status="Pending").count(),
        "in_progress": base.filter_by(status="In Progress").count(),
        "completed": base.filter_by(status="Completed").count(),
    }
    return render_template("employee/reports.html", summary=summary, tasks=base.order_by(Task.due_date.asc().nullslast()).all())


@app.route("/employee/settings")
@role_required("employee")
def employee_settings():
    return render_template("settings.html", manager_settings=False)


@app.get("/health")
def health():
    """Simple deployment health check."""
    if CONFIG_ERRORS:
        return {"status": "error", "database": "unconfigured", "errors": CONFIG_ERRORS}, 503
    try:
        db.session.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok", "email": "configured" if email_configured() else "not configured"}, 200
    except Exception:
        db.session.rollback()
        return {"status": "error", "database": "unavailable"}, 503


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, message="You do not have permission to access this page."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, message="The requested page was not found."), 404


@app.cli.command("init-db")
def init_db_command():
    ensure_schema()
    print("Database initialized.")


with app.app_context():
    # On Vercel, DATABASE_URL should point to a persistent PostgreSQL database.
    # The first admin is created automatically from ADMIN_* environment variables.
    if db_url.startswith("sqlite:///") and db_url != "sqlite:///:memory:":
        Path(BASE_DIR / "instance").mkdir(exist_ok=True)
    if not CONFIG_ERRORS:
        try:
            ensure_schema()
            bootstrap_admin()
        except Exception as exc:  # pragma: no cover - defensive guard for cold starts
            CONFIG_ERRORS.append(f"Database setup failed: {exc}")


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "1") == "1", host="127.0.0.1", port=int(os.getenv("PORT", "5000")))
