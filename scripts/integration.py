"""Launch disposable real services, then run the integration suite against them."""

import argparse
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from testcontainers.core.config import testcontainers_config
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-image", default="postgres:17-alpine")
    parser.add_argument("--redis-image", default="redis:7.4-alpine")
    parser.add_argument(
        "--no-ryuk",
        action="store_true",
        help="Use context-manager cleanup only; useful offline when Ryuk is not cached",
    )
    options, pytest_args = parser.parse_known_args()
    if options.no_ryuk:
        testcontainers_config.ryuk_disabled = True
    root = Path(__file__).resolve().parent.parent
    (root / ".test-output").mkdir(exist_ok=True)
    # Docker may reassign a dynamically published port after stop/start. Bind an
    # explicit free host port so outage recovery tests keep the same broker URL.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        redis_port = probe.getsockname()[1]
    with (
        PostgresContainer(options.postgres_image, driver="psycopg") as postgres,
        RedisContainer(options.redis_image).with_bind_ports(6379, redis_port) as redis,
    ):
        env = {
            **os.environ,
            "DATABASE_URL": postgres.get_connection_url().replace(
                "postgresql+psycopg", "postgresql"
            ),
            "REDIS_URL": f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0",
            "REDIS_CONTAINER_ID": redis.get_wrapped_container().id,
            "DEBUG": "true",
            "OTEL_ENABLED": "false",
            "REGISTRATION_OPEN": "true",
            "ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
            "SECRET_KEY": "integration-only-secret-never-use-outside-tests-1234",
            "LEASE_GRACE_SECONDS": "1",
            "QUEUE_REPUBLISH_SECONDS": "2",
        }
        subprocess.run(
            [sys.executable, "manage.py", "migrate", "--noinput"], cwd=root, env=env, check=True
        )
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/integration",
                "--integration",
                "--basetemp",
                str(root / ".test-output" / f"integration-{uuid.uuid4().hex[:8]}"),
                "-p",
                "no:cacheprovider",
                "-v",
                *pytest_args,
            ],
            cwd=root,
            env=env,
        )


if __name__ == "__main__":
    raise SystemExit(main())
