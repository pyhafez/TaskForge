# TaskForge

A Django task platform with a REST API, CLI, dashboard, and durable asynchronous execution. PostgreSQL is the source of truth; Redis transports work to Celery workers.

## Quick start

Python 3.12 or 3.13:

```sh
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -c requirements.lock -e '.[dev,observability]'
taskforge-server
```

Open `http://127.0.0.1:8000` and create an account. The server command applies migrations and starts Django's development server. `GET /healthz` returns `{"status":"ok"}` with the default SQLite configuration. Copy `.env.example` to `.env` to customize settings.

SQLite mode supports the dashboard, authentication, CRUD, and durable submission. **Executing queued jobs requires PostgreSQL, Redis, a worker, and the dispatcher.** SQLite is for local API exploration and fast tests; it does not provide the row locks required for concurrent workers.

## Run the complete platform

With Docker Desktop or Docker Engine running:

```sh
docker compose up --build -d
docker compose exec app python manage.py createsuperuser
```

- Dashboard and API: `http://localhost:8000`
- Jaeger traces: `http://localhost:16686` (service: `taskforge`)
- Structured logs: `docker compose logs -f app worker dispatcher`
- Scale workers: `docker compose up -d --scale worker=3`
- Stop without deleting data: `docker compose down`

Compose includes a one-shot migration service, API, Celery worker (four concurrent threads), independent outbox dispatcher, PostgreSQL, Redis with AOF persistence, and Jaeger. Each task executes inside its own killable Python subprocess, so timeouts also apply when the Celery pool uses threads. Images and the app run locally; service ports bind to loopback.

For host development with containerized dependencies:

```sh
docker compose up -d postgres redis
```

Set these in `.env`:

```dotenv
DATABASE_URL=postgresql://taskforge:taskforge@localhost:5432/taskforge
REDIS_URL=redis://localhost:6379/0
```

Then run each command in a separate activated terminal:

```sh
taskforge-server
celery -A taskforge.celery:app worker --pool=threads --concurrency=4 --loglevel=INFO
python manage.py dispatch
```

`python manage.py dispatch --once` performs one recovery/dispatch pass. Celery Beat can schedule the same maintenance task, but the independent dispatcher is the recommended deployment: it keeps recovering database leases during a Redis outage.

## CLI workflow

Register in the dashboard or `POST /api/auth/register`, then:

```sh
taskforge login --email operator@example.com
taskforge create-project "Demo"
taskforge create-job PROJECT_UUID "First job" --task-type python_demo
taskforge submit JOB_UUID --payload '{"value":"hello"}' --idempotency-key first-run
taskforge list runs
taskforge status RUN_UUID
taskforge logs --run RUN_UUID --follow
taskforge requeue DEAD_RUN_UUID
taskforge logout
```

PowerShell JSON argument quoting depends on your PowerShell version; the dashboard is another way to submit payloads. `taskforge --help` lists all commands.

The CLI emits JSON for resource commands, human-readable logs, and nonzero exit codes for invalid arguments, HTTP failures, failed followed runs, and interrupted follows. Follow mode polls the cursor-based endpoint; it drains all pages before exiting. It does not use SSE or WebSockets.

For automation, create a key with `POST /api/api-keys` and set `TASKFORGE_API_KEY`. `taskforge login --api-key ...` also works, but environment variables avoid shell history. `TASKFORGE_URL`, `TASKFORGE_TOKEN`, and `TASKFORGE_CONFIG` override the default server, credentials, or credentials file location. Credentials are written atomically with mode `0600` on POSIX; Windows uses the user's home-directory ACL. The file contains credentials in plaintext, as do most token-based CLI configs. Logout removes local credentials; revoke API keys through the API. JWTs expire after 60 minutes by default.

## API

Routes use **no trailing slash**. Resource endpoints require `Authorization: Bearer JWT` or `X-API-Key: tf_...`. Registration, login, health, and the static dashboard shell are public. Operators manage their own projects and related resources; admins can access all resources and manage user roles/activation. Registration always creates an operator. Create the initial admin with `createsuperuser`.

