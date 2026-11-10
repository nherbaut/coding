from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
import zipfile
import yaml
from django.db import transaction
from .models import Case, Document, DocumentVersion, SharedCodebook
from .services import validate_book


def put_document(key, name, text, metadata, cases):
    document, _ = Document.objects.get_or_create(key=key, defaults={"name": name})
    digest = hashlib.sha256(text.encode()).hexdigest()
    version, _ = DocumentVersion.objects.get_or_create(
        document=document, sha256=digest, defaults={"text": text, "metadata": metadata}
    )
    document.cases.add(*cases)
    return version


@transaction.atomic
def import_qualcoder(project, taxonomy=None, registry=None):
    connection = sqlite3.connect(
        (Path(project).resolve() / "data.qda").as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        data = {
            table: [dict(row) for row in connection.execute("SELECT * FROM " + table)]
            for table in ("source", "cases", "case_text", "code_name", "code_cat")
        }
    finally:
        connection.close()
    cases = {}
    for row in data["cases"]:
        cases[row["caseid"]], _ = Case.objects.get_or_create(
            key="qda-case-" + str(row["caseid"]),
            defaults={"name": row["name"], "metadata": row},
        )
    for row in data["source"]:
        if row["fulltext"] is None:
            continue
        memberships = {r["caseid"] for r in data["case_text"] if r["fid"] == row["id"]}
        put_document(
            "qda-source-" + str(row["id"]),
            row["name"],
            row["fulltext"],
            {k: v for k, v in row.items() if k != "fulltext"},
            [cases[k] for k in memberships],
        )
    known = (registry or {}).get("entities", {})
    ids = {(v["kind"], v["id"]): key for key, v in known.items()}
    for kind, table, pk in [
        ("code", "code_name", "cid"),
        ("category", "code_cat", "catid"),
    ]:
        for row in data[table]:
            ids.setdefault((kind, row[pk]), str(uuid.uuid4()))
    codes = []
    for kind, table, pk in [
        ("category", "code_cat", "catid"),
        ("code", "code_name", "cid"),
    ]:
        for row in data[table]:
            parent = row.get("supercid" if kind == "code" else "supercatid")
            parent_id = (
                ids.get((kind, parent))
                if parent
                else ids.get(("category", row.get("catid"))) if kind == "code" else None
            )
            codes.append(
                dict(
                    id=ids[kind, row[pk]],
                    kind=kind,
                    name=row["name"],
                    memo=row.get("memo") or "",
                    parent=parent_id,
                    references=[],
                    rationale="",
                    archived=False,
                    provenance={"qualcoder": row},
                )
            )
    book = validate_book(
        dict(version=1, codes=codes, provenance={"taxonomy": deepcopy(taxonomy or {})})
    )
    if not SharedCodebook.objects.exists():
        SharedCodebook.objects.create(content=book)
    return len(data["source"])


@transaction.atomic
def import_zip(file):
    with zipfile.ZipFile(file) as archive:
        if sum(info.file_size for info in archive.infolist()) > 100 * 1024 * 1024:
            raise ValueError("Uncompressed corpus exceeds 100 MB")
        manifest = yaml.safe_load(archive.read("manifest.yaml"))
        if manifest.get("version") != 1:
            raise ValueError("Expected corpus manifest version 1")
        cases = {}
        for item in manifest["cases"]:
            cases[item["id"]], _ = Case.objects.get_or_create(
                key=item["id"],
                defaults={"name": item["name"], "metadata": item.get("metadata", {})},
            )
        for item in manifest["documents"]:
            text = archive.read(item["path"]).decode("utf-8-sig")
            put_document(
                item["id"],
                item["name"],
                text,
                item.get("metadata", {}),
                [cases[k] for k in item["cases"]],
            )
        return len(manifest["documents"])
