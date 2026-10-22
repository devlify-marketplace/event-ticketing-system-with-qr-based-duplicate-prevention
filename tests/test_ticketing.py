import hashlib
import hmac
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import create_app


@pytest.fixture()
def system():
    tmp = tempfile.TemporaryDirectory(prefix="topic77-")
    db_path = str(Path(tmp.name) / "test.sqlite3")
    app = create_app({
        "TESTING": True,
        "DATABASE": db_path,
        "SECRET_KEY": "test-only-secret-key-for-ticket-signatures",
        "ADMIN_PASSWORD": "admin-test-password",
        "GATE_PASSWORD": "gate-test-password",
        "SESSION_COOKIE_SECURE": False,
    })
    yield app, db_path
    tmp.cleanup()


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("csrf_token", "")


def sign_in_as(client, db_path, role="gate"):
    connection = sqlite3.connect(db_path)
    email = "admin@example.com" if role == "admin" else "gate@example.com"
    user_id = connection.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    connection.close()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["email"] = email
        sess["role"] = role
        sess["csrf_token"] = f"csrf-{role}-test"


def issue_order(client, event_id=1, quantity=1):
    client.get(f"/events/{event_id}")  # initialize CSRF token
    return client.post(
        f"/events/{event_id}/checkout",
        data={
            "csrf_token": csrf(client),
            "buyer_name": "Amina Guest",
            "buyer_email": "amina@example.com",
            "quantity": str(quantity),
        },
        follow_redirects=False,
    )


def load_ticket_id(db_path):
    connection = sqlite3.connect(db_path)
    row = connection.execute("SELECT public_id FROM tickets ORDER BY id LIMIT 1").fetchone()
    connection.close()
    return row[0]


