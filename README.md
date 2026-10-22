# Topic 77 — Event Ticketing System with QR-Based Duplicate Prevention

A runnable event-ticketing web app: organizers publish events, guests complete an explicitly simulated checkout and receive unique QR tickets, and authenticated gate staff validate each ticket once. A repeat scan is recorded and rejected.

## Devlify

An academic project in the [Devlify GitHub collection](https://github.com/devlify-marketplace).

[Visit Devlify](https://devlify.com.ng) · [Browse the marketplace](https://devlify.com.ng/marketplace)

## Requirements

- Python 3.10+
- `pip`
- A modern browser for the UI. Camera scanning needs HTTPS (or `localhost`) and a browser exposing the native `BarcodeDetector` API. USB/Bluetooth QR readers that type the decoded value, or manual paste, work as a fallback.

## Run locally

```bash
cd /workspace/topic-77-event-ticketing-qr-duplicate-prevention
./run.sh
```

Open <http://127.0.0.1:5000>. `run.sh` creates `.venv`, installs the small requirements list and launches the app. Alternatively:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The first run creates `instance/ticketing.sqlite3`, a private persistent signing key at `instance/.secret_key`, two demo staff users and one sample event. The sample event date is seeded 45 days from the first launch. The included event photograph and QR generation run locally; the web UI does not need a remote asset host.

### Demo staff access

| Role | Email | Password |
|---|---|---|
| Organizer | `admin@example.com` | `TicketDemo!77` |
| Gate staff | `gate@example.com` | `GateDemo!77` |

These are **development-only** defaults. For a fresh database, set `ADMIN_PASSWORD`, `GATE_PASSWORD`, and a long random `SECRET_KEY` before starting. The seeded passwords are only applied when the database is first created; changing environment values does not rewrite existing account hashes. Change these accounts in production before exposing the app.

## Try the end-to-end flow

1. On the discovery page, open the seeded event, enter a name/email, and choose 1–8 tickets.
2. Submit the demo checkout. Each ticket gets a different signed QR image, attendee details and a private order link. Print or save the page; copy the QR code value with a scanner or use camera scan.
3. Sign in as gate staff and select the same event. Click **Use camera** (where supported), scan the QR, or paste/type the decoded ticket code.
4. The first scan returns **VALID — entry approved**. Scan the same QR again: it returns **DUPLICATE**. Scan attempts and accepted entries are visible in the database-backed audit log; the organizer dashboard shows totals.
5. Sign in as organizer to publish more events, hide/unhide a listing, and review recent orders and ticket totals.

## Duplicate-prevention design

- Ticket IDs are generated from 128 bits of cryptographic randomness. The QR value is `ticket_id.HMAC-SHA256`; the signature is checked in constant time and QR values are not stored as plaintext in the database. Keep the persistent app signing key private and stable: changing it invalidates existing QR codes.
- The gate handler opens a SQLite `BEGIN IMMEDIATE` transaction, checks the selected event, then conditionally changes `tickets.status` from `valid` to `used` with one SQL `UPDATE ... WHERE status='valid'`. Only the request that changes one row can return accepted. A repeat (including simultaneous scans) observes the used state and is rejected.
- Scan audit rows record accepted, duplicate and invalid results and the staff user. Raw QR tokens are never written to the scan log.
- Seat capacity is also checked under a write transaction before tickets are issued, preventing concurrent demo orders from overselling the event.
- Passwords use Werkzeug's scrypt password hashing. Staff routes are role protected, mutating forms use session-bound CSRF tokens, session cookies are HttpOnly/SameSite, and database queries are parameterized.

## Tests

```bash
python3 -m pip install -r requirements.txt pytest
python3 -m pytest -q
```

Automated tests use a temporary SQLite database and cover public discovery, completed demo checkout, per-ticket QR output, protected order links, capacity enforcement (including two simultaneous buyers competing for the last ticket), invalid tokens, wrong-event validation, successful/duplicate scans, and concurrent repeated scans.

Health probe: `GET /health` returns JSON for a simple local readiness check.

## Important scope and deployment notes

This is a fully runnable academic/demo build, **not connected to a payment processor or email/SMS provider**. Checkout is deliberately labeled demo and marks orders `demo_paid`; it collects no card data and never charges money. Add a real payment gateway with signed webhook confirmation before treating paid orders as real sales. The order link is a bearer secret; share it privately. No email is sent automatically.

SQLite with write locking is appropriate for local demos and small single-host deployments. For higher-concurrency production use, migrate to PostgreSQL and a managed deployment, add staff account management, password reset/MFA, rate limiting, HTTPS, monitoring, backup/restore, privacy retention and reconciliation, payment webhooks, email delivery, event cancellation/refunds and operational runbooks. Set `COOKIE_SECURE=1` behind HTTPS. Do not use the Flask development server as a public production server.

## Project map

- `app.py` — Flask routes, SQLite schema/transactions, HMAC QR tokens and role checks.
- `templates/` — public listing, event page, order/ticket, login, dashboard, gate scan and errors.
- `static/style.css`, `static/app.js` — responsive UI and native browser camera/keyboard scan support.
- `tests/test_ticketing.py` — end-to-end and concurrency-oriented tests.
- `docs/SYSTEM_DESIGN.md` — workflow, data model and project evaluation notes.
- `static/images/live-event.jpg` — bundled local illustrative event image.
