"""Administrator-only report generation, saved views and portable exports."""

import csv
import io
import json
import uuid
from pathlib import Path

from django.contrib.admin.views.decorators import staff_member_required
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


def attachment(content, content_type, name):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["Cache-Control"] = "private, no-store"
    return response


def safe_csv(value):
    # Spreadsheet programs interpret formula prefixes even in quoted CSV cells.
    value = str(value) if value is not None else ""
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def report_csv(rows, concepts, summary=False):
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
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows({key: safe_csv(row.get(key)) for key in fields} for row in data)
    return stream.getvalue()


@staff_member_required
@require_http_methods(["GET", "POST"])
@api_errors
def coding_report(request, report_id=None):
    saved = get_object_or_404(CodingReport, pk=report_id) if report_id else None
    if request.method == "POST":
        if saved:
            return HttpResponseBadRequest(
                "Saved reports cannot be changed. Generate a new report from the overview."
            )
        filters = read_filters(request.POST)
        payload = capture_report(filters)
        name = request.POST.get("name", "").strip() or "Coding report"
        if len(name) > 200:
            raise ValueError("Report names must be at most 200 characters.")
        concepts, _ = report_evidence(payload)
        if filters.get("code") and filters["code"] not in {c["key"] for c in concepts}:
            raise ValueError(
                "The selected concept is no longer in this scope. Refresh the report."
            )
        saved = CodingReport.objects.create(
            name=name, created_by=request.user, payload=payload
        )
        return redirect("saved-report", report_id=saved.pk)
    payload = saved.payload if saved else capture_report(read_filters(request.GET))
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
    context = dict(
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
        cases=Case.objects.order_by("name"),
        coders=CodingSession.objects.order_by("coder", "pk"),
        documents=Document.objects.order_by("name"),
        reports=CodingReport.objects.select_related("created_by").order_by("-created")[
            :30
        ],
        download_base=download_base,
        report_base="corpus/base.html",
        code_query=query,
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
            report_csv(rows, summary_concepts, summary=format == "summary-csv"),
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
            report_css=Path(finders.find("corpus/reports.css")).read_text(),
        )
        return attachment(
            render_to_string("corpus/report.html", context, request=request),
            "text/html; charset=utf-8",
            "coding-report.html",
        )
    response = render(request, "corpus/report.html", context)
    response["Cache-Control"] = "private, no-store"
    return response
