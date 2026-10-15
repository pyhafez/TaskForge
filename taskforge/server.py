import os

from django.core.management import call_command, execute_from_command_line


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taskforge.settings")
    import django

    django.setup()
    call_command("migrate", interactive=False)
    execute_from_command_line(["taskforge-server", "runserver", "127.0.0.1:8000"])
