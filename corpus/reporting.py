"""Capture reproducible coding evidence and derive research-facing report views."""

from copy import deepcopy

from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from .codebook_review import concept_path, reference_text
from .colors import code_color, highlight_style
from .models import (
    Assignment,
    CodingSession,
    DocumentVersion,
    MergeAudit,
    SharedCodebook,
    Submission,
    Study,
)
from .services import serialize_annotations


@transaction.atomic
def capture_report(study, filters):
    source = filters.get("source", "submitted")
    mode = filters.get("mode", "original")
    if source not in ("submitted", "current") or mode not in ("original", "shared"):
        raise ValueError(
            "Choose submitted or current coding and original or shared concepts."
        )
    assignments = (
        Assignment.objects.filter(session__study=study)
        .select_related("session__base", "session__instructions", "case")
        .order_by("case__name", "session__coder", "pk")
    )
    if filters.get("case"):
        assignments = assignments.filter(case_id=filters["case"])
    if filters.get("coder"):
        assignments = assignments.filter(session_id=filters["coder"])
    assignments = list(assignments)
    if source == "current":
        # Match the session-before-assignment locking order used by coding writes.
        list(
            CodingSession.objects.select_for_update()
            .filter(pk__in=[a.session_id for a in assignments])
            .order_by("pk")
        )
        list(
            Assignment.objects.select_for_update()
            .filter(pk__in=[a.pk for a in assignments])
            .order_by("pk")
        )
        # Refresh personal books and assignment revisions after acquiring locks.
        assignments = list(
            Assignment.objects.select_related("session__base", "case")
            .filter(pk__in=[a.pk for a in assignments])
            .order_by("case__name", "session__coder", "pk")
        )
    study = Study.objects.select_for_update().get(pk=study.pk)
    shared = study.codebook
    latest = Submission.objects.filter(
        assignment_id=OuterRef("assignment_id")
    ).order_by("-pk")
    submissions = Submission.objects.filter(
        assignment_id__in=[a.pk for a in assignments],
        pk=Subquery(latest.values("pk")[:1]),
    ).select_related("withdrawal")
    by_assignment = {s.assignment_id: s for s in submissions}
    audits = {
        a.submission_id: a
        for a in MergeAudit.objects.filter(
            submission_id__in=[s.pk for s in by_assignment.values()]
        )
    }
    records = []
    missing = []
    for assignment in assignments:
        submission = by_assignment.get(assignment.pk)
        if source == "submitted" and (
            submission is None or hasattr(submission, "withdrawal")
        ):
            missing.append(
                dict(coder=assignment.session.coder, case=assignment.case.name)
            )
            continue
        if source == "submitted":
            snapshot = deepcopy(submission.snapshot)
            document_ids = [d["id"] for d in snapshot["documents"]]
            submitted = submission.created.isoformat()
            audit = audits.get(submission.pk)
        else:
            snapshot = dict(
                coder=assignment.session.coder,
                session=str(assignment.session_id),
                case=assignment.case.name,
                case_key=assignment.case.key,
                case_metadata=assignment.case.metadata,
                codebook=deepcopy(assignment.session.codebook),
                base=assignment.session.base_id,
                instructions=dict(
                    revision=assignment.session.instructions_id,
                    content=deepcopy(assignment.session.instructions.content),
                ),
                annotations=serialize_annotations(assignment),
            )
            document_ids = list(assignment.documents.values_list("pk", flat=True))
            submitted = None
            audit = None
        documents = [
            dict(
                id=d.pk,
                key=d.document.key,
                name=d.document.name,
                hash=d.sha256,
                text=d.text,
                metadata=d.metadata,
            )
            for d in DocumentVersion.objects.filter(pk__in=document_ids)
            .select_related("document")
            .order_by("document__name", "pk")
        ]
        if filters.get("document"):
            documents = [d for d in documents if d["key"] == filters["document"]]
            if not documents:
                continue
        allowed = {d["id"] for d in documents}
        annotations = [a for a in snapshot["annotations"] if a["document"] in allowed]
        # Current work is never assigned a mapping from an older submitted book.
        mappings = deepcopy(audit.mappings) if audit else {}
        records.append(
            dict(
                instructions=deepcopy(snapshot["instructions"]),
                key=str(assignment.pk),
                coder=snapshot["coder"],
                session=snapshot["session"],
                case=snapshot["case"],
                case_key=snapshot["case_key"],
                case_metadata=snapshot.get("case_metadata", {}),
                documents=documents,
                annotations=annotations,
                codebook=snapshot["codebook"],
                base_revision=snapshot["base"],
                submission=submission.pk if source == "submitted" else None,
                submitted_at=submitted,
                source=source,
                locked=assignment.locked,
                session_deleted_at=(
                    assignment.session.deleted_at.isoformat()
                    if assignment.session.deleted_at
                    else None
                ),
                assignment_removed_at=(
                    assignment.removed_at.isoformat() if assignment.removed_at else None
                ),
                assignment_revision=assignment.revision,
                codebook_revision=(
                    None if source == "submitted" else assignment.session.revision
                ),
                mappings=mappings,
                mapping_revision=audit.result_id if audit else None,
            )
        )
    return dict(
        schema_version=1,
        study=dict(id=str(study.pk), title=study.title, description=study.description),
        current_instructions=dict(
            revision=study.instructions_id, content=deepcopy(study.instructions.content)
        ),
        generated_at=timezone.now().isoformat(),
        filters={**filters, "source": source, "mode": mode},
        shared_revision=shared.pk if shared else None,
        shared_codebook=(
            deepcopy(shared.content) if shared else dict(version=1, codes=[])
        ),
        records=records,
        missing=missing,
    )


