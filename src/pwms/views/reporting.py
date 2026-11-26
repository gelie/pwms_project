import logging
from urllib.parse import urlencode

from django.http import (
    Http404,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import render
from django.views.decorators.http import require_POST

from ..forms import (
    ReportShareForm,
)
from ..reporting import (
    ATTACHMENT_CHOICES,
    DATE_FIELD_CHOICES,
    EXPORT_FORMATS,
    PERIOD_CHOICES,
    REPORT_TYPE_CHOICES,
    SORT_CHOICES,
    ReportFilters,
    absolute_share_url,
    build_report,
    create_share,
    report_for_share,
    resolve_share,
    response_for,
    send_share_email,
)

logger = logging.getLogger(__name__)


#: Values dropped from a report's filter query string.
_EMPTY_FILTER_VALUES = (None, "", False)


def _filter_query(filters):
    """The filter set as a query string (for export and share links)."""
    params = {
        key: value
        for key, value in filters.to_dict().items()
        if value not in _EMPTY_FILTER_VALUES
    }
    return urlencode(params)


def _report_context(request, report, *, share=None, is_shared=False):
    """Everything the reports page, its preview partial and the share modal read."""
    filters = report.filters
    query = _filter_query(filters)
    if share is not None:
        query = f"{query}&share={share.token}" if query else f"share={share.token}"
    return {
        "report": report,
        "filters": filters,
        "report_types": REPORT_TYPE_CHOICES,
        "period_choices": PERIOD_CHOICES,
        "date_field_choices": DATE_FIELD_CHOICES,
        "sort_choices": SORT_CHOICES,
        "export_formats": EXPORT_FORMATS,
        "attachment_choices": ATTACHMENT_CHOICES,
        "filter_params": filters.to_dict(),
        "filter_query": query,
        "share_form": ReportShareForm(initial={"title": report.title}),
        "share": share,
        "is_shared": is_shared,
    }


def reports(request):
    """The report builder: filters, a live preview, exports and sharing."""
    filters = ReportFilters.from_request(request)
    report = build_report(request.user, filters)
    return render(request, "pwms/reports.html", _report_context(request, report))


def reports_preview(request):
    """Just the report preview, so the filter form can swap it over HTMX."""
    filters = ReportFilters.from_request(request)
    report = build_report(request.user, filters)
    return render(
        request, "pwms/partials/report_preview.html", _report_context(request, report)
    )


def reports_export(request):
    """Download the current report as xlsx / pdf / html / csv.

    ``?share=<token>`` exports a shared report as its creator saw it, so a file
    downloaded from a share link matches the page it was downloaded from.
    """
    token = request.GET.get("share", "")
    if token:
        share = resolve_share(token)
        if share is None:
            raise Http404("No such report link.")
        report = report_for_share(share) if share.is_active else None
        if report is None:
            return HttpResponseBadRequest("This report link is no longer active.")
    else:
        report = build_report(request.user, ReportFilters.from_request(request))

    try:
        return response_for(report, request.GET.get("format", "xlsx"))
    except ValueError:
        return HttpResponseBadRequest("Unknown export format.")


@require_POST
def reports_share(request):
    """Mint a share for the current filters, optionally emailing and repeating it."""
    filters = ReportFilters.from_mapping(request.POST)
    report = build_report(request.user, filters)

    form = ReportShareForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"success": False, "errors": form.errors.get_json_data()}, status=400
        )

    data = form.cleaned_data
    scheduled = data["schedule"] not in ("", "none")
    # A repeating share is emailed now and thereafter, so either choice implies
    # the address list — the form rejects one without the other.
    email_to = data["recipients"] if (data["send_email"] or scheduled) else ""
    share = create_share(
        user=request.user,
        filters=filters,
        title=data["title"] or report.title,
        recipients=email_to,
        message=data["message"],
        expires_days=data["expires_days"] or None,
        schedule=data["schedule"] or "none",
        schedule_format=data["attach_format"],
    )
    link = absolute_share_url(share, request)

    sent = 0
    if email_to:
        try:
            sent = send_share_email(
                share,
                request=request,
                report=report,
                attachment_format=data["attach_format"],
            )
        except Exception as error:
            logger.exception("Report share %s could not be emailed", share.pk)
            return JsonResponse(
                {
                    "success": False,
                    "error": f"The link was created, but the email failed: {error}",
                    "link": link,
                    "sent": 0,
                    "schedule": share.schedule,
                },
                status=502,
            )

    return JsonResponse(
        {
            "success": True,
            "link": link,
            "sent": sent,
            "title": share.title,
            "schedule": share.schedule,
            "next_send_at": (
                share.next_send_at.isoformat() if share.next_send_at else None
            ),
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        }
    )


def report_shared(request, token):
    """Open a shared report read-only, exactly as its creator saw it."""
    share = resolve_share(token)
    if share is None:
        raise Http404("No such report link.")
    if not share.is_active:
        return render(
            request,
            "pwms/report-shared.html",
            {"share": share, "inactive": True},
            status=410,
        )

    report = report_for_share(share)
    if report is None:
        return render(
            request,
            "pwms/report-shared.html",
            {"share": share, "unreadable": True},
            status=410,
        )

    share.record_access()
    return render(
        request,
        "pwms/report-shared.html",
        _report_context(request, report, share=share, is_shared=True),
    )
