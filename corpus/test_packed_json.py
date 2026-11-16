import json
from unittest.mock import patch
from django.db import connection
from django.test import SimpleTestCase, TestCase
from .packed_json import pack, unpack, MARKER
from .models import CorpusUploadBatch
from .test_support import new_study


class PackedValueTests(SimpleTestCase):
    def test_large_unicode_nested_values_roundtrip_without_losing_order(self):
        value = {
            "files": [
                {
                    "text": '😀 quoted "text"\r\n' * 15000,
                    "refs": [None, True, 3.25, {"memo": "Source"}],
                }
            ]
        }
        encoded = pack(value)
        self.assertIn(MARKER, encoded)
        self.assertLess(len(json.dumps(encoded)), len(json.dumps(value)) // 10)
        self.assertEqual(unpack(encoded), value)
        self.assertEqual(unpack({"old": "plain JSON"}), {"old": "plain JSON"})

    def test_corrupt_truncated_and_oversized_payloads_are_rejected(self):
        with self.assertRaises(ValueError):
            unpack({MARKER: "not base64"})
        encoded = pack({"text": "a" * 100000})
        with patch("corpus.packed_json.MAX_PAYLOAD", 10):
            with self.assertRaises(ValueError):
                unpack(encoded)
        with self.assertRaises(ValueError):
            unpack({MARKER: encoded[MARKER][:-4]})


class PackedFieldTests(TestCase):
    def test_old_rows_new_rows_queries_and_repeat_save_are_lossless(self):
        study = new_study()
        data = {
            "mode": "cases",
            "files": [{"text": "Unicode 😀\r\n" * 30000}],
            "case": {"name": "Case"},
        }
        batch = CorpusUploadBatch.objects.create(
            study=study, created_by=study.owner, data=data
        )
        batch.refresh_from_db()
        self.assertEqual(batch.data, data)
        batch.save()
        batch.refresh_from_db()
        self.assertEqual(batch.data, data)
        self.assertTrue(CorpusUploadBatch.objects.filter(data__mode="cases").exists())
        self.assertTrue(
            CorpusUploadBatch.objects.exclude(data__has_key="parent_batch").exists()
        )
        # Inspect physical JSON without the field decoder.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT data FROM corpus_corpusuploadbatch WHERE id=%s",
                [batch.pk if connection.vendor == "postgresql" else batch.pk.hex],
            )
            raw = cursor.fetchone()[0]
        raw = json.loads(raw) if isinstance(raw, str) else raw
        self.assertIn(MARKER, raw["files"])
        # Existing stored JSON is read without rewriting it.
        plain = json.dumps({"mode": "old", "files": [{"text": "Existing evidence"}]})
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE corpus_corpusuploadbatch SET data=%s WHERE id=%s",
                [
                    plain,
                    batch.pk if connection.vendor == "postgresql" else batch.pk.hex,
                ],
            )
        batch.refresh_from_db()
        self.assertEqual(batch.data["files"][0]["text"], "Existing evidence")
