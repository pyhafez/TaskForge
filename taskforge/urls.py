from django.urls import include, path, re_path
from django.views.generic import TemplateView
from rest_framework.routers import DefaultRouter

from taskforge.core import errors, views

router = DefaultRouter(trailing_slash=False)
router.register("projects", views.ProjectViewSet, basename="project")
router.register("jobs", views.JobViewSet, basename="job")
router.register("runs", views.RunViewSet, basename="run")
router.register("api-keys", views.APIKeyViewSet, basename="api-key")
router.register("users", views.UserViewSet, basename="user")

urlpatterns = [
    path("", TemplateView.as_view(template_name="dashboard.html")),
    path("healthz", views.HealthView.as_view()),
    path("api/auth/register", views.RegisterView.as_view()),
    path("api/auth/login", views.LoginView.as_view()),
    path("api/auth/me", views.MeView.as_view()),
    path("api/", include(router.urls)),
    re_path(r"^.*$", errors.not_found),
]
handler404 = errors.not_found
handler500 = errors.server_error