def concept_key(identity, record, payload):
    if payload["filters"]["mode"] == "shared":
        shared = {c["id"] for c in payload["shared_codebook"]["codes"]}
        mapped = record["mappings"].get(identity, identity)
        if mapped in shared:
            return "shared:" + mapped
        return "unmapped:" + identity
    return "original:" + identity


def report_evidence(payload):
    concepts = {}
    shared = payload["shared_codebook"]
    if payload["filters"]["mode"] == "shared":
        for code in shared["codes"]:
            key = "shared:" + code["id"]
            concepts[key] = dict(
                key=key,
                code=code,
                parent="shared:" + code["parent"] if code.get("parent") else None,
                path=concept_path(code["id"], shared),
                definitions=[],
                unmapped=False,
            )
    excerpts = []
    for record in payload["records"]:
        book = record["codebook"]
        lookup = {c["id"]: c for c in book["codes"]}
        for code in book["codes"]:
            key = concept_key(code["id"], record, payload)
            parent = (
                concept_key(code["parent"], record, payload)
                if code.get("parent")
                else None
            )
            if key not in concepts:
                concepts[key] = dict(
                    key=key,
                    code=code,
                    parent=parent,
                    path=concept_path(code["id"], book),
                    definitions=[],
                    unmapped=key.startswith("unmapped:"),
                )
            definition = dict(
                coder=record["coder"],
                session=record["session"],
                name=code["name"],
                path=concept_path(code["id"], book),
                memo=code.get("memo", ""),
                rationale=code.get("rationale", ""),
                references=deepcopy(code.get("references", [])),
                reference_text=reference_text(code.get("references", [])),
            )
            if definition not in concepts[key]["definitions"]:
                concepts[key]["definitions"].append(definition)
        documents = {d["id"]: d for d in record["documents"]}
        for annotation in record["annotations"]:
            document = documents[annotation["document"]]
            direct = sorted(
                {
                    concept_key(identity, record, payload)
                    for identity in annotation["codes"]
                }
            )
            # Original hierarchies may differ between personal codebooks. Follow each
            # occurrence's own hierarchy, never a name-based or first-coder guess.
            rolled = set(direct)
            for identity in annotation["codes"]:
                chain = []
                parent = identity
                seen = set()
                while parent and parent not in seen and parent in lookup:
                    seen.add(parent)
                    key = concept_key(parent, record, payload)
                    chain.append(key)
                    if key.startswith("shared:"):
                        # Once mapped, the accepted shared hierarchy governs ancestry.
                        shared_parent = concepts[key]["parent"]
                        shared_seen = {key}
                        while (
                            shared_parent
                            and shared_parent not in shared_seen
                            and shared_parent in concepts
                        ):
                            shared_seen.add(shared_parent)
                            chain.append(shared_parent)
                            shared_parent = concepts[shared_parent]["parent"]
                        break
                    parent = lookup[parent].get("parent")
                rolled.update(chain)
            excerpts.append(
                dict(
                    key=record["key"] + ":" + annotation["id"],
                    annotation=annotation["id"],
                    record=record["key"],
                    coder=record["coder"],
                    session=record["session"],
                    case=record["case"],
                    case_key=record["case_key"],
                    document=document["name"],
                    document_key=document["key"],
                    document_version=document["id"],
                    document_hash=document["hash"],
                    start=annotation["start"],
                    end=annotation["end"],
                    selected_text=annotation["selected_text"],
                    memo=annotation.get("memo", ""),
                    original_codes=annotation["codes"],
                    original_code_details=[
                        dict(
                            id=identity,
                            key=concept_key(identity, record, payload),
                            name=lookup[identity]["name"],
                            path=concept_path(identity, book),
                            color=code_color(lookup[identity]),
                        )
                        for identity in annotation["codes"]
                    ],
                    direct=direct,
                    rolled=sorted(rolled),
                    submission=record["submission"],
                    submitted_at=record["submitted_at"],
                    source=record["source"],
                    archived=bool(
                        record.get("session_deleted_at")
                        or record.get("assignment_removed_at")
                    ),
                    mappings=record["mappings"],
                    mapping_revision=record["mapping_revision"],
                )
            )
    for concept in concepts.values():
        concept["name"] = concept["code"]["name"]
        concept["kind"] = concept["code"]["kind"]
        concept["color"] = code_color(concept["code"])
        concept["style"] = (
            highlight_style([concept["color"]]) if concept["kind"] == "code" else ""
        )
        concept["memo"] = concept["code"].get("memo", "")
        concept["rationale"] = concept["code"].get("rationale", "")
        concept["references"] = reference_text(concept["code"].get("references", []))
        concept["variants"] = (
            len(
                {
                    (d["path"], d["memo"], d["rationale"], d["reference_text"])
                    for d in concept["definitions"]
                }
            )
            > 1
        )
        direct_rows = [e for e in excerpts if concept["key"] in e["direct"]]
        rows = [e for e in excerpts if concept["key"] in e["rolled"]]
        concept["counts"] = counts(rows)
        concept["direct_count"] = len(direct_rows)
    for excerpt in excerpts:
        excerpt["codes"] = []
        for key in excerpt["direct"]:
            concept = concepts[key]
            original = next(
                c for c in excerpt["original_code_details"] if c["key"] == key
            )
            display = concept if key.startswith("shared:") else original
            excerpt["codes"].append(
                dict(
                    key=key,
                    name=display["name"],
                    path=display["path"],
                    color=display["color"],
                    style=highlight_style([display["color"]]),
                    unmapped=concept["unmapped"],
                )
            )
    return (
        sorted(concepts.values(), key=lambda c: (c["path"].casefold(), c["key"])),
        excerpts,
    )


