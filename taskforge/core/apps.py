from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "taskforge.core"

    def ready(self):
        from taskforge.observability import configure_logging, configure_tracing

        configure_logging()
        configure_tracing()