| Method | Route | Behavior |
|---|---|---|
| GET | `/healthz` | 200 when healthy, 503 when a configured dependency is unavailable |
| POST | `/api/auth/register` | Register email/password; 201, duplicate 409 |
| POST | `/api/auth/login` | JWT with expiry; incorrect password 401 |
| GET | `/api/auth/me` | Current account |
| GET, POST | `/api/projects` | List/create projects |
| GET, PUT, PATCH, DELETE | `/api/projects/{id}` | Project CRUD |
| GET, POST | `/api/projects/{id}/jobs` | List/create jobs in project |
| GET | `/api/jobs` | List visible jobs |
| GET, PUT, PATCH, DELETE | `/api/jobs/{id}` | Job CRUD |
| POST | `/api/jobs/{id}/submit` | Accept a run; 202 and Location header |
| GET | `/api/jobs/{id}/logs` | Logs across a job's runs |
| GET | `/api/runs?job={id}&state=DEAD&page=1` | Filter runs; DEAD is the durable dead-letter queue |
| GET | `/api/runs/{id}` | State, result, failure, attempt count |
| GET | `/api/runs/{id}/attempts` | Individual attempt history |
| GET | `/api/runs/{id}/logs?after=0&limit=100` | Cursor-based output, `next_cursor`, `has_more`, current state |
| POST | `/api/runs/{id}/requeue` | Requeue DEAD run, preserving attempt history |
| GET, POST | `/api/api-keys` | List/create own API keys; secret shown once |
| DELETE | `/api/api-keys/{id}` | Soft revoke key |
| GET, PATCH, DELETE | `/api/users/{id}` | Admin-only read, role update, deactivate |
| GET | `/api/users` | Admin-only user list |

Lists use `{count, next, previous, results}` with 50 entries per page. Successful creates return 201, reads/updates 200, deletes 204, submissions/requeues 202, missing resources 404, conflicts 409, and validation failures 422. Errors use `application/problem+json` with `type`, `title`, `status`, `detail`, `instance`, `trace_id`, and field-level `errors` when applicable. Cross-account resource access returns 404. User deletion deactivates the account and immediately invalidates its tokens and keys.

Example job body:

```json
{
  "name": "Flaky demo",
  "task_type": "python_demo",
  "payload_template": {"value": "Hello", "fail_until": 2},
  "timeout_seconds": 30,
  "retry_policy": {"max_attempts": 4, "base_delay": 1, "max_delay": 30, "jitter": 0.2}
}
```

Submit body: `{"payload":{"value":"optional shallow overrides"}}`. A validated, immutable copy of the effective payload, timeout, task type, and retry policy is stored on the run. A later job edit cannot change accepted work.

Task types:

- `python_demo`: `value` (string), `fail_until` (number of initial attempts to fail).
- `sleep`: `seconds` (0–3600), with progress logs while executing.
- `http_call`: `url`, HTTPS GET to an explicitly allowed public host, no redirects, capped response size. Set `HTTP_ALLOWED_HOSTS` to a comma-separated allowlist.

To add a trusted plug-in, define a Pydantic payload schema and a handler decorated with `@register(name, schema)` in `taskforge/task_types.py`. The API and child executor load the same registry. User-supplied Python and shell execution are intentionally unsupported. Task handlers receive `run_id`, `trace_id`, and attempt numbers; handlers that cause external side effects must use the stable run ID for downstream idempotency.

## Reliability contract

```mermaid
flowchart LR
    Client --> API[Django REST API]
    API --> DB[(PostgreSQL: run + outbox)]
    DB --> Dispatcher[Outbox dispatcher]
    Dispatcher --> Redis[(Redis)]
    Redis --> Worker[Celery workers]
    Worker --> Child[Isolated task process]
    Worker --> DB
    Child --> Logs[Run logs]
    Logs --> DB
```

- **Durable acceptance:** run and outbox entry are committed together before publishing. Redis downtime leaves accepted work pending. The dispatcher republishes after recovery. The API attempts one bounded publish; it never waits for task execution.
- **Idempotent submission:** `(job_id, idempotency_key)` has a database unique constraint. Repeating a key and request payload returns the original run with `Idempotency-Replayed: true`. A different payload with that key returns 409. Keys are scoped to a job and retained with the run.
- **At-least-once delivery:** late acknowledgements, Redis visibility timeout, and periodic republishing cover dropped messages. A PostgreSQL row lock and lease token prevent simultaneous claims and fence stale results. Duplicate deliveries do not create extra attempts on an active or completed run.
- **Crash recovery:** a lease expires after the task timeout plus 15 seconds (configurable). The dispatcher records the interrupted attempt and schedules a retry or marks it DEAD. Recovery is bounded by that lease plus the dispatcher interval; it is not instantaneous. Linux task children receive a parent-death signal.
- **Retries:** `max_attempts` includes the first attempt. The delay after failure number `n` is `min(max_delay, base_delay * 2^(n-1)) * U(1-jitter, 1)`. Due times live in PostgreSQL, not in long-lived broker countdown messages. Dispatcher cadence adds up to about two seconds to the configured delay when healthy and unloaded.
- **Dead letters:** DEAD runs, errors, and attempt history are persisted in PostgreSQL and queryable through the API. Requeue starts a fresh retry budget on the same immutable run while retaining total attempt history. It does not copy messages into a second Redis list.
- **Timeouts:** the worker kills the task subprocess, stores the timeout failure, and applies the retry policy.
- **Delivery is not exactly-once side effects.** A worker may execute an external operation and crash before committing its result. External systems must support idempotency or application-specific deduplication. Database fencing protects TaskForge state; it cannot undo external effects.

