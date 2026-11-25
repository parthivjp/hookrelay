# HookRelay

A service that delivers webhooks reliably. Think of how Stripe or GitHub send events to your server. HookRelay is the sending side.

Your app says "an order was created". HookRelay makes sure every customer who cares about it gets that message, even when their server is slow or down.

**Stack:** Python, FastAPI, PostgreSQL, Redis, Celery, Docker

## What problems does it solve?

| Problem | How HookRelay solves it |
|---|---|
| Customer server is down or slow | Retries with exponential backoff (10s, 20s, 40s ...) plus a little random jitter |
| It keeps failing forever | After 6 tries the delivery goes to a dead-letter list (`status=dead`) so a human can look at it |
| Customer fixed their server | Replay a delivery with one API call |
| Two workers pick the same job | One atomic `UPDATE` decides the winner, so the customer gets one attempt, not two |
| A worker crashes in the middle | Every job has a 60 second lease. When it runs out, another worker takes the job |
| Redis is down when an event arrives | The event is already saved in PostgreSQL. A sweeper job finds it later. Nothing is lost |
| Your own client retries a request | `Idempotency-Key` header: same key returns the old result, no duplicate event |
| Customer needs to trust the message | Every request is signed with HMAC-SHA256 and a timestamp (stops fake and replayed messages) |
| Slow customer server blocks workers | 5 second timeout on every request |
| Dashboard polls stats all the time | `/stats` is cached in Redis for 10 seconds |

## How it works

```
 your app --POST /events--> [ FastAPI ] --saves--> [ PostgreSQL ]
                                 |                      ^
                                 | queue job            | claim / update status
                                 v                      |
                            [ Redis ] ------> [ Celery worker ] --signed POST--> customer URL
                                 ^
                                 |  every 10s: "anything due or lost?"
                            [ Celery beat sweeper ]
```

1. A customer registers an endpoint (a URL plus which event types they want).
2. You send an event. HookRelay creates one **delivery** row for each matching endpoint.
3. A worker claims the delivery, sends the signed request, and saves the result.
4. Success: done. Failure: wait and retry. Too many failures: dead-letter.

PostgreSQL is the source of truth. Redis and Celery are only the "go now" signal. That is why a Redis crash cannot lose data.

## Run it

You need Docker.

```bash
docker compose up --build
```

### Run locally on Windows without Docker

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DATABASE_URL = "sqlite:///./hookrelay.db"
$env:CACHE_ENABLED = "false"
$env:API_KEY = "dev-key"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In separate terminals, start the demo receiver and local delivery worker:

```powershell
$env:WEBHOOK_SECRET = "whsec_demo"
$env:FAIL_RATE = "0"
.\.venv\Scripts\python.exe -m uvicorn demo_receiver.main:app --host 127.0.0.1 --port 9000
```

```powershell
$env:DATABASE_URL = "sqlite:///./hookrelay.db"
.\.venv\Scripts\python.exe -m app.local_worker
```

- API docs (try everything in the browser): http://localhost:8000/docs
- API key: `dev-key` (send it as the `X-API-Key` header)

Then, in a second terminal:

```bash
./scripts/demo.sh
```

This registers a demo receiver that **fails half the time**, sends 5 events, and shows the stats. Watch the worker logs with `docker compose logs -f worker demo-receiver` to see retries happen.

## API

| Method | Path | What it does |
|---|---|---|
| POST | `/endpoints` | Add a customer URL (returns the secret once) |
| GET | `/endpoints`, `/endpoints/{id}` | List or read |
| PATCH | `/endpoints/{id}` | Change URL, event types, or turn on/off |
| POST | `/endpoints/{id}/rotate-secret` | Make a new signing secret |
| DELETE | `/endpoints/{id}` | Delete |
| POST | `/events` | Send an event (use the `Idempotency-Key` header) |
| GET | `/events`, `/events/{id}` | List events, or one event with all its deliveries |
| GET | `/deliveries?status=dead` | List deliveries, filter by status or endpoint |
| POST | `/deliveries/{id}/replay` | Send a delivery again |
| GET | `/stats` | Counts and success rate |
| GET | `/health` | Health check (no key needed) |

Event type patterns: `*` (everything), `order.*` (prefix), `order.created` (exact).

## How a customer checks the signature

Each request has the header `X-HookRelay-Signature: t=<timestamp>,v1=<hash>`.
The hash is `HMAC-SHA256(secret, "<timestamp>.<raw body>")`. Reject it if the hash does not match or the timestamp is older than 5 minutes. A working example is in `demo_receiver/main.py`.

## Run the tests

```bash
pip install -r requirements.txt
pytest
```

18 tests, no Docker needed (they use SQLite and a fake HTTP client). They cover signatures, backoff, idempotency, retries, dead-letter, replay, two workers racing for one job, crash recovery, and the queue being down.

## Project layout

```
app/
  main.py        API routes
  delivery.py    the important part: claim, send, retry, dead-letter
  worker.py      Celery tasks and the sweeper
  security.py    signing, verifying, API key
  models.py      Endpoint, Event, Delivery tables
  cache.py       Redis cache for /stats
demo_receiver/   fake customer server that fails randomly
tests/
```

## Known limits (good to say honestly in an interview)

- Delivery is **at least once**. A customer can get the same event twice (for example, they answered OK but our worker crashed before saving). That is why every request has an `X-HookRelay-Event-Id` for the customer to de-duplicate. Exactly-once is not possible over a network.
- Events can arrive out of order when an earlier one is being retried.
- Tables are created on startup. A real system would use Alembic migrations.
- Not done yet: blocking private IP addresses (SSRF protection), per-customer rate limits, per-user API keys.

## Resume lines you can use

- Built HookRelay, a webhook delivery service (FastAPI, PostgreSQL, Redis, Celery, Docker) with exponential-backoff retries, a dead-letter queue and one-call replay.
- Prevented duplicate deliveries with an atomic claim query and a lease-based crash recovery, and made event intake idempotent with `Idempotency-Key`.
- Signed every request with HMAC-SHA256 and a timestamp; wrote 18 automated tests covering race conditions and failure recovery.
