from django.core.management.base import BaseCommand
from corpus.importers import import_zip


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("archive")

    def handle(self, *args, **options):
        self.stdout.write(f"Imported {import_zip(options['archive'])} documents")
