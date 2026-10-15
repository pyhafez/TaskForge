# Story implementation and acceptance coverage

All story IDs from the requested scope are represented below. Tests listed here are executable acceptance evidence, not a claim that unexecuted environments have passed.

| Story | Implementation | Coverage |
|---|---|---|
| US-01 | pyproject, environment settings, server entry point, Compose, pytest, CI | Install, Django checks, fast suite |
| US-02 | Public health route, configured PostgreSQL/Redis checks | API health test; real Redis outage |
| US-03 | Owner-scoped project CRUD with DB uniqueness | API CRUD, conflicts, 422, 404 |
| US-04 | Nested job CRUD, FK/cascades, retry schema | API CRUD, missing parent, cascade |
| US-05 | Pure lifecycle rules, immutable runs, attempt records | Every state pair; worker execution |
| US-06 | Problem detail exception handler and validation | API error content type/status/fields |
| US-07 | Email registration/login, Django Argon2, expiring JWT | Hash, login, wrong password, expiry |
| US-08 | JWT/API-key dependency on resource views | Anonymous/malformed tokens; owner isolation |
| US-09 | Admin user management, operators scoped to own data | Operator 403; admin update |
| US-10 | One-time key reveal, SHA-256 digest, soft revocation | API key lifecycle |
| US-11 | CLI login/logout, private atomic config | CLI credentials and API-key login |
| US-12 | CLI create, submit, list, status, requeue | CLI request mapping; real CLI submission |
| US-13 | Cursor log retrieval and CLI follow polling | Pagination/draining; real CLI logs |
| US-14 | Responsive dashboard using only public REST endpoints | Dashboard/static smoke; manual browser review |
| US-15 | Transactional run/outbox plus bounded Redis publish | Real Redis message; API 202 |
| US-16 | Celery consumption, subprocess execution, results, late ack | Unit worker; real-service golden path |
| US-17 | Thread pool, row-locked claim, lease fencing | Real 1-vs-4 concurrency; attempt count |
| US-18 | Shared validated task registry; demo/sleep/HTTP | Registry/schema validation; demo/sleep execution |
| US-19 | Capped exponential jitter, persisted retry deadlines | Pure policy tests; timing; real transient recovery |
| US-20 | PostgreSQL DEAD queue, error/attempt inspection, requeue | Retry exhaustion and manual requeue |
| US-21 | Unique per-job idempotency key and request fingerprint | Unit replay/conflict; simultaneous real submissions |
| US-22 | Per-run timeout with subprocess kill | Unit and real worker timeout |
| US-23 | Leases, fenced completion, independent recovery dispatcher | Unit stale token; real worker kill |
| US-24 | structlog over stdlib JSON formatter | Logging output during all API/worker tests |
| US-25 | Request trace ID propagated into run, queue, logs | API response header and correlated stored logs |
| US-26 | Optional Django/Celery/Redis/psycopg OTel + execution span | Optional instrumentation startup; Compose Jaeger |
| US-27 | Run/job log endpoints with cursors | API log validation; worker log content |
| US-28 | Testcontainers-backed CLI → API → Redis → worker → logs | `test_cli_api_queue_worker_logs_golden_path` |
| US-29 | Duplicate, outage, kill, retry exhaustion injection | `tests/integration/test_system.py` |
| US-30 | Complete Compose deployment with local Jaeger | Compose config/build/start checks |

The chosen backend is Django + DRF, as requested. Django ORM and migrations replace the original stories' illustrative SQLAlchemy/Alembic choices. Celery provides the queue and concurrency. The web UI stays deliberately thin; REST exposes more management operations than the dashboard.

Important boundaries: submission deduplication does not imply exactly-once external effects; PostgreSQL is required for concurrency guarantees; run logs are polling-based rather than a push stream; the database is the durable dead-letter queue; optional tracing and live UI behavior need deployment-level verification in addition to unit tests.
