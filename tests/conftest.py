import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from taskforge.core.authentication import issue_token
from taskforge.core.models import Job, Project, User


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        help="Run real-service tests (use scripts/integration.py)",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--integration"):
        marker = pytest.mark.skip(reason="Run python scripts/integration.py with Docker available")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(marker)


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create_user("operator@example.com", "correct-horse-battery-77")


@pytest.fixture
def client(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(user)}")
    return client


@pytest.fixture
def job(user):
    project = Project.objects.create(owner=user, name="Test project")
    return Job.objects.create(project=project, name="demo")
