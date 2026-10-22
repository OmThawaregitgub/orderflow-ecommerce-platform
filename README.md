# OrderFlow — Reliable E-Commerce Order & Inventory Platform

OrderFlow is a full-stack Python application that demonstrates production-oriented backend engineering patterns through an order and inventory workflow. It includes a responsive operations console, a versioned JSON API, database-backed inventory controls, idempotent order creation, an audit trail, and a transactional outbox worker.

The project is designed to run locally on SQLite with a single command sequence, while supporting PostgreSQL, Docker Compose, Alembic migrations, and a separate background worker for deployment.

> **Portfolio project:** Payment processing, email delivery, shipment carrier integration, and external fulfillment are simulated. This is not an Amazon product and is not affiliated with Amazon.

## What it does

- **Operations dashboard:** order activity over the last seven days, gross order value, pending order count, low-stock alerts, recent orders, and outbox queue visibility.
- **Inventory administration:** create and deactivate products, filter/search the catalog, restock products, enforce low-stock thresholds, and view a recent inventory movement audit trail.
- **Order lifecycle:** create orders from the UI or JSON API; inspect line-item snapshots and totals; move orders through pending → processing → shipped; cancel eligible orders and return their reserved stock.
- **Correctness under retries:** every order creation request uses an idempotency key. Repeating a key with the same request returns the existing order without reserving stock again; reusing that key with a different payload is rejected.
- **Atomic stock reservation:** stock is decremented using a conditional database update inside the order transaction. A failed reservation rolls back the order, inventory movement, and outbox event together.
- **Background processing:** a transactional outbox records domain events in the same commit as the business write. A separate worker claims events, retries transient failures with exponential backoff, tracks failed events, and reclaims expired worker leases.
- **Security baseline:** signed sessions, PBKDF2-SHA256 password hashing, CSRF validation for browser/API mutations, server-side validation, authenticated operations routes, and production checks for a strong secret key.
- **Developer workflow:** pytest suite, Alembic schema migration, Dockerfile, PostgreSQL Docker Compose stack, health/readiness endpoints, and GitHub Actions CI.

## UI preview

The UI uses a restrained operations-console style: fixed workspace navigation, responsive layouts, small metric cards, activity bars, order-status badges, inventory alerts, dialog-based product creation, and order details with lifecycle status.

## Tech stack

| Area | Technology |
|---|---|
| Language | Python 3.10+ (CI uses Python 3.12) |
| Web framework | FastAPI + Uvicorn |
| Data layer | SQLAlchemy 2.x |
| Database | SQLite locally; PostgreSQL supported |
| Schema evolution | Alembic |
| UI | Jinja2 templates, semantic HTML, custom CSS, vanilla JavaScript |
| Sessions | Starlette signed-cookie sessions |
| Password hashing | PBKDF2-HMAC-SHA256 (stdlib) |
| Background work | Database-backed transactional outbox and a separate Python worker process |
| Validation / testing | Pydantic 2, pytest, HTTPX TestClient |
| Deployment | Docker, Docker Compose, GitHub Actions |

The worker intentionally uses a durable database outbox rather than requiring a Redis/Celery server for the core demo. In a larger deployment, the outbox can publish to SQS, RabbitMQ, or Redis-backed workers without moving the order write out of its transaction.

## Architecture

```mermaid
flowchart TD
    Browser[Operations UI] --> Web[FastAPI / Jinja2]
    Client[Authenticated API Client] --> API[Versioned JSON API]
    API --> Services[Order and Inventory Services]
    Web --> Services
    Services --> DB[(SQL database)]
    Services --> Outbox[Transactional outbox event]
    Outbox --> Worker[Separate outbox worker]
    Worker --> DB
    Worker --> Logs[Application logs]
```

### Create-order transaction

1. Validate the request and aggregate repeated product lines by product ID.
2. Resolve the idempotency key. If the same request was already committed, return the original order. If the payload differs, return a conflict.
3. Insert the order record, then conditionally decrement stock only when the product is active and enough units remain.
4. Store immutable product name/SKU/price snapshots in order lines and write inventory movements.
5. Write `order.created` to the outbox in the same transaction and commit everything together.
6. If any reservation or insert fails, roll back all writes.
7. The worker claims the outbox event and advances a still-pending order to processing. An operator can then mark it shipped.

### Important design decisions

