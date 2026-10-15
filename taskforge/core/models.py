import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower

from taskforge.domain import State, transition


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=email.strip().lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True, role="admin")
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True)
    role = models.CharField(
        max_length=10, choices=[("admin", "Admin"), ("operator", "Operator")], default="operator"
    )
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="user_email_case_insensitive")]


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, max_length=4000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="unique_project_name_per_owner")
        ]


def default_retry_policy():
    return {"max_attempts": 3, "base_delay": 1, "max_delay": 300, "jitter": 0.2}


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, related_name="jobs", on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    task_type = models.CharField(max_length=40, default="python_demo")
    payload_template = models.JSONField(default=dict)
    retry_policy = models.JSONField(default=default_retry_policy)
    timeout_seconds = models.PositiveIntegerField(default=60)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_job_name_per_project")
        ]


class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, related_name="runs", on_delete=models.CASCADE)
    state = models.CharField(
        max_length=12,
        choices=[(s.value, s.value) for s in State],
        default=State.PENDING,
        db_index=True,
    )
    # Immutable snapshots ensure later edits to a job cannot change an accepted execution.
    task_type = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    retry_policy = models.JSONField(default=default_retry_policy)
    timeout_seconds = models.PositiveIntegerField(default=60)
    trace_id = models.CharField(max_length=32)
    trace_context = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=200, null=True, blank=True)
    request_fingerprint = models.CharField(max_length=64)
    attempt_count = models.PositiveIntegerField(default=0)
    cycle_attempt_count = models.PositiveIntegerField(default=0)
    result = models.JSONField(null=True)
    last_error = models.TextField(blank=True)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True, db_index=True)
    next_attempt_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def move_to(self, target):
        self.state = transition(self.state, target)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "idempotency_key"], name="unique_job_idempotency_key"
            )
        ]


class Attempt(models.Model):
    run = models.ForeignKey(Run, related_name="attempts", on_delete=models.CASCADE)
    number = models.PositiveIntegerField()
    lease_token = models.UUIDField(unique=True)
    state = models.CharField(max_length=12, default=State.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["number"]
        constraints = [models.UniqueConstraint(fields=["run", "number"], name="unique_run_attempt")]


class Outbox(models.Model):
    run = models.OneToOneField(Run, related_name="outbox", on_delete=models.CASCADE)
    available_at = models.DateTimeField(db_index=True)
    published_at = models.DateTimeField(null=True)


class RunLog(models.Model):
    run = models.ForeignKey(Run, related_name="logs", on_delete=models.CASCADE)
    timestamp = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=10, default="info")
    event = models.CharField(max_length=100)
    message = models.TextField(blank=True)
    trace_id = models.CharField(max_length=32)
    attempt = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["run", "id"])]


class APIKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="api_keys", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=100)
    digest = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["-created_at", "id"]
