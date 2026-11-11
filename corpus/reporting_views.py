"""Administrator-only report generation, saved views and portable exports."""

import csv
import io
import json
import uuid
from pathlib import Path

from .permissions import admin_required
from django.contrib.staticfiles import finders
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_http_methods

from .models import Case, CodingReport, CodingSession, Document
from .reporting import (
    capture_report,
    comparison_view,
    counts,
    report_evidence,
    visible_excerpts,
)
from .views import api_errors
from .exports import csv_cell
from .studies import get_study
from django.urls import reverse


def read_filters(data):
    result = dict(
        source=data.get("source", "submitted"), mode=data.get("mode", "original")
    )
    if data.get("case"):
        result["case"] = int(data["case"])
    if data.get("coder"):
        result["coder"] = str(uuid.UUID(data["coder"]))
    for key in ("document", "code"):
        if data.get(key):
            result[key] = data[key]
    return result


def study_filters(study, data):
    result = read_filters(data)
    if result.get("case"):
        get_object_or_404(Case, study=study, pk=result["case"])
    if result.get("coder"):
        get_object_or_404(CodingSession, study=study, pk=result["coder"])
    if result.get("document"):
        get_object_or_404(Document, study=study, key=result["document"])
    return result


def attachment(content, content_type, name):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["Cache-Control"] = "private, no-store"
    return response


def report_csv(rows, concepts, payload, summary=False):
    stream = io.StringIO(newline="")
    if summary:
        fields = [
            "code",
            "hierarchy",
            "kind",
            "unmapped",
            "direct_passages",
            "passages_including_descendants",
            "documents",
            "document_versions",
            "cases",
            "coders",
        ]
        data = [
            dict(
                code=c["name"],
                hierarchy=c["path"],
                kind=c["kind"],
                unmapped=c["unmapped"],
                direct_passages=c["direct_count"],
                passages_including_descendants=c["counts"]["passages"],
                **{k: v for k, v in c["counts"].items() if k != "passages"},
            )
            for c in concepts
        ]
    else:
        fields = [
            "case",
            "case_key",
            "document",
            "document_key",
            "document_version",
            "document_hash",
            "coder",
            "session",
            "annotation",
            "start_utf16",
            "end_utf16",
            "selected_text",
            "codes",
            "original_code_names",
            "original_code_ids",
            "accepted_shared_mappings",
            "mapping_revision",
            "memo",
            "submission",
            "submitted_at",
            "source",
        ]
        data = [
            {
                **{k: e.get(k) for k in fields},
                "start_utf16": e["start"],
                "end_utf16": e["end"],
                "codes": "; ".join(c["path"] for c in e["codes"]),
                "original_code_ids": json.dumps(e["original_codes"]),
                "original_code_names": "; ".join(
                    c["path"] for c in e["original_code_details"]
                ),
                "accepted_shared_mappings": json.dumps(
                    e["mappings"], ensure_ascii=False
                ),
            }
            for e in rows
        ]
    fields = ["study_id", "study", *fields]
    if summary:
        fields += [
            "current_instructions_revision",
            "current_research_questions",
            "current_coding_guidelines",
        ]
        for row in data:
            row.update(
                current_instructions_revision=payload["current_instructions"][
                    "revision"
                ],
                current_research_questions=payload["current_instructions"]["content"][
                    "research_questions"
                ],
                current_coding_guidelines=payload["current_instructions"]["content"][
                    "guidelines"
                ],
            )
    else:
        fields += ["instructions_revision", "research_questions", "coding_guidelines"]
        for row, excerpt in zip(data, rows):
            row.update(
                instructions_revision=excerpt["instructions"]["revision"],
                research_questions=excerpt["instructions"]["content"][
                    "research_questions"
                ],
                coding_guidelines=excerpt["instructions"]["content"]["guidelines"],
            )
    for row in data:
        row.update(study_id=payload["study"]["id"], study=payload["study"]["title"])
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows({key: csv_cell(row.get(key)) for key in fields} for row in data)
    return stream.getvalue()


