import json
from pathlib import Path
from django.core.management.base import BaseCommand
from corpus.importers import import_qualcoder


class Command(BaseCommand):
    help = "Read a QualCoder project, importing corpus and initial codebook without annotations"

    def add_arguments(self, parser):
        parser.add_argument("project")
        parser.add_argument("--taxonomy")
        parser.add_argument("--registry")

    def handle(self, *args, **options):
        taxonomy = (
            json.loads(Path(options["taxonomy"]).read_text())
            if options["taxonomy"]
            else None
        )
        registry = (
            json.loads(Path(options["registry"]).read_text())
            if options["registry"]
            else None
        )
        count = import_qualcoder(options["project"], taxonomy, registry)
        self.stdout.write(f"Imported {count} documents")
