"""Lossless bounded storage for large internal snapshots, without JSONB parse spikes.

Small/existing JSON values remain readable. Searchable batch metadata stays plain;
only its files array is packed. Repository/sync maps are packed as a whole.
"""

import base64
import binascii
import json
import zlib
from django.db import models
from django.db.models.fields.json import KeyTransform

MARKER = "$coding:zlib-json:v1"
THRESHOLD = 64 * 1024
MAX_PAYLOAD = 256 * 1024 * 1024


def pack(value):
    raw = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(raw) <= THRESHOLD:
        return value
    if len(raw) > MAX_PAYLOAD:
        raise ValueError("Snapshot exceeds the supported storage size")
    return {MARKER: base64.b64encode(zlib.compress(raw)).decode("ascii")}


def unpack(value):
    if not isinstance(value, dict) or set(value) != {MARKER}:
        return value
    try:
        compressed = base64.b64decode(value[MARKER], validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_PAYLOAD + 1)
        if (
            len(raw) > MAX_PAYLOAD
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise ValueError("Invalid packed snapshot")
        return json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, zlib.error, binascii.Error):
        raise ValueError("Cannot read the packed snapshot") from None


class PackedJSONField(models.JSONField):
    def __init__(self, *args, packed_key=None, **kwargs):
        self.packed_key = packed_key
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.packed_key is not None:
            kwargs["packed_key"] = self.packed_key
        return name, path, args, kwargs

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if self.packed_key:
            if isinstance(value, dict) and self.packed_key in value:
                return {**value, self.packed_key: pack(value[self.packed_key])}
            return value
        return pack(value) if isinstance(value, (dict, list)) else value

    def from_db_value(self, value, expression, connection):
        value = super().from_db_value(value, expression, connection)
        if isinstance(expression, KeyTransform):
            return unpack(value)
        if self.packed_key and isinstance(value, dict):
            if self.packed_key in value:
                return {**value, self.packed_key: unpack(value[self.packed_key])}
            return value
        return unpack(value)