@admin_required
@require_http_methods(["GET", "POST"])
@api_errors
def coding_report(request, study_id, report_id=None):
    study = get_study(request.user, study_id)
    saved = (
        get_object_or_404(CodingReport, study=study, pk=report_id)
        if report_id
        else None
    )
    if request.method == "POST":
        if saved:
            return HttpResponseBadRequest(
                "Saved reports cannot be changed. Generate a new report from the overview."
            )
        filters = study_filters(study, request.POST)
        payload = capture_report(study, filters)
        name = request.POST.get("name", "").strip() or "Coding report"
        if len(name) > 200:
            raise ValueError("Report names must be at most 200 characters.")
        concepts, _ = report_evidence(payload)
        if filters.get("code") and filters["code"] not in {c["key"] for c in concepts}:
            raise ValueError(
                "The selected concept is no longer in this scope. Refresh the report."
            )
        saved = CodingReport.objects.create(
            study=study, name=name, created_by=request.user, payload=payload
        )
        return redirect("saved-report", study_id=study.pk, report_id=saved.pk)
    payload = (
        saved.payload
        if saved
        else capture_report(study, study_filters(study, request.GET))
    )
    concepts, evidence = report_evidence(payload)
    selected_code = request.GET.get("code", payload["filters"].get("code", ""))
    # A changed corpus/coder/mode selection can legitimately remove a previously
    # selected concept; show the whole scope in that situation.
    if selected_code not in {c["key"] for c in concepts}:
        selected_code = ""
    rows = visible_excerpts(evidence, selected_code)
    selected = next((c for c in concepts if c["key"] == selected_code), None)
    view = "compare" if request.GET.get("view") == "compare" else "codes"
    comparison = comparison_view(payload, rows, request.GET)
    params = request.GET.copy()
    params.pop("format", None)
    for concept in concepts:
        link_params = params.copy()
        link_params["code"] = concept["key"]
        link_params["view"] = "codes"
        concept["href"] = request.path + "?" + link_params.urlencode()
    for excerpt in rows:
        link_params = params.copy()
        link_params.update(
            {
                "view": "compare",
                "compare_case": excerpt["case_key"],
                "compare_document": excerpt["document_key"],
                "left": excerpt["record"],
            }
        )
        link_params.pop("right", None)
        excerpt["compare_href"] = request.path + "?" + link_params.urlencode()
    codes_params = params.copy()
    codes_params["view"] = "codes"
    compare_params = params.copy()
    compare_params["view"] = "compare"
    query = params.urlencode()
    download_base = request.path + ("?" + query + "&" if query else "?")
    needed = {(e["record"], e["document_version"]) for e in rows}
    needed.update(
        (pane["record"]["key"], pane["document"]["id"])
        for pane in comparison["panes"]
        if pane and pane["document"]
    )
    reading_documents = []
    for record in payload["records"]:
        for document in record["documents"]:
            if (record["key"], document["id"]) not in needed:
                continue
            annotations = [
                e
                for e in rows
                if e["record"] == record["key"]
                and e["document_version"] == document["id"]
            ]
            reading_documents.append(
                dict(
                    key=record["key"] + ":" + str(document["id"]),
                    name=document["name"],
                    text=document["text"],
                    metadata=document["metadata"],
                    annotations=[
                        dict(
                            id=e["key"],
                            start=e["start"],
                            end=e["end"],
                            codes=e["direct"],
                        )
                        for e in annotations
                    ],
                    colors={
                        code["key"]: code["color"]
                        for e in annotations
                        for code in e["codes"]
                    },
                    labels={
                        code["key"]: code["path"]
                        for e in annotations
                        for code in e["codes"]
                    },
                )
            )
    context = dict(
        study=study,
        saved=saved,
        payload=payload,
        filters=payload["filters"],
        concepts=concepts,
        excerpts=rows,
        selected=selected,
        selected_code=selected_code,
        totals=counts(rows),
        comparison=comparison,
        view=view,
        cases=Case.objects.filter(study=study).order_by("name"),
        coders=CodingSession.objects.filter(study=study).order_by("coder", "pk"),
        documents=Document.objects.filter(study=study).order_by("name"),
        reports=CodingReport.objects.filter(study=study)
        .select_related("created_by")
        .order_by("-created")[:30],
        download_base=download_base,
        report_base="corpus/base.html",
        code_query=query,
        reading_documents=reading_documents,
        source_records=len(payload["records"]),
        codes_url=request.path + "?" + codes_params.urlencode(),
        compare_url=request.path + "?" + compare_params.urlencode(),
    )
    format = request.GET.get("format")
    if format == "json":
        content = dict(
            report_name=saved.name if saved else "Coding report",
            saved_report=str(saved.pk) if saved else None,
            view_filters=dict(code=selected_code, view=view),
            payload=payload,
            concepts=concepts,
            excerpts=rows,
            counts=counts(rows),
        )
        return attachment(
            json.dumps(content, ensure_ascii=False, indent=2),
            "application/json; charset=utf-8",
            "coding-report.json",
        )
    if format in ("csv", "summary-csv"):
        summary_concepts = [selected] if selected else concepts
        return attachment(
            report_csv(
                rows, summary_concepts, payload, summary=format == "summary-csv"
            ),
            "text/csv; charset=utf-8",
            (
                "coding-report-summary.csv"
                if format == "summary-csv"
                else "coding-report-excerpts.csv"
            ),
        )
    if format == "html":
        context.update(
            report_base="corpus/report_export_base.html",
            portable=True,
            report_css=Path(finders.find("corpus/reports.css")).read_text()
            + Path(finders.find("corpus/markdown.css")).read_text(),
            markdown_script=Path(finders.find("corpus/markdown.js"))
            .read_text()
            .replace("</script", "<\\/script"),
            reading_script=Path(finders.find("corpus/reading.js"))
            .read_text()
            .replace("</script", "<\\/script"),
        )
        return attachment(
            render_to_string("corpus/report.html", context, request=request),
            "text/html; charset=utf-8",
            "coding-report.html",
        )
    response = render(request, "corpus/report.html", context)
    response["Cache-Control"] = "private, no-store"
    return response
