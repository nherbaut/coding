"""Portable repository format and deterministic, independently replayable codebooks."""

from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath, Path
import uuid
import jsonpatch
import jsonpointer
import yaml
from .services import validate_book, span


def dump(value):
    return yaml.safe_dump(value, sort_keys=True, allow_unicode=True)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def blob_sha(text):
    raw = text.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid repository path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError(f"Unsafe repository path: {value}")
    return path


def coding_path(path):
    return str(PurePosixPath(path).with_suffix(".coding"))


def corpus_paths(files):
    result = []
    for path in sorted(files):
        parts = safe_path(path).parts
        if parts[0] != "corpus":
            continue
        if len(parts) < 3 or parts[1].endswith(".coding"):
            raise ValueError(f"Corpus files must be inside a case directory: {path}")
        if any(part.endswith(".coding") for part in parts[2:-1]):
            continue
        if parts[-1].endswith(".coding"):
            raise ValueError(
                f"The .coding suffix is reserved for metadata directories: {path}"
            )
        result.append(path)
    used = {}
    for path in result:
        coding = coding_path(path)
        if coding in used or any(other.startswith(coding + "/") for other in result):
            raise ValueError(
                f"Coding directory collision: {path} and {used.get(coding, coding)}"
            )
        used[coding] = path
    return result


def load_yaml(text):
    try:
        value = yaml.safe_load(text)
        json.dumps(value, allow_nan=False)

        def check(item):
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("Metadata mapping keys must be strings")
                for nested in item.values():
                    check(nested)
            elif isinstance(item, list):
                for nested in item:
                    check(nested)

        check(value)
        return value
    except (yaml.YAMLError, TypeError, ValueError):
        raise ValueError(
            "Invalid YAML: metadata must use JSON-compatible scalar values"
        ) from None


def replay_codebook(files):
    if "codebook/codebook.yaml" not in files:
        raise ValueError("Missing codebook/codebook.yaml")
    current = validate_book(load_yaml(files["codebook/codebook.yaml"]))
    history = load_yaml(files.get("codebook/history.yaml", "version: 1\nchanges: []\n"))
    if (
        not isinstance(history, dict)
        or history.get("version") != 1
        or not isinstance(history.get("changes"), list)
    ):
        raise ValueError("Expected codebook history version 1")
    if "codebook/codebook-base.yaml" not in files and history["changes"]:
        raise ValueError("A codebook history requires codebook-base.yaml")
    base = validate_book(
        load_yaml(
            files.get("codebook/codebook-base.yaml", files["codebook/codebook.yaml"])
        )
    )
    state = deepcopy(base)
    used = set()
    try:
        for sequence, entry in enumerate(history["changes"], 1):
            path = str(safe_path(entry["path"]))
            if (
                not path.startswith("codebook/diffs/")
                or not path.endswith("/change.yaml")
                or path in used
                or entry["sequence"] != sequence
            ):
                raise ValueError("Invalid or duplicated codebook history entry")
            used.add(path)
            change = load_yaml(files[path])
            if (
                change["sequence"] != sequence
                or entry["before"] != digest(state)
                or change["before"] != digest(state)
            ):
                raise ValueError(
                    "Broken codebook history: input hash or sequence differs"
                )
            state = jsonpatch.apply_patch(state, change["operations"], in_place=False)
            validate_book(state)
            if entry["after"] != digest(state) or change["after"] != digest(state):
                raise ValueError("Broken codebook history: result hash differs")
    except (
        KeyError,
        IndexError,
        TypeError,
        jsonpatch.JsonPatchException,
        jsonpointer.JsonPointerException,
    ) as error:
        raise ValueError("Invalid codebook patch chain") from error
    unlisted = {
        path
        for path in files
        if path.startswith("codebook/diffs/") and path.endswith("/change.yaml")
    } - used
    if unlisted:
        raise ValueError("Codebook diff directories are missing from history.yaml")
    if digest(state) != digest(current):
        raise ValueError("Applying codebook history does not reconstruct codebook.yaml")
    return base, current, history


