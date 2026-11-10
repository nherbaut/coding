import logging
import signal
import threading
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, ProgrammingError, close_old_connections
from corpus.notifications import deliver_next_email

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Deliver queued transactional emails, retrying temporary SMTP failures"

    def add_arguments(self, parser):
        parser.add_argument("--watch", action="store_true")
        parser.add_argument("--interval", type=int, default=10)

    def handle(self, *args, **options):
        if not 1 <= options["interval"] <= 60:
            raise CommandError("Interval must be between 1 and 60 seconds")
        stop = threading.Event()
        if options["watch"]:
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            try:
                close_old_connections()
                for _ in range(50):
                    if stop.is_set() or not deliver_next_email():
                        break
            except (OperationalError, ProgrammingError):
                if not options["watch"]:
                    raise
                logger.warning("Notification database unavailable; retrying shortly")
                close_old_connections()
            if not options["watch"]:
                return
            stop.wait(options["interval"])
