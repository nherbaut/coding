from django.core.management.base import BaseCommand
from corpus.importers import import_zip
from corpus.models import Study


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("archive")
        parser.add_argument("--study", required=True)

    def handle(self, *args, **options):
        self.stdout.write(
            f"Imported {import_zip(Study.objects.get(pk=options['study']), options['archive'])} documents"
        )
