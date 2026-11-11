"""Convert a corpus ZIP + YAML into the normal portable GitHub data format."""

import zipfile
import yaml
from pathlib import PurePosixPath
from urllib.parse import quote
from django.conf import settings
from .github_repository import safe_path, load_yaml, dump, inspect_repository
from .services import load_book


def uploaded_repository(corpus, codebook):
    if not corpus or not codebook:
        raise ValueError("Upload both a corpus ZIP and a codebook YAML")
    if codebook.size > settings.GITHUB_IMPORT_MAX_BYTES:
        raise ValueError("Codebook exceeds the import size limit")
    try:
        book = load_book(codebook.read().decode("utf-8-sig"))
        with zipfile.ZipFile(corpus) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise ValueError("Corpus ZIP contains duplicate paths")
            if sum(item.file_size for item in infos) > settings.GITHUB_IMPORT_MAX_BYTES:
                raise ValueError("Uncompressed corpus exceeds 100 MB")
            for item in infos:
                safe_path(item.filename)
                if ((item.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError("Corpus ZIP cannot contain symlinks")
            if "manifest.yaml" in names:
                manifest = load_yaml(archive.read("manifest.yaml").decode("utf-8-sig"))
                if not isinstance(manifest, dict) or manifest.get("version") != 1:
                    raise ValueError("Expected corpus manifest version 1")
                cases = []
                folders = {}
                seen = set()
                for item in manifest["cases"]:
                    identity = str(item["id"])
                    folder = quote(identity, safe=" -_.")
                    if not folder or folder in (".", "..") or identity in folders:
                        raise ValueError("Invalid or duplicated case identifier")
                    folders[identity] = folder
                    cases.append(
                        dict(
                            key=folder,
                            name=item["name"],
                            metadata=item.get("metadata", {}),
                        )
                    )
                files = {}
                documents = []
                references = {}
                for item in manifest["documents"]:
                    identity = str(item["id"])
                    if identity in seen:
                        raise ValueError("Corpus document identifiers must be unique")
                    seen.add(identity)
                    source = str(safe_path(item["path"]))
                    text = archive.read(source).decode("utf-8")
                    memberships = item["cases"]
                    if not isinstance(memberships, list) or not memberships:
                        raise ValueError("Every document must belong to a case")
                    for case in memberships:
                        folder = folders[str(case)]
                        path = f"corpus/{folder}/{PurePosixPath(source).name}"
                        if path in files:
                            raise ValueError(
                                "Several documents would share a repository path: "
                                + path
                            )
                        files[path] = text
                        documents.append(
                            dict(
                                id=identity + "@" + folder,
                                path=path,
                                name=item["name"],
                                metadata=item.get("metadata", {}),
                            )
                        )
                        references.setdefault(identity, path)
                files["codebook/study.yaml"] = dump(
                    dict(version=1, cases=cases, documents=documents)
                )
                for code in book["codes"]:
                    for reference in code.get("references", []):
                        if (
                            reference.get("type") == "corpus"
                            and str(reference.get("document")) in references
                        ):
                            reference["document"] = references[
                                str(reference["document"])
                            ]
            else:
                files = {
                    name: archive.read(name).decode("utf-8")
                    for name in names
                    if name.startswith("corpus/")
                }
                if any(not name.startswith("corpus/") for name in names):
                    raise ValueError(
                        "ZIP must contain manifest.yaml or a corpus/<case>/<file> layout"
                    )
        files["codebook/codebook.yaml"] = dump(book)
        files["codebook/codebook-base.yaml"] = dump(book)
        files["codebook/history.yaml"] = dump(dict(version=1, changes=[]))
        if (
            sum(len(value.encode("utf-8")) for value in files.values())
            > settings.GITHUB_IMPORT_MAX_BYTES
        ):
            raise ValueError("Combined corpus and codebook exceed 100 MB")
        inspect_repository(files)
        return files
    except (
        zipfile.BadZipFile,
        UnicodeDecodeError,
        KeyError,
        TypeError,
        yaml.YAMLError,
    ) as error:
        raise ValueError(
            "Invalid corpus ZIP or YAML: use UTF-8 documents and a valid corpus manifest"
        ) from error
