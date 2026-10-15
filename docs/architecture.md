# Architecture and operational notes

`taskforge/domain.py` contains lifecycle/retry rules and is independent of Django. `core/models.py` holds persistence, `core/services.py` coordinates transactions, `core/views.py` is the HTTP adapter, and `core/tasks.py` is the Celery adapter. Neither CLI nor dashboard imports service or model code.

## Transactions and concurrency

Submission locks a job before reading its execution definition and checking the idempotency key. The unique database constraint is a second line of defense. The accepted run snapshots execution settings and persists its outbox record in the same transaction. An on-commit publisher makes healthy-broker submissions immediately available; failures leave the outbox untouched.

Dispatchers lock run then outbox consistently. A publish can succeed just before a database rollback or dispatcher crash, so duplicate messages are expected. The worker's transactional claim checks QUEUED, assigns a random lease token, increments attempt counters, creates an attempt record, and removes the outbox. Only that token can persist output and completion while the run remains RUNNING.

Worker subprocesses produce NDJSON output. A reader thread feeds a bounded queue while the worker writes incremental run logs, checks its deadline, and collects the result. Results and errors are bounded. Plug-ins are trusted, repository-installed code; this process boundary is a timeout mechanism, not a sandbox for arbitrary hostile code.

On expiry, the dispatcher records an interrupted attempt and applies the same retry policy as an ordinary failure. A late old worker result is ignored after token replacement. A returned Celery task is acknowledged; if a database exception prevents completion, the lease remains and recovery still applies. Redis redelivery by itself is not the sole recovery mechanism.

Outbox republishing also handles Redis data loss: queued runs are published again when `QUEUE_REPUBLISH_SECONDS` elapses. Redis persistence improves delivery but PostgreSQL is the authoritative record. Do not run production workers on SQLite, and do not disable the dispatcher unless another independently monitored maintenance scheduler is in place.

## State and retention

Each Run represents an accepted execution; each Attempt records an individual try with its own number, outcome, timing, and failure reason. The API cannot set arbitrary state strings. Illegal transitions raise a domain error, translated to 409 when exposed through an API action. Manual requeue keeps the original payload and task settings and resets the cycle retry counter; total attempts remain monotonic.

Deleting a project or job cascades into its runs/logs/attempts/outbox. Deletion is not a task-cancellation API: an already executing external operation may finish after deletion. Retention is not automatically scheduled. A production retention policy must preserve idempotency keys for at least the client's retry horizon; deleting runs permits future reuse of their keys.

## Security

Operators see only their own project hierarchy. Administrators can manage user access and inspect all resources. The API reads current user activation and role from the database on every authenticated request. Passwords use Argon2; API keys are high-entropy random values whose SHA-256 digest alone is stored. JWTs have explicit issuer/audience/expiry and use a single configured signing secret. There is no refresh-token flow; log in again after expiry.

HTTP tasks are restricted to configured hostnames, HTTPS/443, publicly resolved addresses, bounded responses, and no redirects or ambient proxies. Egress firewall/proxy policy is still necessary to protect against DNS changes between validation and connection. HTTP request query strings and task payloads are excluded from infrastructure logs. Output is visible to authorized resource owners and may itself contain sensitive application data.

The dashboard uses text nodes for untrusted values, a same-origin Content Security Policy, and tab-scoped sessionStorage for its bearer token. It sends no authenticated cookie-based resource requests. Static files are served by WhiteNoise in the packaged deployment. Public login/registration are rate-limited locally; distribute rate limiting at the ingress when deploying multiple app processes.

## Observability

`X-Trace-ID` is a correlation ID independently useful without an OTel collector. Request middleware binds context for API logs; the accepted run stores it, and publisher/worker logs explicitly retain it. Output rows store run ID, trace ID, attempt, event, message, and timestamp. Log API cursors use monotonic row IDs to avoid missing records that share timestamps.

With OTel enabled, instrumented database/Redis/HTTP request and Celery operations produce spans. The incoming W3C trace context is snapshotted at acceptance so delayed execution can retain its original parent even when dispatched from another process. Jaeger's local in-memory storage is a demonstration deployment; configure durable trace storage for production.

## Test separation

Fast tests exercise pure rules, API behavior with a temporary SQLite database, and trusted child execution. PostgreSQL-specific locking and Redis behavior belong to the real-service integration suite. The suite talks over HTTP, starts actual worker/dispatcher processes, stops the disposable Redis container, kills a worker, and compares measured throughput at two concurrency settings. It does not patch application I/O.

The concurrency benchmark is a regression check for this controlled sleep workload, not a capacity estimate. Production tuning depends on task CPU/memory/network needs and PostgreSQL connection limits. Each worker thread maintains its own Django connection while executing; plan pool sizes accordingly.