def counts(excerpts):
    return dict(
        passages=len(excerpts),
        documents=len({e["document_key"] for e in excerpts}),
        document_versions=len(
            {e["document_hash"] + ":" + e["document_key"] for e in excerpts}
        ),
        cases=len({e["case_key"] for e in excerpts}),
        coders=len({e["session"] for e in excerpts}),
    )


def visible_excerpts(excerpts, selected_code):
    return [e for e in excerpts if not selected_code or selected_code in e["rolled"]]


def segments(text, excerpts):
    encoded = text.encode("utf-16-le")
    points = sorted(
        {0, len(encoded) // 2, *(p for e in excerpts for p in (e["start"], e["end"]))}
    )
    result = []
    for start, end in zip(points, points[1:]):
        active = [e for e in excerpts if e["start"] < end and e["end"] > start]
        codes = {c["key"]: c for e in active for c in e["codes"]}
        result.append(
            dict(
                start=start,
                end=end,
                text=encoded[start * 2 : end * 2].decode("utf-16-le"),
                style=highlight_style([c["color"] for c in codes.values()]),
                title=", ".join(c["path"] for c in codes.values()),
                annotations=" ".join(e["key"] for e in active),
            )
        )
    return result


def comparison_view(payload, excerpts, params):
    case_keys = sorted({r["case_key"] for r in payload["records"]})
    case_key = (
        params.get("compare_case")
        if params.get("compare_case") in case_keys
        else next(iter(case_keys), "")
    )
    records = [r for r in payload["records"] if r["case_key"] == case_key]
    keys = {r["key"] for r in records}
    left = (
        params.get("left")
        if params.get("left") in keys
        else (records[0]["key"] if records else "")
    )
    right = (
        params.get("right")
        if params.get("right") in keys and params.get("right") != left
        else next((r["key"] for r in records if r["key"] != left), "")
    )
    chosen = [
        next((r for r in records if r["key"] == key), None) for key in (left, right)
    ]
    documents = {
        d["key"]: d for record in chosen if record for d in record["documents"]
    }
    document_key = (
        params.get("compare_document")
        if params.get("compare_document") in documents
        else next(
            iter(sorted(documents, key=lambda k: documents[k]["name"].casefold())), ""
        )
    )
    panes = []
    for record in chosen:
        if not record:
            panes.append(None)
            continue
        document = next(
            (d for d in record["documents"] if d["key"] == document_key), None
        )
        rows = [
            e
            for e in excerpts
            if e["record"] == record["key"] and e["document_key"] == document_key
        ]
        panes.append(
            dict(
                record=record,
                document=document,
                excerpts=rows,
                segments=segments(document["text"], rows) if document else [],
            )
        )
    same_version = bool(
        all(p and p["document"] for p in panes)
        and panes[0]["document"]["hash"] == panes[1]["document"]["hash"]
    )
    comparisons = []
    if same_version:
        text = panes[0]["document"]["text"].encode("utf-16-le")
        rows = [p["excerpts"] for p in panes]
        points = sorted(
            {p for side in rows for e in side for p in (e["start"], e["end"])}
        )
        for start, end in zip(points, points[1:]):
            active = [
                [e for e in side if e["start"] < end and e["end"] > start]
                for side in rows
            ]
            if not any(active):
                continue
            sets = [{c for e in side for c in e["direct"]} for side in active]
            if not all(active):
                status = "Coded by one coder"
            elif sets[0] == sets[1]:
                status = "Same code identities"
            else:
                status = "Different code identities"
            comparisons.append(
                dict(
                    start=start,
                    end=end,
                    text=text[start * 2 : end * 2].decode("utf-16-le"),
                    sides=active,
                    status=status,
                )
            )
    cases = sorted(
        {(r["case_key"], r["case"]) for r in payload["records"]},
        key=lambda pair: pair[1].casefold(),
    )
    return dict(
        cases=cases,
        case_key=case_key,
        records=records,
        left=left,
        right=right,
        panes=panes,
        documents=sorted(documents.values(), key=lambda d: d["name"].casefold()),
        document_key=document_key,
        same_version=same_version,
        comparisons=comparisons,
    )
