import json

from django.contrib.auth.password_validation import validate_password
from pydantic import ValidationError as PolicyValidationError
from rest_framework import serializers

from taskforge.core.models import APIKey, Attempt, Job, Project, Run, RunLog, User
from taskforge.domain import RetryPolicy
from taskforge.task_types import TASK_TYPES


def validate_payload(value):
    if not isinstance(value, dict):
        raise serializers.ValidationError("Must be a JSON object.")
    try:
        size = len(json.dumps(value, allow_nan=False).encode())
    except (ValueError, TypeError) as exc:
        raise serializers.ValidationError("Must contain finite JSON values.") from exc
    if size > 65_536:
        raise serializers.ValidationError("Payload exceeds 64 KiB.")
    return value


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=256, trim_whitespace=False)

    def validate(self, attrs):
        attrs["email"] = attrs["email"].lower()
        validate_password(attrs["password"], User(email=attrs["email"]))
        return attrs


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, max_length=256)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "role", "is_active"]
        read_only_fields = ["id", "email"]


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["id", "name", "description", "created_at"]
        read_only_fields = ["id", "created_at"]
        validators = []


class JobSerializer(serializers.ModelSerializer):
    task_type = serializers.ChoiceField(choices=list(TASK_TYPES), default="python_demo")
    timeout_seconds = serializers.IntegerField(min_value=1, max_value=3600, default=60)

    class Meta:
        model = Job
        fields = [
            "id",
            "project",
            "name",
            "task_type",
            "payload_template",
            "retry_policy",
            "timeout_seconds",
            "created_at",
        ]
        read_only_fields = ["id", "project", "created_at"]
        validators = []

    def validate_payload_template(self, value):
        return validate_payload(value)

    def validate_retry_policy(self, value):
        try:
            return RetryPolicy.model_validate(value).model_dump()
        except PolicyValidationError as exc:
            raise serializers.ValidationError("Invalid retry policy: " + str(exc)) from exc


class SubmitSerializer(serializers.Serializer):
    payload = serializers.JSONField(default=dict)

    def validate_payload(self, value):
        return validate_payload(value)


class AttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attempt
        fields = ["number", "state", "started_at", "finished_at", "error"]


class RunSerializer(serializers.ModelSerializer):
    class Meta:
        model = Run
        fields = [
            "id",
            "job",
            "state",
            "task_type",
            "payload",
            "retry_policy",
            "timeout_seconds",
            "trace_id",
            "attempt_count",
            "result",
            "last_error",
            "next_attempt_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class RunLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = RunLog
        fields = ["id", "run_id", "timestamp", "level", "event", "message", "trace_id", "attempt"]


class APIKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = APIKey
        fields = ["id", "name", "prefix", "created_at", "revoked_at"]
        read_only_fields = ["id", "prefix", "created_at", "revoked_at"]
