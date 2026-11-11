import logging
import signal
import threading
from django.core.management.base import BaseCommand
from django.db import close_old_connections, OperationalError, ProgrammingError
from corpus.github_sync import synchronize_next

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Publish durable GitHub study events (use --watch for the worker)"

    def add_arguments(self, parser):
        parser.add_argument("--watch", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        if options["watch"]:
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            try:
                close_old_connections()
                for _ in range(20):
                    if stop.is_set() or not synchronize_next():
                        break
            except (OperationalError, ProgrammingError):
                if not options["watch"]:
                    raise
                logger.warning("GitHub synchronization database unavailable; retrying")
            except Exception:
                if not options["watch"]:
                    raise
                # Do not emit request objects, tokens or remote response bodies.
                logger.error(
                    "GitHub synchronization interrupted; the lease will expire and permit recovery"
                )
            if not options["watch"]:
                return
            stop.wait(5)
