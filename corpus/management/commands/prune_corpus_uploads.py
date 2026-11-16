import datetime
from django.core.management.base import BaseCommand
from django.db import transaction, connection
from django.utils import timezone
from corpus.models import CorpusUploadBatch


def prune():
    with transaction.atomic():
        expired = CorpusUploadBatch.objects.filter(
            committed_at__isnull=True,
            created__lt=timezone.now() - datetime.timedelta(hours=1),
        ).select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked
        )
        ids = list(expired.values_list("pk", flat=True))
        CorpusUploadBatch.objects.filter(pk__in=ids).delete()
        return len(ids)


class Command(BaseCommand):
    help = "Delete expired corpus upload previews; retain imported batch receipts"

    def handle(self, *args, **options):
        self.stdout.write(f"Removed {prune()} expired upload previews")
