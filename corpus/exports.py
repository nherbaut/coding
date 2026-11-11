"""Portable export formatting shared by report and submission downloads."""

import json


def csv_cell(value):
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    value = str(value) if value is not None else ""
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
