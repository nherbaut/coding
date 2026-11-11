import hashlib
import zipfile
import yaml
from django.db import transaction
from .models import Case, Document, DocumentVersion


@transaction.atomic
def put_document(study, key, name, text, metadata, cases):
    document, _ = Document.objects.get_or_create(
        study=study, key=key, defaults={"name": name}
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    version, _ = DocumentVersion.objects.get_or_create(
        document=document, sha256=digest, defaults={"text": text, "metadata": metadata}
    )
    if not isinstance(metadata, dict):
        raise ValueError("Document metadata must be an object")
    document.cases.add(*cases)
    return version


@transaction.atomic
def import_zip(study, file):
    with zipfile.ZipFile(file) as archive:
        if sum(info.file_size for info in archive.infolist()) > 100 * 1024 * 1024:
            raise ValueError("Uncompressed corpus exceeds 100 MB")
        manifest = yaml.safe_load(archive.read("manifest.yaml"))
        if manifest.get("version") != 1:
            raise ValueError("Expected corpus manifest version 1")
        cases = {}
        for item in manifest["cases"]:
            cases[item["id"]], _ = Case.objects.get_or_create(
                study=study,
                key=item["id"],
                defaults={"name": item["name"], "metadata": item.get("metadata", {})},
            )
        for item in manifest["documents"]:
            text = archive.read(item["path"]).decode("utf-8-sig")
            put_document(
                study,
                item["id"],
                item["name"],
                text,
                item.get("metadata", {}),
                [cases[k] for k in item["cases"]],
            )
        return len(manifest["documents"])