- **Modular monolith:** the HTTP app, services, and data models are separated by responsibility without premature microservice boundaries.
- **Database-side inventory decrement:** `UPDATE products SET stock = stock - :quantity WHERE id = :id AND is_active = true AND stock >= :quantity RETURNING stock` avoids the usual read-then-write overselling bug on SQLite and PostgreSQL.
- **Idempotency fingerprint:** a SHA-256 request fingerprint ensures an idempotency key cannot silently be reused for a different order payload.
- **Transactional outbox:** prevents the classic “database committed but queue publish failed” gap. It is durable, retryable, and observable. The worker’s business effect must remain idempotent as it evolves.
- **Inventory ledger:** stock changes are written with reason, order reference, signed quantity delta, and resulting stock, making reservation/return/restock activity inspectable.
- **Money:** prices and totals use `Decimal` / fixed-precision SQL `NUMERIC`, not binary floating-point arithmetic.
- **Historical order lines:** SKU, product name, unit price, quantity, and line total are snapshotted when the order is created.
- **Health checks:** `/healthz` reports process health; `/readyz` verifies database connectivity.

## Run locally

### 1. Requirements

- Python 3.10 or newer
- pip

No external database or Redis service is needed for local SQLite development.

### 2. Create an environment

**macOS / Linux**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Open `.env` and replace `SECRET_KEY` with a unique random secret before using the app beyond a local demo. You can generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 3. Prepare the database and demo account

The local `.env.example` enables `AUTO_CREATE_TABLES=true`; tables are created when the app starts. To exercise the explicit migration workflow as well, run:

```bash
alembic upgrade head
python -m app.seed
```

The seed command adds representative products and a local operator account if they do not exist. Local demo login:

- **Email:** `admin@orderflow.local`
- **Password:** `OrderFlow123!`

These credentials are for local development only. Change the password or create a different operator before exposing the app to any network. The seed command refuses to run when `ENVIRONMENT=production`.

### 4. Start the web app

```bash
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), sign in, and explore the dashboard, inventory and order screens.

### 5. Start the background worker

In a **second terminal**, activate the same virtual environment and run:

```bash
python -m app.worker
```

The worker polls the transactional outbox and processes available events. Without the worker, orders are still safely committed and visible, but remain pending until their creation event is handled. Stop the worker with `Ctrl+C`.

To process at most one available event and exit:

```bash
python -m app.worker --once
```

## Docker Compose (PostgreSQL + web + worker)

1. Create `.env` from `.env.example` and set a unique `SECRET_KEY` of at least 32 characters.
2. For a throwaway local demo, the compose file defaults the database password to `local-development-only`; override `POSTGRES_PASSWORD` in your shell before using it beyond local development.
3. Start the stack:

```bash
docker compose up --build
```

The web container runs `alembic upgrade head` before starting Uvicorn. The worker waits for the web container's readiness check, then consumes outbox events. The app is available at `http://localhost:8000`.

For a real deployment, use a managed PostgreSQL service, a strong database password from a secret store, HTTPS terminated by a trusted proxy/load balancer, `SESSION_HTTPS_ONLY=true`, restricted inbound traffic, and an appropriate backup/restore plan. The HTTP-only local Compose profile leaves secure cookies disabled so the local HTTP demo can sign in; **do not use that setting on a public HTTPS deployment**.

## Environment configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_NAME` | `OrderFlow` | Product label |
| `ENVIRONMENT` | `development` | Production mode requires a configured strong secret |
| `SECRET_KEY` | Development-only fallback | Signs session cookies; must be unique and secret in production |
| `DATABASE_URL` | `sqlite:///./orderflow.db` | SQLAlchemy connection URL |
| `SESSION_HTTPS_ONLY` | `false` | Set `true` in production behind HTTPS |
| `AUTO_CREATE_TABLES` | `true` outside production | Automatically create schema for local development |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `WORKER_POLL_SECONDS` | `2` | Outbox polling interval |
| `WORKER_MAX_ATTEMPTS` | `5` | Maximum worker attempts before an event is marked failed |
| `WORKER_RETRY_BASE_SECONDS` | `3` | Base delay for exponential retry backoff |

`DATABASE_URL` examples:

```text
sqlite:///./orderflow.db
postgresql+psycopg://orderflow:password@localhost:5432/orderflow
```

## API reference

