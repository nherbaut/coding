from django.core.management.base import BaseCommand, CommandError
from corpus.github_repository import verify_directory, dump, digest


class Command(BaseCommand):
    help = "Reconstruct codebook-base + ordered patches and verify codebook.yaml (no database required)"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("directory")
        parser.add_argument("--print-codebook", action="store_true")

    def handle(self, *args, **options):
        try:
            book = verify_directory(options["directory"])
        except (ValueError, OSError) as error:
            raise CommandError(str(error)) from error
        if options["print_codebook"]:
            self.stdout.write(dump(book))
        else:
            self.stdout.write("Verified codebook: " + digest(book))
