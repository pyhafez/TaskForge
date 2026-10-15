# Local verification — September 25, 2026

Verified on Windows with Python 3.13.15:

- Dependency installation and `pip check` passed.
- 90 fast tests passed (pure domain, Django API/database, CLI, subprocess execution, dashboard shell, and tracing context).
- 8 real-service integration tests passed in 46.55 seconds.
- The real-service suite used PostgreSQL 16 and Redis 7 in disposable Docker containers, with actual HTTP, CLI, Celery worker, and dispatcher processes.
- The suite passed concurrent idempotency, retry exhaustion/recovery, actual worker kill/recovery, stopped Redis/recovery, timeout, log retrieval, and 1-versus-4-thread throughput checks.
- Ruff lint and format checks, Django system checks, migration drift checks, JavaScript syntax checking, and Compose configuration validation passed.
- The Linux application Docker image built successfully, including pinned dependencies and collected static assets.
- A disposable Linux container passed Django startup checks with all OpenTelemetry instrumentation enabled.
- The installed `taskforge-server` entry point applied migrations, booted successfully, returned `{"status":"ok"}` at `/healthz`, and served the dashboard with HTTP 200.

Commands for reproducing these results are in the README. The default integration images are PostgreSQL 17 and Redis 7.4; cached compatible images were used locally because Docker Hub refused downloads. The runner supports explicit image overrides and context-manager cleanup without Ryuk for that case.

The full Jaeger Compose deployment and interactive browser behavior were not exercised in this session. The dashboard shell/assets, API workflow, JavaScript syntax, and trace propagation were tested independently. CI is configured but has not been run by a remote CI service from this workspace.
