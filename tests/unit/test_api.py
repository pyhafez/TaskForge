import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from django.conf import settings
from rest_framework.test import APIClient

from taskforge.core.models import APIKey, Job, Project, Run, User

pytestmark = pytest.mark.django_db


def test_public_health_and_private_resources():
    client = APIClient()
    assert client.get("/healthz").json() == {"status": "ok"}
    response = client.get("/api/projects")
    assert response.status_code == 401
    assert response.json()["status"] == 401
    assert response["Content-Type"] == "application/problem+json"
    assert response["WWW-Authenticate"] == "Bearer"


def test_register_login_and_wrong_password():
    client = APIClient()
    values = {"email": "New@Example.com", "password": "correct-horse-battery-77", "role": "admin"}
    assert client.post("/api/auth/register", values, format="json").status_code == 201
    user = User.objects.get(email="new@example.com")
    assert user.role == "operator"
    assert user.password.startswith("argon2$")
    assert user.check_password(values["password"])
    assert client.post("/api/auth/register", values, format="json").status_code == 409
    response = client.post("/api/auth/login", values, format="json")
    assert response.status_code == 200
    assert response.json()["expires_in"] > 0
    client.credentials(HTTP_AUTHORIZATION="Bearer " + response.json()["access_token"])
    assert client.get("/api/auth/me").json()["email"] == "new@example.com"
    values["password"] = "incorrect"
    assert client.post("/api/auth/login", values, format="json").status_code == 401


def test_expired_and_malformed_tokens():
    client = APIClient()
    token = jwt.encode(
        {
            "sub": "1",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
            "iss": "taskforge",
            "aud": "taskforge-api",
            "jti": "x",
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    for value in ("Bearer " + token, "Bearer nonsense", "Basic xyz", "Bearer"):
        client.credentials(HTTP_AUTHORIZATION=value)
        assert client.get("/api/projects").status_code == 401


def test_project_crud_and_structured_validation(client):
    response = client.post("/api/projects", {"name": "Release"}, format="json")
    assert response.status_code == 201
    url = f"/api/projects/{response.json()['id']}"
    assert client.get(url).json()["name"] == "Release"
    assert client.get("/api/projects").json()["count"] == 1
    assert client.post("/api/projects", {"name": "Release"}, format="json").status_code == 409
    invalid = client.post("/api/projects", {"name": ""}, format="json")
    assert invalid.status_code == 422
    assert "name" in invalid.json()["errors"]
    assert (
        client.put(url, {"name": "Changed", "description": "v2"}, format="json").status_code == 200
    )
    assert client.patch(url, {"description": "v3"}, format="json").json()["description"] == "v3"
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404


def test_job_crud_cascade_and_missing_project(client, job):
    nested = f"/api/projects/{job.project_id}/jobs"
    response = client.post(
        nested,
        {"name": "sleep", "task_type": "sleep", "payload_template": {"seconds": 1}},
        format="json",
    )
    assert response.status_code == 201
    url = f"/api/jobs/{response.json()['id']}"
    assert client.get(url).status_code == 200
    assert client.patch(url, {"timeout_seconds": 3}, format="json").json()["timeout_seconds"] == 3
    assert client.post(nested, {"name": "sleep"}, format="json").status_code == 409
    assert client.get(nested).json()["count"] == 2
    assert (
        client.post(
            f"/api/projects/{uuid.uuid4()}/jobs", {"name": "missing"}, format="json"
        ).status_code
        == 404
    )
    assert client.post(f"/api/jobs/{job.id}/submit", {}, format="json").status_code == 202
    assert client.delete(f"/api/projects/{job.project_id}").status_code == 204
    assert not Job.objects.exists()
    assert not Run.objects.exists()


def test_ownership_and_roles(client, user):
    other = User.objects.create_user("other@example.com", "secure-password-92")
    project = Project.objects.create(owner=other, name="private")
    assert client.get(f"/api/projects/{project.id}").status_code == 404
    assert client.get("/api/users").status_code == 403
    user.role = "admin"
    user.save()
    assert client.get("/api/users").status_code == 200
    assert (
        client.patch(f"/api/users/{other.id}", {"is_active": False}, format="json").status_code
        == 200
    )


def test_api_key_shown_once_and_revocable(client):
    response = client.post("/api/api-keys", {"name": "ci"}, format="json")
    assert response.status_code == 201
    raw = response.json()["key"]
    assert raw not in str(client.get("/api/api-keys").json())
    assert APIKey.objects.get().digest != raw
    key_client = APIClient()
    key_client.credentials(HTTP_X_API_KEY=raw)
    assert key_client.get("/api/projects").status_code == 200
    assert client.delete(f"/api/api-keys/{response.json()['id']}").status_code == 204
    assert key_client.get("/api/projects").status_code == 401
    assert APIKey.objects.get().revoked_at is not None


def test_submission_idempotency_conflict_and_snapshot(client, job):
    url = f"/api/jobs/{job.id}/submit"
    first = client.post(
        url, {"payload": {"value": "original"}}, format="json", HTTP_IDEMPOTENCY_KEY="stable"
    )
    assert first.status_code == 202
    second = client.post(
        url, {"payload": {"value": "original"}}, format="json", HTTP_IDEMPOTENCY_KEY="stable"
    )
    assert second.json()["id"] == first.json()["id"]
    assert second["Idempotency-Replayed"] == "true"
    assert (
        client.post(
            url, {"payload": {"value": "different"}}, format="json", HTTP_IDEMPOTENCY_KEY="stable"
        ).status_code
        == 409
    )
    assert client.post(url, {}, format="json", HTTP_IDEMPOTENCY_KEY="other").status_code == 202
    job.payload_template = {"value": "edited"}
    job.save()
    assert Run.objects.get(pk=first.json()["id"]).payload["value"] == "original"
    assert Run.objects.count() == 2


@pytest.mark.parametrize(
    "values",
    [
        {"retry_policy": {"max_attempts": 0}},
        {"retry_policy": {"junk": 1}},
        {"timeout_seconds": 0},
        {"payload_template": []},
        {"task_type": "shell"},
    ],
)
def test_invalid_job_inputs(client, job, values):
    assert client.patch(f"/api/jobs/{job.id}", values, format="json").status_code == 422


def test_invalid_submit_and_log_cursor(client, job):
    url = f"/api/jobs/{job.id}/submit"
    assert client.post(url, {"payload": {"unknown": True}}, format="json").status_code == 422
    assert client.post(url, {"payload": []}, format="json").status_code == 422
    run = client.post(url, {}, format="json").json()
    assert client.get(f"/api/runs/{run['id']}/logs?after=bad").status_code == 422
    assert client.get("/api/runs?state=INVALID").status_code == 422
    assert client.get("/api/missing").json()["status"] == 404