def token_for(app, public_id):
    signature = hmac.new(
        app.config["SECRET_KEY"].encode(),
        f"ticket:{public_id}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{public_id}.{signature}"


def validate(client, token, event_id=1):
    return client.post(
        "/api/gate/validate",
        json={"ticket": token, "event_id": event_id},
        headers={"X-CSRF-Token": csrf(client)},
    )


def test_public_discovery_shows_seed_event(system):
    app, _ = system
    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"Lagos Afterglow Live" in response.data
    assert b"YOUR CITY. YOUR PEOPLE." in response.data


def test_checkout_issues_multiple_unique_qr_tickets_and_private_order_page(system):
    app, db_path = system
    client = app.test_client()
    response = issue_order(client, quantity=2)
    assert response.status_code == 302
    assert "/orders/EVT-" in response.headers["Location"]
    order_page = client.get(response.headers["Location"])
    assert order_page.status_code == 200
    assert order_page.data.count(b"data:image/png;base64,") == 2
    assert b"Demo order confirmed" in order_page.data
    assert b"Amina Guest" in order_page.data
    connection = sqlite3.connect(db_path)
    rows = connection.execute("SELECT public_id, status FROM tickets ORDER BY id").fetchall()
    connection.close()
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert {row[1] for row in rows} == {"valid"}


def test_order_link_requires_the_bearer_key(system):
    app, _ = system
    client = app.test_client()
    response = issue_order(client)
    private_url = response.headers["Location"]
    path_only = private_url.split("?", 1)[0]
    assert client.get(path_only).status_code == 403
    assert client.get(private_url).status_code == 200


def test_first_scan_is_accepted_and_second_scan_is_rejected_and_logged(system):
    app, db_path = system
    client = app.test_client()
    sign_in_as(client, db_path)
    issue_order(client)
    token = token_for(app, load_ticket_id(db_path))
    first = validate(client, token).get_json()
    second = validate(client, token).get_json()
    assert first["outcome"] == "accepted"
    assert second["outcome"] == "duplicate"
    assert "already been used" in second["message"]
    connection = sqlite3.connect(db_path)
    status = connection.execute("SELECT status FROM tickets").fetchone()[0]
    outcomes = [r[0] for r in connection.execute("SELECT outcome FROM checkin_log ORDER BY id")]
    connection.close()
    assert status == "used"
    assert outcomes == ["accepted", "duplicate"]


def test_invalid_signature_and_wrong_event_are_never_accepted(system):
    app, db_path = system
    client = app.test_client()
    sign_in_as(client, db_path)
    issue_order(client)
    public_id = load_ticket_id(db_path)
    invalid = validate(client, public_id + "." + "0" * 64).get_json()
    assert invalid["outcome"] == "invalid"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO events(title, description, venue, city, starts_at, price, currency, capacity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("Other event", "Test", "Hall", "Lagos", "2030-01-01T18:00", 1000, "NGN", 5),
    )
    connection.commit()
    connection.close()
    wrong_event = validate(client, token_for(app, public_id), event_id=2).get_json()
    assert wrong_event["outcome"] == "invalid"
    connection = sqlite3.connect(db_path)
    status = connection.execute("SELECT status FROM tickets").fetchone()[0]
    connection.close()
    assert status == "valid"


def test_capacity_is_enforced_before_order_commit(system):
    app, db_path = system
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE events SET capacity=1 WHERE id=1")
    connection.commit()
    connection.close()
    client = app.test_client()
    response = issue_order(client, quantity=2)
    assert response.status_code == 302
    assert "/events/1" in response.headers["Location"]
    connection = sqlite3.connect(db_path)
    count = connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    connection.close()
    assert count == 0


def test_two_simultaneous_checkouts_only_sell_the_last_ticket_once(system):
    app, db_path = system
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE events SET capacity=1 WHERE id=1")
    connection.commit()
    connection.close()
    barrier = threading.Barrier(2)

    def buyer_request(buyer_number):
        # Each buyer has an independent session and a real, session-bound CSRF token.
        client = app.test_client()
        assert client.get("/events/1").status_code == 200
        csrf_token = csrf(client)
        assert csrf_token
        name = f"Buyer {buyer_number}"
        email = f"buyer{buyer_number}@example.com"
        barrier.wait(timeout=3)
        response = client.post(
            "/events/1/checkout",
            data={
                "csrf_token": csrf_token,
                "buyer_name": name,
                "buyer_email": email,
                "quantity": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        page = client.get(location)
        assert page.status_code == 200
        return name, email, location, page.data

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(buyer_request, range(2)))

    winners = [result for result in results if result[2].startswith("/orders/EVT-")]
    losers = [result for result in results if result[2] == "/events/1"]
    assert len(winners) == 1
    assert len(losers) == 1
    name, email, _location, order_page = winners[0]
    assert b"Demo order confirmed" in order_page
    assert name.encode() in order_page
    assert b"There are not enough tickets left for that quantity." in losers[0][3]

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
        ticket = connection.execute(
            """SELECT o.buyer_name, o.buyer_email, o.status,
                      t.event_id, t.attendee_name, t.status
               FROM tickets t JOIN orders o ON o.id=t.order_id"""
        ).fetchone()
    finally:
        connection.close()
    assert ticket == (name, email, "demo_paid", 1, name, "valid")


def test_gate_and_admin_pages_require_staff_roles(system):
    app, db_path = system
    client = app.test_client()
    assert client.get("/gate").status_code == 302
    assert client.get("/admin").status_code == 302
    sign_in_as(client, db_path, "gate")
    assert client.get("/gate").status_code == 200
    assert client.get("/admin").status_code == 403


def test_two_simultaneous_scans_only_admit_one(system):
    app, db_path = system
    issue_order(app.test_client())
    ticket_token = token_for(app, load_ticket_id(db_path))
    barrier = threading.Barrier(2)

    def scanner_request():
        client = app.test_client()
        sign_in_as(client, db_path)
        barrier.wait(timeout=3)
        return validate(client, ticket_token).get_json()["outcome"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _n: scanner_request(), range(2)))
    assert sorted(outcomes) == ["accepted", "duplicate"]
    connection = sqlite3.connect(db_path)
    count = connection.execute("SELECT COUNT(*) FROM checkin_log WHERE outcome='accepted'").fetchone()[0]
    connection.close()
    assert count == 1


def test_organizer_login_publish_and_hide_event(system):
    app, _db_path = system
    client = app.test_client()
    login_page = client.get("/login")
    assert login_page.status_code == 200
    response = client.post(
        "/login",
        data={"csrf_token": csrf(client), "email": "admin@example.com", "password": "admin-test-password"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin")
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    response = client.post(
        "/admin",
        data={
            "csrf_token": csrf(client),
            "title": "Community Jazz Night",
            "description": "An evening of local jazz.",
            "venue": "The Green Room",
            "city": "Lagos",
            "starts_at": "2030-11-20T19:00",
            "price": "5000",
            "currency": "NGN",
            "capacity": "80",
        },
    )
    assert response.status_code == 302
    assert b"Community Jazz Night" in client.get("/").data
    event_id = 2
    response = client.post(f"/admin/events/{event_id}/toggle", data={"csrf_token": csrf(client)})
    assert response.status_code == 302
    assert b"Community Jazz Night" not in client.get("/").data
    assert client.get(f"/events/{event_id}").status_code == 404