Lifecycle: `PENDING → QUEUED → RUNNING → SUCCEEDED` or `RUNNING → FAILED → RETRYING → QUEUED`; exhausted failures become `DEAD`. Only manual requeue allows `DEAD → QUEUED`. FAILED is recorded on individual attempts and is a transient run state within the failure transaction. SUCCEEDED is terminal. Clients cannot arbitrarily mutate run state.

## Tests

```sh
pytest                              # fast suite; real-service tests explicitly skipped
pytest tests/unit --cov=taskforge --cov-report=term-missing
python scripts/integration.py       # requires Docker; disposable PostgreSQL/Redis
ruff check .
ruff format --check .
python manage.py check
python manage.py makemigrations --check --dry-run
```

The fast suite includes pure lifecycle/retry tests, Django API/database tests, authentication/authorization, CLI tests, and real task subprocess execution. API tests use isolated SQLite test databases. Narrow mocks are used for CLI request mapping and broker failure unit tests.

The integration runner uses Testcontainers and creates a fresh PostgreSQL database, Redis, an HTTP API process, workers, and dispatcher processes. **No I/O mocks** are used in these tests. It verifies the CLI golden path, concurrent idempotency, an actual Redis message, retry recovery/exhaustion, worker kill and recovery, stopped Redis and recovery, timeout enforcement, and a 1-thread versus 4-thread throughput comparison. Containers are stopped automatically on exit. The tests modify only their disposable infrastructure. Docker Hub access is needed on the first run.

For offline testing with cached compatible images, use `python scripts/integration.py --postgres-image postgres:16-alpine --redis-image redis:7-alpine --no-ryuk`. This keeps normal context-manager container cleanup but omits the auxiliary Ryuk cleanup container; if the runner itself is force-killed, remove its leftover test containers manually.

CI is configured to run the fast suite on Python 3.12/3.13 and real-service tests on Linux. See [local verification](docs/verification.md) for executed checks and their limits, [story coverage](docs/stories.md) for the acceptance mapping, and [architecture notes](docs/architecture.md) for operational details.

## Configuration and deployment

Settings are loaded from environment variables and `.env` by pydantic-settings. Important settings include `DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `JWT_TTL_MINUTES`, `REGISTRATION_OPEN`, `HTTP_ALLOWED_HOSTS`, `LEASE_GRACE_SECONDS`, `QUEUE_REPUBLISH_SECONDS`, and `OTEL_ENABLED`.

`requirements.lock` is an exact dependency constraint snapshot for reproducible installs; `pyproject.toml` declares supported ranges. Update constraints deliberately and rerun both suites.

The supplied Compose configuration is a local development deployment. Before exposing it, set unique database credentials and a random secret, terminate HTTPS at your ingress, set explicit hosts, close public registration if appropriate, enforce an outbound network policy for HTTP tasks, and arrange backups and log retention. HTTP task host validation is defense in depth; DNS rebinding must also be controlled by network egress policy. Django's built-in auth throttle is process-local; use ingress throttling for a public multi-process deployment. Jaeger uses ephemeral storage in this setup.

Structured logs include timestamp, level, event, and request/run context. `X-Trace-ID` accepts 32 lowercase hex characters or generates a fresh ID; response headers and stored run logs expose it. Optional OpenTelemetry instruments Django, Celery, Redis, and psycopg, plus a persisted-context execution span. Django's ORM and migrations replace SQLAlchemy/Alembic from the original FastAPI-oriented stories; Django's Argon2 hasher replaces passlib.
