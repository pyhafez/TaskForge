import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taskforge.settings")
app = Celery("taskforge")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["taskforge.core"])
