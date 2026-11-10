from copy import deepcopy
import re
import uuid
from django.test import SimpleTestCase
from .colors import code_color, colored_book, highlighted_segments
from .services import load_book, validate_book
import yaml


class CodeColorTests(SimpleTestCase):
    def setUp(self):
        self.code = dict(id=str(uuid.uuid4()), name="Review", kind="code", parent=None)

    def test_defaults_are_stable_across_renaming_and_export(self):
        color = code_color(self.code)
        renamed = {**self.code, "name": "Other name"}
        self.assertEqual(code_color(renamed), color)
        self.assertRegex(color, r"^#[0-9a-f]{6}$")
        book = dict(version=1, codes=[self.code])
        exported = colored_book(book)
        self.assertNotIn("color", self.code)
        self.assertEqual(exported["codes"][0]["color"], color)
        self.assertEqual(load_book(yaml.safe_dump(exported)), exported)

    def test_custom_colors_and_invalid_styles(self):
        self.code["color"] = "#AB12CD"
        self.assertEqual(code_color(self.code), "#ab12cd")
        validate_book(dict(version=1, codes=[self.code]))
        for invalid in ("red", "#fff", "url(https://example.org)", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_book(dict(version=1, codes=[{**self.code, "color": invalid}]))

    def test_overlaps_show_all_colors_without_changing_unicode_text(self):
        first = {**self.code, "color": "#ff0000"}
        second = dict(id=str(uuid.uuid4()), name="Second", kind="code", color="#0000ff")
        annotations = [
            dict(start=1, end=3, codes=[first["id"]]),
            dict(start=0, end=3, codes=[second["id"], first["id"]]),
        ]
        segments = highlighted_segments("A😀B", annotations, [first, second])
        self.assertEqual("".join(s["text"] for s in segments), "A😀B")
        emoji = next(s for s in segments if s["text"] == "😀")
        self.assertIn("#ff0000", emoji["style"])
        self.assertIn("#0000ff", emoji["style"])
        self.assertEqual(emoji["style"].count("#ff0000"), 2)
        self.assertEqual(segments[-1]["style"], "")