def inspect_repository(files, repository_id=0):
    paths = corpus_paths(files)
    if not paths:
        raise ValueError("The repository corpus has no UTF-8 documents")
    base, book, history = replay_codebook(files)
    manifest = load_yaml(files.get("codebook/study.yaml", "version: 1"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("Expected study manifest version 1")
    cases = {
        key: dict(key=key, name=key, metadata={})
        for key in sorted({PurePosixPath(path).parts[1] for path in paths})
    }
    listed_cases = manifest.get("cases", [])
    if not isinstance(listed_cases, list):
        raise ValueError("Manifest cases must be a list")
    seen = set()
    for case in listed_cases:
        key = case["key"]
        if (
            key not in cases
            or key in seen
            or not isinstance(case.get("metadata", {}), dict)
        ):
            raise ValueError("Invalid or duplicated manifest case")
        seen.add(key)
        name = case.get("name", key)
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise ValueError("Case names must contain 1 to 500 characters")
        cases[key].update(name=case.get("name", key), metadata=case.get("metadata", {}))
    described = {}
    for doc in manifest.get("documents", []):
        path = str(safe_path(doc["path"]))
        if path not in paths or path in described:
            raise ValueError("Invalid or duplicated manifest document path")
        described[path] = doc
    if any(len(key) > 255 for key in cases):
        raise ValueError("Case directory names exceed 255 characters")
    documents = []
    identities = set()
    for path in paths:
        text = files[path]
        if "\x00" in text:
            raise ValueError(f"Binary text is not supported: {path}")
        doc = described.get(path, {})
        identity = doc.get(
            "id", str(uuid.uuid5(uuid.NAMESPACE_URL, f"github:{repository_id}:{path}"))
        )
        if not isinstance(identity, str) or identity in identities:
            raise ValueError("Document identities must be unique strings")
        identities.add(identity)
        metadata = doc.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Document metadata must be a mapping")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if doc.get("hash", sha) != sha:
            raise ValueError(f"Manifest document hash differs: {path}")
        name = doc.get("name", PurePosixPath(path).name)
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 500
            or len(path) > 500
        ):
            raise ValueError(
                "Document names and paths must contain at most 500 characters"
            )
        documents.append(
            dict(
                id=identity,
                path=path,
                case=PurePosixPath(path).parts[1],
                name=doc.get("name", PurePosixPath(path).name),
                metadata=metadata,
                text=text,
                hash=sha,
            )
        )
    by_path = {d["path"]: d for d in documents}

    def states():
        state = base
        yield state
        for entry in history["changes"]:
            state = jsonpatch.apply_patch(
                state, load_yaml(files[entry["path"]])["operations"], in_place=False
            )
            yield state

    for codebook in states():
        for code in codebook["codes"]:
            for ref in code.get("references", []):
                if ref.get("type") == "corpus":
                    doc = by_path.get(ref.get("document"))
                    if (
                        not doc
                        or ref.get("hash") != doc["hash"]
                        or span(doc["text"], ref["start"], ref["end"]) != ref["quote"]
                    ):
                        raise ValueError(
                            "Corpus references must use repository document paths, exact hashes and UTF-16 passages"
                        )
    return dict(
        base=base,
        book=book,
        history=history,
        cases=list(cases.values()),
        documents=documents,
        manifest=manifest,
    )


def convert_book(book, paths, *, importing=False):
    result = deepcopy(book)
    for code in result["codes"]:
        for ref in code.get("references", []):
            if ref.get("type") == "corpus":
                key = ref.get("document")
                if importing:
                    candidates = [
                        (pk, item) for pk, item in paths.items() if item["path"] == key
                    ]
                    if not candidates:
                        raise ValueError("Unknown repository document reference")
                    ref["document"] = int(candidates[0][0])
                else:
                    if str(key) not in paths:
                        raise ValueError("Unknown document in submitted codebook")
                    ref["document"] = paths[str(key)]["path"]
    return result


def external_id(study_id, kind, pk):
    return str(uuid.uuid5(uuid.UUID(str(study_id)), f"{kind}:{pk}"))


def verify_directory(directory):
    root = Path(directory).resolve()
    files = {}
    for path in (root / "codebook").rglob("*.yaml"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Symlinks are not allowed in codebook history")
        files[path.relative_to(root).as_posix()] = path.read_bytes().decode("utf-8")
    return replay_codebook(files)[1]


def make_patch(before, after):
    """Deterministic RFC 6902 add/remove/replace, independent of hash seed.

    jsonpatch's diff builder iterates sets; its apply/validation implementation
    is used for replay, but stored immutable patch files need stable generation.
    """
    operations = []

    def child(path, key):
        return path + "/" + str(key).replace("~", "~0").replace("/", "~1")

    def compare(source, target, path=""):
        if isinstance(source, dict) and isinstance(target, dict):
            for key in sorted(source.keys() - target.keys()):
                operations.append(dict(op="remove", path=child(path, key)))
            for key in sorted(target.keys() - source.keys()):
                operations.append(
                    dict(op="add", path=child(path, key), value=deepcopy(target[key]))
                )
            for key in sorted(source.keys() & target.keys()):
                compare(source[key], target[key], child(path, key))
        elif isinstance(source, list) and isinstance(target, list):
            for index in range(min(len(source), len(target))):
                compare(source[index], target[index], child(path, index))
            for index in range(len(source) - 1, len(target) - 1, -1):
                operations.append(dict(op="remove", path=child(path, index)))
            for index in range(len(source), len(target)):
                operations.append(
                    dict(op="add", path=path + "/-", value=deepcopy(target[index]))
                )
        elif type(source) is not type(target) or source != target:
            operations.append(dict(op="replace", path=path, value=deepcopy(target)))

    compare(before, after)
    return operations
