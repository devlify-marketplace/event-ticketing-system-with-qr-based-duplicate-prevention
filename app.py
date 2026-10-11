from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

import qrcode
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
TOKEN_RE = re.compile(r"^([0-9a-f]{32})\.([0-9a-f]{64})$")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'gate')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    venue TEXT NOT NULL,
    city TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    price INTEGER NOT NULL CHECK (price >= 0),
    currency TEXT NOT NULL DEFAULT 'NGN',
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT NOT NULL UNIQUE,
    buyer_name TEXT NOT NULL,
    buyer_email TEXT NOT NULL,
    total_amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'demo_paid' CHECK (status IN ('demo_paid', 'cancelled')),
    access_hash TEXT NOT NULL,
    payment_reference TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    attendee_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'used', 'cancelled')),
    used_at TEXT,
    used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tickets_event_status ON tickets(event_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_order ON tickets(order_id);
CREATE TABLE IF NOT EXISTS checkin_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    ticket_id INTEGER REFERENCES tickets(id) ON DELETE SET NULL,
    staff_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL,
    scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_checkin_event_time ON checkin_log(event_id, scanned_at DESC);
"""


def _password_digest(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


def _ensure_secret(instance_dir: Path) -> str:
    configured = os.environ.get("SECRET_KEY")
    if configured:
        return configured
    path = instance_dir / ".secret_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(48)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def _now_local_iso() -> str:
    return (datetime.now().replace(second=0, microsecond=0) + timedelta(days=45)).strftime("%Y-%m-%dT%H:%M")


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_path=str(BASE_DIR / "instance"), instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.from_mapping(
        SECRET_KEY=_ensure_secret(Path(app.instance_path)),
        DATABASE=os.environ.get("DATABASE_PATH", str(Path(app.instance_path) / "ticketing.sqlite3")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "TicketDemo!77"),
        GATE_PASSWORD=os.environ.get("GATE_PASSWORD", "GateDemo!77"),
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    def open_database() -> sqlite3.Connection:
        connection = sqlite3.connect(app.config["DATABASE"], timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize_database() -> None:
        db = open_database()
        try:
            db.executescript(SCHEMA)
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                db.execute(
                    "INSERT INTO users(email, password_hash, role) VALUES (?, ?, 'admin')",
                    ("admin@example.com", _password_digest(app.config["ADMIN_PASSWORD"])),
                )
                db.execute(
                    "INSERT INTO users(email, password_hash, role) VALUES (?, ?, 'gate')",
                    ("gate@example.com", _password_digest(app.config["GATE_PASSWORD"])),
                )
            if db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0:
                db.execute(
                    """INSERT INTO events(title, description, venue, city, starts_at, price, currency, capacity)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "Lagos Afterglow Live",
                        "One unforgettable night of live music, bold new sounds and a room full of good energy. Your QR ticket is issued immediately after demo checkout.",
                        "The Harbour Arena",
                        "Lagos",
                        _now_local_iso(),
                        12500,
                        "NGN",
                        240,
                    ),
                )
        finally:
            db.close()

    initialize_database()

    @app.before_request
    def attach_database() -> None:
        g.db = open_database()
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(24)
        if request.method == "POST":
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            expected = session.get("csrf_token", "")
            if not supplied or not hmac.compare_digest(str(supplied), str(expected)):
                abort(400, description="This form expired. Reload the page and try again.")

    @app.teardown_request
    def close_database(_error: BaseException | None = None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def template_helpers() -> dict[str, Any]:
        return {"csrf_token": lambda: session.get("csrf_token", "")}

    def money(amount: int, currency: str) -> str:
        symbols = {"NGN": "₦", "USD": "$", "GBP": "£", "EUR": "€", "GHS": "GH₵", "KES": "KSh "}
        symbol = symbols.get(currency.upper(), f"{currency.upper()} ")
        return f"{symbol}{amount:,.0f}"

    app.jinja_env.filters["money"] = money

    def login_required(*roles: str):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                if not session.get("user_id"):
                    return redirect(url_for("login", next=request.path))
                if roles and session.get("role") not in roles:
                    abort(403)
                return view(*args, **kwargs)
            return wrapped
        return decorator

    def make_ticket_token(public_id: str) -> str:
        signature = hmac.new(
            app.config["SECRET_KEY"].encode("utf-8"),
            f"ticket:{public_id}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{public_id}.{signature}"

    def verify_ticket_token(token: str) -> str | None:
        match = TOKEN_RE.fullmatch(token.strip())
        if not match:
            return None
        public_id, supplied_signature = match.groups()
        expected = make_ticket_token(public_id).split(".", 1)[1]
        return public_id if hmac.compare_digest(supplied_signature, expected) else None

    def qr_data_uri(token: str) -> str:
        image = qrcode.make(token, box_size=7, border=3)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    def event_with_sales(event_id: int) -> sqlite3.Row | None:
        return g.db.execute(
            """SELECT e.*, (SELECT COUNT(*) FROM tickets t JOIN orders o ON o.id=t.order_id
                              WHERE t.event_id=e.id AND o.status='demo_paid') AS sold
               FROM events e WHERE e.id=?""",
            (event_id,),
        ).fetchone()

    @app.get("/")
    def index():
        events = g.db.execute(
            """SELECT e.*, (SELECT COUNT(*) FROM tickets t JOIN orders o ON o.id=t.order_id
                              WHERE t.event_id=e.id AND o.status='demo_paid') AS sold
               FROM events e WHERE e.is_active=1 ORDER BY e.starts_at ASC"""
        ).fetchall()
        return render_template("index.html", events=events)

    @app.get("/events/<int:event_id>")
    def event_detail(event_id: int):
        event = event_with_sales(event_id)
        if event is None or not event["is_active"]:
            abort(404)
        return render_template("event_detail.html", event=event, remaining=max(0, event["capacity"] - event["sold"]))

    @app.post("/events/<int:event_id>/checkout")
    def checkout(event_id: int):
        event = event_with_sales(event_id)
        if event is None or not event["is_active"]:
            abort(404)
        name = request.form.get("buyer_name", "").strip()[:120]
        email = request.form.get("buyer_email", "").strip().lower()[:254]
        try:
            quantity = int(request.form.get("quantity", "1"))
        except ValueError:
            quantity = 0
        if not name or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or not 1 <= quantity <= 8:
            flash("Enter a name, a valid email address and a ticket quantity from 1 to 8.", "error")
            return redirect(url_for("event_detail", event_id=event_id))

        order_ref = "EVT-" + secrets.token_hex(5).upper()
        order_key = secrets.token_urlsafe(32)
        db = g.db
        try:
            db.execute("BEGIN IMMEDIATE")
            locked_event = db.execute("SELECT * FROM events WHERE id=? AND is_active=1", (event_id,)).fetchone()
            if locked_event is None:
                db.rollback()
                abort(404)
            sold = db.execute(
                "SELECT COUNT(*) FROM tickets t JOIN orders o ON o.id=t.order_id WHERE t.event_id=? AND o.status='demo_paid'",
                (event_id,),
            ).fetchone()[0]
            if locked_event["capacity"] - sold < quantity:
                db.rollback()
                flash("There are not enough tickets left for that quantity. Please choose fewer tickets.", "error")
                return redirect(url_for("event_detail", event_id=event_id))
            total = locked_event["price"] * quantity
            cursor = db.execute(
                """INSERT INTO orders(reference, buyer_name, buyer_email, total_amount, currency,
                                      status, access_hash, payment_reference)
                   VALUES (?, ?, ?, ?, ?, 'demo_paid', ?, ?)""",
                (
                    order_ref,
                    name,
                    email,
                    total,
                    locked_event["currency"],
                    hashlib.sha256(order_key.encode()).hexdigest(),
                    "DEMO-" + secrets.token_hex(4).upper(),
                ),
            )
            order_id = cursor.lastrowid
            for _ in range(quantity):
                db.execute(
                    "INSERT INTO tickets(public_id, order_id, event_id, attendee_name) VALUES (?, ?, ?, ?)",
                    (secrets.token_hex(16), order_id, event_id, name),
                )
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        return redirect(url_for("order_view", reference=order_ref, key=order_key))

    @app.get("/orders/<reference>")
    def order_view(reference: str):
        order = g.db.execute("SELECT * FROM orders WHERE reference=?", (reference,)).fetchone()
        if order is None:
            abort(404)
        key = request.args.get("key", "")
        supplied_hash = hashlib.sha256(key.encode()).hexdigest() if key else ""
        if not key or not hmac.compare_digest(supplied_hash, order["access_hash"]):
            abort(403, description="Use the private order link shown after checkout to view these tickets.")
        tickets = g.db.execute(
            """SELECT t.*, e.title AS event_title, e.venue, e.city, e.starts_at
               FROM tickets t JOIN events e ON e.id=t.event_id WHERE t.order_id=? ORDER BY t.id""",
            (order["id"],),
        ).fetchall()
        prepared = [{**dict(ticket), "token": make_ticket_token(ticket["public_id"]),
                    "qr": qr_data_uri(make_ticket_token(ticket["public_id"]))} for ticket in tickets]
        event = g.db.execute("SELECT * FROM events WHERE id=?", (tickets[0]["event_id"],)).fetchone() if tickets else None
        return render_template("order.html", order=order, tickets=prepared, event=event)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = g.db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not user or not check_password_hash(user["password_hash"], password):
                flash("The email or password was not recognized.", "error")
                return render_template("login.html"), 401
            session.clear()
            session["user_id"] = user["id"]
            session["email"] = user["email"]
            session["role"] = user["role"]
            session["csrf_token"] = secrets.token_urlsafe(24)
            session.permanent = True
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("admin" if user["role"] == "admin" else "gate")
            return redirect(target)
        return render_template("login.html")

    @app.post("/logout")
    @login_required()
    def logout():
        session.clear()
        flash("You are signed out.", "success")
        return redirect(url_for("index"))

    @app.route("/admin", methods=["GET", "POST"])
    @login_required("admin")
    def admin():
        if request.method == "POST":
            title = request.form.get("title", "").strip()[:140]
            description = request.form.get("description", "").strip()[:2000]
            venue = request.form.get("venue", "").strip()[:180]
            city = request.form.get("city", "").strip()[:100]
            starts_at = request.form.get("starts_at", "").strip()
            currency = request.form.get("currency", "NGN").strip().upper()
            try:
                price, capacity = int(request.form.get("price", "-1")), int(request.form.get("capacity", "0"))
                datetime.strptime(starts_at, "%Y-%m-%dT%H:%M")
            except ValueError:
                price, capacity = -1, 0
            if not all((title, description, venue, city, starts_at)) or price < 0 or capacity < 1 or currency not in {"NGN", "USD", "GBP", "EUR", "GHS", "KES"}:
                flash("Complete every field with a valid date, non-negative price and capacity above zero.", "error")
            else:
                g.db.execute(
                    """INSERT INTO events(title, description, venue, city, starts_at, price, currency, capacity)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (title, description, venue, city, starts_at, price, currency, capacity),
                )
                flash("Event published. It is now available on the public listing.", "success")
                return redirect(url_for("admin"))
        events = g.db.execute(
            """SELECT e.*, (SELECT COUNT(*) FROM tickets t JOIN orders o ON o.id=t.order_id
                              WHERE t.event_id=e.id AND o.status='demo_paid') AS sold
               FROM events e ORDER BY e.starts_at ASC"""
        ).fetchall()
        orders = g.db.execute(
            """SELECT o.*, COUNT(t.id) AS ticket_count, e.title AS event_title
               FROM orders o JOIN tickets t ON t.order_id=o.id JOIN events e ON e.id=t.event_id
               GROUP BY o.id ORDER BY o.id DESC LIMIT 12"""
        ).fetchall()
        totals = {
            "events": g.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "tickets": g.db.execute("SELECT COUNT(*) FROM tickets WHERE status != 'cancelled'").fetchone()[0],
            "checkins": g.db.execute("SELECT COUNT(*) FROM checkin_log WHERE outcome='accepted'").fetchone()[0],
            "orders": g.db.execute("SELECT COUNT(*) FROM orders WHERE status='demo_paid'").fetchone()[0],
        }
        return render_template("admin.html", events=events, orders=orders, totals=totals, starts_default=_now_local_iso())

    @app.post("/admin/events/<int:event_id>/toggle")
    @login_required("admin")
    def toggle_event(event_id: int):
        event = g.db.execute("SELECT id, is_active FROM events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            abort(404)
        g.db.execute("UPDATE events SET is_active=? WHERE id=?", (0 if event["is_active"] else 1, event_id))
        flash("Event visibility updated.", "success")
        return redirect(url_for("admin"))

    @app.get("/gate")
    @login_required("admin", "gate")
    def gate():
        events = g.db.execute("SELECT id, title, starts_at, venue, city FROM events WHERE is_active=1 ORDER BY starts_at").fetchall()
        return render_template("gate.html", events=events)

    @app.post("/api/gate/validate")
    @login_required("admin", "gate")
    def validate_ticket():
        payload = request.get_json(silent=True) or {}
        token = str(payload.get("ticket", "")).strip()[:256]
        try:
            event_id = int(payload.get("event_id", 0))
        except (TypeError, ValueError):
            event_id = 0
        db = g.db
        db.execute("BEGIN IMMEDIATE")
        try:
            event = db.execute("SELECT id, title FROM events WHERE id=?", (event_id,)).fetchone()
            if event is None:
                db.rollback()
                return jsonify(ok=False, outcome="invalid_event", message="Choose a valid event before scanning."), 400
            public_id = verify_ticket_token(token)
            if public_id is None:
                db.execute(
                    "INSERT INTO checkin_log(event_id, ticket_id, staff_user_id, outcome) VALUES (?, NULL, ?, 'invalid')",
                    (event_id, session["user_id"]),
                )
                db.commit()
                return jsonify(ok=False, outcome="invalid", message="Invalid or damaged ticket QR."), 200
            ticket = db.execute(
                """SELECT t.*, o.status AS order_status FROM tickets t JOIN orders o ON o.id=t.order_id
                   WHERE t.public_id=?""",
                (public_id,),
            ).fetchone()
            if ticket is None or ticket["event_id"] != event_id or ticket["order_status"] != "demo_paid":
                db.execute(
                    "INSERT INTO checkin_log(event_id, ticket_id, staff_user_id, outcome) VALUES (?, ?, ?, 'invalid')",
                    (event_id, ticket["id"] if ticket else None, session["user_id"]),
                )
                db.commit()
                return jsonify(ok=False, outcome="invalid", message="Ticket is not valid for this event."), 200
            if ticket["status"] != "valid":
                db.execute(
                    "INSERT INTO checkin_log(event_id, ticket_id, staff_user_id, outcome) VALUES (?, ?, ?, 'duplicate')",
                    (event_id, ticket["id"], session["user_id"]),
                )
                db.commit()
                used_at = ticket["used_at"]
                return jsonify(ok=False, outcome="duplicate", message="DUPLICATE — this ticket has already been used.",
                               used_at=used_at, attendee=ticket["attendee_name"], event=event["title"]), 200
            changed = db.execute(
                """UPDATE tickets SET status='used', used_at=CURRENT_TIMESTAMP, used_by=?
                   WHERE id=? AND status='valid'""",
                (session["user_id"], ticket["id"]),
            ).rowcount
            if changed != 1:
                db.rollback()
                return jsonify(ok=False, outcome="duplicate", message="DUPLICATE — this ticket has already been used."), 200
            db.execute(
                "INSERT INTO checkin_log(event_id, ticket_id, staff_user_id, outcome) VALUES (?, ?, ?, 'accepted')",
                (event_id, ticket["id"], session["user_id"]),
            )
            db.commit()
            return jsonify(ok=True, outcome="accepted", message="VALID — entry approved.",
                           attendee=ticket["attendee_name"], event=event["title"]), 200
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise

    @app.get("/health")
    def health():
        return jsonify(status="ok", service="event-ticketing", database="connected")

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def friendly_error(error):
        return render_template("error.html", code=error.code, message=getattr(error, "description", "Something went wrong.")), error.code

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
