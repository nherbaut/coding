"""Research-facing descriptions of codebook differences."""

from .colors import code_color


def concept_path(identity, book):
    lookup = {code["id"]: code for code in book["codes"]}
    names = []
    seen = set()
    while identity in lookup and identity not in seen:
        seen.add(identity)
        names.insert(0, lookup[identity]["name"])
        identity = lookup[identity].get("parent")
    return " › ".join(names) if names else "Top level"


def reference_text(references):
    parts = []
    for reference in references:
        values = [
            reference.get(key) for key in ("title", "citation", "quote", "location")
        ]
        parts.append(
            " — ".join(str(value) for value in values if value)
            or "Supporting reference"
        )
    return "\n\n".join(parts)


def field_value(code, field, book):
    if code is None:
        return "—"
    if field == "parent":
        return concept_path(code.get("parent"), book)
    if field == "kind":
        return code["kind"].title()
    if field == "archived":
        return "Archived" if code.get("archived") else "Active"
    if field == "color":
        return code_color(code) if code["kind"] == "code" else ""
    if field == "references":
        return reference_text(code.get("references", [])) or "—"
    return code.get(field) or "—"


def describe_changes(changes, base, incoming, shared):
    labels = {
        "name": "Name",
        "kind": "Type",
        "parent": "Parent",
        "memo": "Memo",
        "rationale": "Researcher note",
        "color": "Highlight color",
        "archived": "Status",
        "references": "References",
    }
    result = []
    for change in changes:
        before, after, current = change["before"], change["incoming"], change["current"]
        concept = after or before
        verb = {"add": "New", "delete": "Removed", "edit": "Updated"}[
            change["operation"]
        ]
        rows = []
        for field, label in labels.items():
            old = field_value(before, field, base)
            new = field_value(after, field, incoming)
            if change["operation"] == "edit" and old == new:
                continue
            if change["operation"] != "edit" and field not in (
                "name",
                "kind",
                "parent",
                "memo",
                "rationale",
                "color",
                "references",
            ):
                continue
            if field in ("memo", "rationale", "references") and old == new == "—":
                continue
            rows.append(
                dict(
                    label=label,
                    kind=field,
                    before=old,
                    after=new,
                    current=field_value(current, field, shared),
                )
            )
        result.append(
            {
                **change,
                "heading": f"{verb} {concept['kind']} · {concept['name']}",
                "rows": rows,
            }
        )
    return result
