"""Bounded UTF-8 document uploads; ZIP members are never extracted to disk."""

import hashlib
import io
import stat
import struct
import zipfile
import zlib
from pathlib import PurePosixPath, PureWindowsPath
from .github_repository import safe_path

MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 100
MAX_ZIP_ENTRIES = 1000


def valid_name(name):
    if (
        not isinstance(name, str)
        or not name.strip()
        or name != name.strip()
        or len(name) > 250
    ):
        raise ValueError(
            "File names must contain 1 to 250 characters without surrounding spaces"
        )
    if "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
        raise ValueError(
            "Upload plain file names, without directories or control characters"
        )
    if PurePosixPath(name).suffix.lower() not in {".md", ".markdown", ".txt"}:
        raise ValueError(
            f"Unsupported file: {name}. Use UTF-8 .md, .markdown or .txt files."
        )
    safe_path(name)
    return name


def zip_members(
    raw,
    archive_name,
    *,
    max_files=None,
    max_entries=None,
    max_bytes=None,
    allow_manifest=False,
):
    file_limit = MAX_FILES if max_files is None else max_files
    entry_limit = MAX_ZIP_ENTRIES if max_entries is None else max_entries
    byte_limit = MAX_BYTES if max_bytes is None else max_bytes
    limit_label = "20 MB" if max_bytes is None else f"{byte_limit // (1024 * 1024)} MB"
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > entry_limit:
                raise ValueError(
                    f"ZIP archives must not contain more than {entry_limit} entries"
                )
            if sum(i.file_size for i in infos) > byte_limit:
                raise ValueError(
                    f"Uncompressed ZIP contents exceed the {limit_label} import limit"
                )
            members = []
            for info in infos:
                path = info.filename
                if (
                    info.orig_filename != path
                    or PureWindowsPath(path).drive
                    or any(ord(c) < 32 for c in path)
                ):
                    raise ValueError("ZIP contains an unsafe file path")
                parts = safe_path(path.rstrip("/") if info.is_dir() else path).parts
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (
                    stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                ):
                    raise ValueError(f"ZIP contains a link or special file: {path}")
                if stat.S_ISDIR(mode) and not info.is_dir():
                    raise ValueError(f"ZIP contains an invalid directory: {path}")
                if info.flag_bits & 1:
                    raise ValueError("Encrypted ZIP archives are not supported")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError("Use a standard stored or deflated ZIP archive")
                if (
                    info.is_dir()
                    or parts[0] == "__MACOSX"
                    or parts[-1] == ".DS_Store"
                    or parts[-1].startswith("._")
                ):
                    continue
                name = (
                    parts[-1]
                    if allow_manifest and path == "manifest.yaml"
                    else valid_name(parts[-1])
                )
                members.append((info, name))
            if len(members) > file_limit:
                raise ValueError(
                    f"An upload batch must not contain more than {file_limit} documents"
                )
            archive_hash = hashlib.sha256(raw).hexdigest()
            total = 0
            for info, name in members:
                with archive.open(info) as stream:
                    content = stream.read(byte_limit - total + 1)
                total += len(content)
                if total > byte_limit:
                    raise ValueError(
                        f"Uncompressed ZIP contents exceed the {limit_label} import limit"
                    )
                if len(content) != info.file_size:
                    raise ValueError("ZIP file size does not match its directory")
                yield name, content, dict(
                    name=archive_name, member=info.filename, sha256=archive_hash
                )
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        RuntimeError,
        NotImplementedError,
        OSError,
        EOFError,
        zlib.error,
        UnicodeError,
        struct.error,
    ):
        raise ValueError(
            f"Cannot read {archive_name}: the ZIP is corrupt, encrypted or unsupported"
        ) from None


def read_uploads(uploads, notes):
    files, uploaded_bytes, content_bytes = [], 0, 0
    for upload in uploads:
        raw = upload.read(MAX_BYTES + 1)
        uploaded_bytes += len(raw)
        if uploaded_bytes > MAX_BYTES:
            raise ValueError("An upload batch must not exceed 20 MB")
        if PurePosixPath(upload.name).suffix.lower() == ".zip":
            members = zip_members(raw, upload.name)
        else:
            members = [(valid_name(upload.name), raw, None)]
        for name, content, source_archive in members:
            if len(files) >= MAX_FILES:
                raise ValueError(
                    "An upload batch must not contain more than 100 documents"
                )
            content_bytes += len(content)
            if content_bytes > MAX_BYTES:
                raise ValueError(
                    "Uncompressed batch contents exceed the 20 MB import limit"
                )
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError(f"{name} is not UTF-8 text") from None
            if "\x00" in text or not text.strip("\ufeff \t\n\r"):
                raise ValueError(f"{name} is empty or contains binary data")
            metadata = dict(notes=notes, url="")
            if source_archive:
                metadata["source_archive"] = source_archive
            files.append(
                dict(
                    name=name,
                    text=text,
                    bytes=len(content),
                    hash=hashlib.sha256(content).hexdigest(),
                    metadata=metadata,
                )
            )
    if not files:
        raise ValueError("No supported documents were found in the upload")
    return files