Interactive OpenAPI documentation is available at **`/api/docs`** and ReDoc at **`/api/redoc`**. API routes are session-authenticated using the same operator sign-in as the UI. Unsafe API methods also require the `X-CSRF-Token` header from the signed-in session. Requests from a command-line client should obtain a session and CSRF token through the sign-in flow; do not disable CSRF protection to simplify a demo.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/healthz` | Process health |
| `GET` | `/readyz` | Database readiness |
| `GET` | `/api/v1/products` | Active catalog |
| `POST` | `/api/v1/orders` | Create order; send `Idempotency-Key` and `X-CSRF-Token` |
| `GET` | `/api/v1/orders/{order_number}` | Inspect order and line items |
| `POST` | `/api/v1/orders/{order_number}/cancel` | Cancel an eligible order and return stock |
| `POST` | `/api/v1/orders/{order_number}/ship` | Mark a processing order as shipped |

Example order body:

```json
{
  "customer_email": "customer@example.com",
  "items": [
    { "product_id": 1, "quantity": 2 },
    { "product_id": 4, "quantity": 1 }
  ]
}
```

Required request header:

```text
Idempotency-Key: checkout-unique-request-0001
```

Product IDs depend on your local database. The API validates the email shape, positive product IDs, item count and quantities. A missing key is a bad request; a repeated key with a different payload or insufficient stock returns a conflict.

## Automated tests

Install development dependencies and run:

```bash
pytest -q
python -m compileall -q app tests
alembic check
```

The test suite covers health/readiness, authentication requirements, page rendering, JSON API authorization, idempotent retries, inventory reservation, rollback on insufficient stock, cancellation/restock idempotency, outbox processing, and order shipping-state transitions. Tests create an isolated SQLite database per test; they do not need a running PostgreSQL service.

## Repository structure

```text
orderflow/
├── app/
│   ├── main.py                 # FastAPI app factory and health routes
│   ├── config.py               # Environment configuration
│   ├── database.py             # Engine and session factory
│   ├── models.py               # SQLAlchemy models / invariants
│   ├── schemas.py              # Pydantic request validation
│   ├── security.py             # Password hashes and CSRF helpers
│   ├── routers/
│   │   ├── web.py              # Server-rendered operations UI
│   │   └── api.py              # Versioned JSON API
│   ├── services/
│   │   ├── orders.py            # Order, stock and lifecycle transactions
│   │   ├── inventory.py         # Manual stock operations
│   │   ├── analytics.py         # Dashboard aggregates
│   │   └── outbox.py            # Durable background event consumer
│   ├── templates/              # Responsive Jinja2 views
│   ├── static/css/app.css       # UI design system
│   └── static/js/app.js         # Navigation, dialogs and small interactions
├── alembic/                    # Versioned schema migration
├── tests/                      # API and domain tests
├── .github/workflows/tests.yml # CI test workflow
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
├── .env.example
└── README.md
```

## Engineering limitations and next steps

This portfolio implementation is intentionally scoped, not a claim of production readiness without further work. Before public production use, add centralized rate limiting and brute-force protection, proper operator/user provisioning, audit actor IDs, centralized secrets management, CSRF/origin policy review for the chosen deployment topology, external payment-provider integration, real notifications, distributed tracing/metrics, backups, and formal concurrency/load testing against the production database. Shipping is manually simulated and no real payment data is collected.

Useful future improvements:

- Add a customer-facing storefront and checkout session.
- Integrate a payment provider using idempotent webhook processing.
- Publish outbox events to SQS/RabbitMQ/Redis workers at larger scale.
- Add role-based access policies, low-stock notifications, and inventory movement export.
- Benchmark concurrency and query plans with a reproducible Locust profile; publish measured p50/p95/p99 latency and error rates.

## Resume-ready summary

**OrderFlow — Reliable E-Commerce Order & Inventory Platform | Python, FastAPI, SQLAlchemy, PostgreSQL, Docker**

- Built a full-stack order and inventory platform with REST APIs, authenticated operations pages, product administration, order lifecycle controls, and an inventory audit trail.
- Implemented transaction-safe stock reservations, idempotent order creation, rollback-safe business logic, and a transactional outbox with a retryable background worker.
- Added automated API/domain tests, Alembic migrations, PostgreSQL Docker Compose deployment, health/readiness checks, and CI; extend this line with actual load-test results after measuring them.

Only add performance percentages, request throughput, or uptime figures to your resume after measuring them on a reproducible test setup.
