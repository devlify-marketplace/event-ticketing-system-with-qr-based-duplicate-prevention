# System Design — Topic 77

## 1. Project summary

**Title:** Event Ticketing System with QR-Based Duplicate Prevention  
**Goal:** Sell event tickets through a simple online flow and prevent a valid ticket from authorizing more than one gate entry.

The runnable prototype contains a public event directory, event checkout, private order/ticket pages, organizer console, staff sign-in, event-specific gate validation, and a durable check-in audit trail.

## 2. Objectives

1. Publish and manage active events with venue, time, capacity, price and sales visibility.
2. Let a guest create an order and receive one distinct signed QR ticket per attendee seat.
3. Validate tickets against both signature and selected event at the gate.
4. Atomically transition each ticket from `valid` to `used`, ensuring concurrent attempts have a single winner.
5. Preserve staff identity, scan result and timestamp for accepted, duplicate and invalid scans.
6. Enforce event capacity during concurrent checkouts.

## 3. Roles and use cases

| Role | Actions |
|---|---|
| Guest | Browse events, submit demo checkout, view/print ticket QR codes using the private order link. |
| Organizer | Sign in, publish events, hide or republish a listing, review order and check-in totals. |
| Gate staff | Sign in, select an event, use device camera / QR reader / paste, admit a first valid scan, reject duplicate/invalid/wrong-event tickets. |

## 4. Functional requirements

- Public discovery shows only active events and remaining capacity.
- Checkout accepts a guest name, email and 1–8 tickets per order.
- No ticket is issued when an event is hidden or remaining capacity is insufficient.
- Every ticket has a random public ID and authenticated HMAC-SHA-256 signature.
- QR scans are validated for correct format, signature, event ownership, paid/demo order and ticket status.
- The gate result is immediate and auditable; duplicate attempts never revert a used ticket.
- Organizer actions require the admin role; the gate requires an admin or gate role.
- Mutating POST requests require a session-bound CSRF token.

## 5. Architecture

```text
Guest browser ─┐
Organizer UI ───┼── HTTPS / Flask routes ── SQLite
Gate device ───┘       │                    ├─ events and capacity
                       ├─ HMAC QR signer    ├─ orders and tickets
                       ├─ local QR renderer └─ staff and scan audit
                       └─ native BarcodeDetector / keyboard wedge
```

The app is intentionally monolithic for a portable academic demonstration. Flask templates render the interface, JavaScript uses the browser's native QR `BarcodeDetector` where available, and the small `qrcode` Python package creates PNG ticket codes locally. A keyboard-wedge USB/Bluetooth reader behaves like a keyboard, so it works even where native camera scanning is unavailable.

## 6. Data model

- **users** — unique email, scrypt password hash, role.
- **events** — title, description, venue, city, start time, currency/price, capacity and publication status.
- **orders** — unique public reference, purchaser contact, total, demo-payment state, payment reference and SHA-256 order-link key hash.
- **tickets** — random public ID, order/event references, attendee, status, used time and staff user.
- **checkin_log** — event, optional ticket, staff user, normalized outcome, timestamp. Raw QR bearer tokens are not stored.

## 7. Ticket integrity and concurrency

The QR payload is `public_id.signature`. The signature is HMAC-SHA-256 over a domain-separated `ticket:<public_id>` message with the persistent app secret. Random 128-bit IDs prevent practical guessing; constant-time comparison prevents signature timing leaks. The database stores the ID and lifecycle state, not the bearer QR value.

Gate validation uses `BEGIN IMMEDIATE` to serialize SQLite writers. Within that transaction the app verifies the event and ticket, updates only `WHERE status='valid'`, records the outcome, and commits. Exactly one concurrent request can change the status and return `accepted`; subsequent requests see `used` and return `duplicate`. Seat capacity is counted and checked inside a write transaction before order/tickets are created.

## 8. Non-functional and security considerations

- Passwords: Werkzeug scrypt hashes; never stored or logged in plaintext.
- Session: HttpOnly, SameSite=Lax; enable Secure cookies behind HTTPS.
- CSRF: POST forms and gate JSON endpoint validate a per-session token.
- Input handling: bounded lengths, basic email and numeric validation, SQL parameter binding, event/role checks.
- QR privacy: order page is protected by a high-entropy bearer key; keep its URL private.
- Availability: health endpoint at `/health`; SQLite busy timeout is 10 seconds.
- Data management: back up the SQLite database and signing secret together; restoring one without the other can break existing ticket validation.

## 9. Test and evaluation plan

The pytest suite runs isolated temporary databases and checks:

- Active event discovery and the purchase/order flow.
- Multiple unique ticket IDs and rendered QR image data.
- Private order-link access controls.
- Capacity exhaustion prevents ticket creation.
- Valid first scan, repeat duplicate rejection and durable audit entries.
- Bad signature and wrong-event rejection.
- Gate/admin role boundaries.
- Two simultaneous scans of one ticket produce exactly one accepted result.

For a manual demo, issue one ticket, scan once (green/accepted), then scan it again (amber/duplicate). Repeat with a different event selection to demonstrate event binding.

## 10. Scope boundaries and future work

Checkout is intentionally simulated. It does not process cards or external payments and does not send order emails/SMS. Before commercial launch, add a payment provider with signed webhooks, email delivery, refunds/cancellation, ticket transfer policy, organizer account lifecycle, rate limiting and abuse monitoring. For larger venues and multiple check-in nodes, move to PostgreSQL and use a transactionally guarded update/unique check-in record; plan network outage handling and offline reconciliation explicitly.
