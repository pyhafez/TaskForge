from django.contrib.auth import authenticate
from django.db import connection, transaction
from django.utils import timezone
from redis import Redis
from rest_framework import permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from taskforge.config import env
from taskforge.core import services
from taskforge.core.authentication import create_key, issue_token
from taskforge.core.models import APIKey, Job, Project, Run, RunLog, User
from taskforge.core.serializers import (
    APIKeySerializer,
    AttemptSerializer,
    JobSerializer,
    LoginSerializer,
    ProjectSerializer,
    RegisterSerializer,
    RunLogSerializer,
    RunSerializer,
    SubmitSerializer,
    UserSerializer,
)
from taskforge.domain import State


def is_admin(user):
    return user.role == "admin" or user.is_superuser


def visible_projects(user):
    return Project.objects.all() if is_admin(user) else Project.objects.filter(owner=user)


def visible_jobs(user):
    return Job.objects.filter(project__in=visible_projects(user))


class AdminOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and is_admin(request.user)


class HealthView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        dependencies = {}
        healthy = True
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            db_status = "ok"
        except Exception:
            db_status, healthy = "unavailable", False
        if connection.vendor == "postgresql" or not healthy:
            dependencies["database"] = db_status
        if env.redis_url:
            try:
                with Redis.from_url(
                    env.redis_url, socket_timeout=1, socket_connect_timeout=1
                ) as redis:
                    redis.ping()
                dependencies["redis"] = "ok"
            except Exception:
                dependencies["redis"], healthy = "unavailable", False
        data = {"status": "ok" if healthy else "degraded"}
        if dependencies:
            data["dependencies"] = dependencies
        return Response(data, status=200 if healthy else 503)


class PublicAuthView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def get_authenticate_header(self, request):
        return "Bearer"


class RegisterView(PublicAuthView):
    def post(self, request):
        if not env.registration_open:
            raise PermissionDenied("Registration is closed.")
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            user = User.objects.create_user(**serializer.validated_data)
        return Response(UserSerializer(user).data, status=201)


class LoginView(PublicAuthView):
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            email=serializer.validated_data["email"].lower(),
            password=serializer.validated_data["password"],
        )
        if not user:
            raise AuthenticationFailed("Invalid email or password.")
        return Response(
            {
                "access_token": issue_token(user),
                "token_type": "bearer",
                "expires_in": env.jwt_ttl_minutes * 60,
            }
        )


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class AtomicModelViewSet(viewsets.ModelViewSet):
    @transaction.atomic
    def perform_create(self, serializer):
        serializer.save()

    @transaction.atomic
    def perform_update(self, serializer):
        serializer.save()


class ProjectViewSet(AtomicModelViewSet):
    serializer_class = ProjectSerializer

    def get_queryset(self):
        return visible_projects(self.request.user)

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["get", "post"])
    def jobs(self, request, pk=None):
        project = self.get_object()
        if request.method == "POST":
            serializer = JobSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            with transaction.atomic():
                project = Project.objects.select_for_update().get(pk=project.pk)
                serializer.save(project=project)
            return Response(serializer.data, status=201)
        page = self.paginate_queryset(project.jobs.all())
        return self.get_paginated_response(JobSerializer(page, many=True).data)


class JobViewSet(AtomicModelViewSet):
    serializer_class = JobSerializer
    http_method_names = ["get", "put", "patch", "delete", "post", "head", "options"]

    def get_queryset(self):
        return visible_jobs(self.request.user)

    def create(self, request, *args, **kwargs):
        raise ValidationError({"project": "Create jobs at /api/projects/{id}/jobs."})

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        # Protect accepted snapshots from concurrent edits while submitting.
        self.get_queryset().select_for_update().get(pk=self.get_object().pk)
        return super().update(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        job = self.get_object()
        serializer = SubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run, created = services.submit(
            job,
            serializer.validated_data["payload"],
            request.headers.get("Idempotency-Key"),
            request.trace_id,
        )
        return Response(
            RunSerializer(run).data,
            status=202,
            headers={
                "Location": f"/api/runs/{run.id}",
                "Idempotency-Replayed": str(not created).lower(),
            },
        )

    @action(detail=True, methods=["get"])
    def logs(self, request, pk=None):
        job = self.get_object()
        return logs_response(RunLog.objects.filter(run__job=job), request)


def logs_response(queryset, request, run_state=None):
    query = serializers.Serializer(data=request.query_params)
    query.fields["after"] = serializers.IntegerField(min_value=0, default=0)
    query.fields["limit"] = serializers.IntegerField(min_value=1, max_value=500, default=100)
    query.is_valid(raise_exception=True)
    after = query.validated_data["after"]
    rows = list(queryset.filter(id__gt=after).order_by("id")[: query.validated_data["limit"]])
    next_cursor = rows[-1].id if rows else after
    return Response(
        {
            "results": RunLogSerializer(rows, many=True).data,
            "next_cursor": next_cursor,
            "has_more": queryset.filter(id__gt=next_cursor).exists(),
            "state": run_state,
        }
    )


class RunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RunSerializer

    def get_queryset(self):
        queryset = Run.objects.filter(job__in=visible_jobs(self.request.user))
        state_filter = self.request.query_params.get("state")
        job_id = self.request.query_params.get("job")
        if state_filter:
            if state_filter not in State:
                raise ValidationError({"state": "Unknown run state."})
            queryset = queryset.filter(state=state_filter)
        if job_id:
            field = serializers.UUIDField()
            queryset = queryset.filter(job_id=field.run_validation(job_id))
        return queryset

    @action(detail=True, methods=["get"])
    def logs(self, request, pk=None):
        run = self.get_object()
        return logs_response(run.logs.all(), request, run.state)

    @action(detail=True, methods=["get"])
    def attempts(self, request, pk=None):
        return Response(AttemptSerializer(self.get_object().attempts.all(), many=True).data)

    @action(detail=True, methods=["post"])
    def requeue(self, request, pk=None):
        run = services.requeue(self.get_object().pk)
        return Response(RunSerializer(run).data, status=202)


class APIKeyViewSet(viewsets.ModelViewSet):
    serializer_class = APIKeySerializer
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        return APIKey.objects.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        key, raw = create_key(request.user, serializer.validated_data["name"])
        return Response({**self.get_serializer(key).data, "key": raw}, status=201)

    def perform_destroy(self, instance):
        instance.revoked_at = timezone.now()
        instance.save(update_fields=["revoked_at"])


class UserViewSet(viewsets.ModelViewSet):
    permission_classes = [AdminOnly]
    serializer_class = UserSerializer
    queryset = User.objects.order_by("id")
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def perform_update(self, serializer):
        if serializer.instance.pk == self.request.user.pk:
            raise PermissionDenied("Use a different administrator to change your own access.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.pk == self.request.user.pk:
            raise PermissionDenied("You cannot deactivate yourself.")
        instance.is_active = False
        instance.save(update_fields=["is_active"])
