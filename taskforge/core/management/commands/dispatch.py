import time

from django.core.management.base import BaseCommand

from taskforge.core.services import maintain


class Command(BaseCommand):
    help = "Recover expired leases and publish due outbox entries (independent of Redis)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        while True:
            maintain()
            if options["once"]:
                return
            time.sleep(2)
