import calendar
import logging
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import (
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from ..forms import (
    MeetingForm,
    MeetingImportForm,
)
from ..models import (
    Meeting,
)
from ..services.meetings_io import (
    MEETING_COLUMNS,
    ImportFormatError,
    export_response,
    import_meetings,
    template_response,
)
from ..services.permissions import (
    visible_instances,
)

logger = logging.getLogger(__name__)

from .common import _can_create_form_type, _workflow_detail_url

# --- Meetings (the Events workflow) -----------------------------------------
# The main page is a colour-coded calendar with three layouts — month, week and
# day (``?view=``) — over the meetings the signed-in user may view, with HTMX
# navigation, type/mine/text filters, a day panel and a "next up" strip. A JSON
# feed over the same permission-scoped query is exposed for interactive consumers
# (``meeting_calendar_json``).

#: Weekday labels, in Python's weekday order (Monday == 0). The grid's columns
#: are rotated to the configured week start; the same order is what
#: ``calendar.Calendar(firstweekday=…)`` expects.
CALENDAR_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CALENDAR_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

#: Weekday names / abbreviations → index (Monday == 0), for parsing the calendar
#: week-start setting and the ``week_start`` query parameter.
_WEEKDAY_INDEX = {
    name.lower(): index for index, name in enumerate(CALENDAR_WEEKDAY_NAMES)
}
_WEEKDAY_INDEX.update(
    {abbr.lower(): index for index, abbr in enumerate(CALENDAR_WEEKDAYS)}
)

#: How many meetings the "next up" strip on the calendar page shows.
MEETING_AGENDA_ROWS = 8

#: The calendar's three layouts. ``month`` is a dated grid; ``week`` and ``day``
#: lay the meetings on a shared time axis.
CALENDAR_VIEWS = ("month", "week", "day")

#: The time axis of the week/day views: a comfortable working-day window that is
#: widened to fit earlier or later meetings, and the height of one hour on it.
CALENDAR_DEFAULT_START_HOUR = 7
CALENDAR_DEFAULT_END_HOUR = 19
CALENDAR_HOUR_PX = 44


def _meeting_create_url(day=None):
    """The "new meeting" form, optionally prefilled for ``day``."""
    base = reverse("pwms:meeting_create")
    if day is None:
        return base
    return f"{base}?{urlencode({'date': day.isoformat()})}"


def _parse_month(value, today):
    """Parse a ``YYYY-MM`` query parameter into the first of that month."""
    if value:
        parts = value.split("-")
        if len(parts) == 2:
            try:
                return date(int(parts[0]), int(parts[1]), 1)
            except TypeError, ValueError:
                pass
    return date(today.year, today.month, 1)


def _shift_month(first, delta):
    """The first of the month ``delta`` months away from ``first``."""
    index = first.year * 12 + (first.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def _parse_day(value, fallback):
    """Parse a ``YYYY-MM-DD`` query parameter into a :class:`~datetime.date`."""
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return fallback


def _calendar_view(request):
    """The requested calendar layout, defaulting to the month grid."""
    view = request.GET.get("view", "month")
    return view if view in CALENDAR_VIEWS else "month"


def _week_start_index(value):
    """Resolve a week-start day to a weekday index (Monday == 0).

    Accepts a weekday name or abbreviation (``"Sunday"``/``"sun"``) or an int
    ``0``–``6``; anything unrecognised falls back to Monday so a bad setting can
    never break the grid.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 0 <= value <= 6 else 0
    key = str(value or "").strip().lower()
    if key.isdigit():
        index = int(key)
        return index if 0 <= index <= 6 else 0
    return _WEEKDAY_INDEX.get(key, 0)


def _calendar_week_start(request):
    """The day the calendar's weeks begin on.

    A ``week_start`` query parameter (the toolbar control) wins, so a reader can
    flip it for themselves; otherwise the site-wide
    ``settings.MEETING_CALENDAR_WEEK_START`` applies, defaulting to Monday.
    """
    override = request.GET.get("week_start")
    if override:
        return _week_start_index(override)
    return _week_start_index(getattr(settings, "MEETING_CALENDAR_WEEK_START", "monday"))


def _minutes_since_midnight(moment):
    """Minutes past local midnight for a timezone-aware datetime."""
    local = timezone.localtime(moment)
    return local.hour * 60 + local.minute


def _time_window(meetings):
    """The ``(start_hour, end_hour)`` the time axis must show to fit ``meetings``.

    Starts from a comfortable working-day window and grows to cover the earliest
    start and latest end, clamped to the clock. A meeting without an end time is
    still given a sensible slot. Days with no timed meetings keep the default.
    """
    start_hour = CALENDAR_DEFAULT_START_HOUR
    end_hour = CALENDAR_DEFAULT_END_HOUR
    for meeting in meetings:
        if meeting.all_day or meeting.start_datetime is None:
            continue
        start_min = _minutes_since_midnight(meeting.start_datetime)
        end_min = _minutes_since_midnight(meeting.end or meeting.start_datetime)
        if end_min <= start_min:
            end_min = start_min + 60
        start_hour = min(start_hour, start_min // 60)
        end_hour = max(end_hour, -(-end_min // 60))
    start_hour = max(0, min(start_hour, 23))
    end_hour = max(start_hour + 1, min(end_hour, 24))
    return start_hour, end_hour


def _assign_lanes(events):
    """Stack overlapping events into side-by-side lanes so none is hidden.

    ``events`` are dicts carrying ``start_min``/``end_min``; each is given a
    ``lane`` and the run's ``lanes`` count. Non-overlapping events reuse a lane
    and therefore stay full width.
    """
    lane_ends = []
    for event in events:
        for lane, lane_end in enumerate(lane_ends):
            if event["start_min"] >= lane_end:
                event["lane"] = lane
                lane_ends[lane] = event["end_min"]
                break
        else:
            event["lane"] = len(lane_ends)
            lane_ends.append(event["end_min"])
    total = max(1, len(lane_ends))
    for event in events:
        event["lanes"] = total
    return events


def _positioned_events(meetings, start_hour, end_hour):
    """Lay timed meetings on the axis as ``top``/``height`` percentages."""
    total = (end_hour - start_hour) * 60
    events = []
    for meeting in meetings:
        if meeting.all_day or meeting.start_datetime is None:
            continue
        start_min = _minutes_since_midnight(meeting.start_datetime)
        end_min = _minutes_since_midnight(meeting.end or meeting.start_datetime)
        if end_min <= start_min:
            end_min = start_min + 45
        top = max(0, start_min - start_hour * 60)
        height = min(total - top, max(30, end_min - start_min))
        events.append(
            {
                "meeting": meeting,
                "start_min": start_min,
                "end_min": end_min,
                "top": round(top / total * 100, 3),
                "height": round(height / total * 100, 3),
                "lane": 0,
                "lanes": 1,
            }
        )
    return _assign_lanes(events)


def _range_label(start, end):
    """A compact date-range label for the week view, e.g. ``6–12 October 2026``."""
    if start.year != end.year:
        return f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"
    if start.month != end.month:
        return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
    return f"{start.strftime('%d')}–{end.strftime('%d %B %Y')}"


def _week_aligned_range(start, end, week_start):
    """Widen ``[start, end]`` to whole weeks beginning on ``week_start``.

    The month grid draws the weeks that *contain* the month, so the feed mirrors
    that coverage: from the week-start day on or before ``start`` to the day
    before the next week start on or after ``end``.
    """
    first = start - timedelta(days=(start.weekday() - week_start) % 7)
    last = end + timedelta(days=(week_start - 1 - end.weekday()) % 7)
    return first, last


def _calendar_ordered(meetings, week_start):
    """Order meetings the way the calendar reads them.

    Days run from the configured week start (so a week reads Mon→Sun or
    Sun→Sat), and within a day meetings run by time. The week-start component
    makes the ordering explicit in one place, shared by the grid's day cells,
    the day panel and the JSON feed; across a date range it is the natural
    chronological order, just anchored to the week start.
    """

    def sort_key(meeting):
        local = timezone.localtime(meeting.start_datetime)
        week_anchor = local.date() - timedelta(days=(local.weekday() - week_start) % 7)
        return (week_anchor, local, meeting.title)

    return sorted(meetings, key=sort_key)


def _meeting_create_initial(params):
    """Prefill a new meeting's schedule from a calendar ``date``/``time`` link."""
    day = _parse_day(params.get("date"), None)
    if day is None:
        return {}
    start_time = time(9, 0)
    raw_time = params.get("time", "")
    if raw_time:
        try:
            start_time = time.fromisoformat(raw_time)
        except ValueError:
            start_time = time(9, 0)
    start = timezone.make_aware(datetime.combine(day, start_time))
    return {"start_datetime": start, "end_datetime": start + timedelta(hours=2)}


def _meeting_type_choices():
    return Meeting.MEETING_TYPE_CHOICES


def _meeting_query(request, start=None, end=None):
    """The permission-scoped meetings the filters select.

    ``start``/``end`` (inclusive dates) restrict the window when both are given;
    the calendar passes its grid range and the export passes none (the whole set
    the reader may see).
    """
    queryset = Meeting.objects.select_related(
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "host_group",
        "chaired_by",
        "venue",
        "venue__building",
    )
    if start is not None and end is not None:
        queryset = queryset.in_period(start, end)

    meeting_type = request.GET.get("type", "").strip()
    if meeting_type:
        queryset = queryset.filter(meeting_type=meeting_type)
    if request.GET.get("mine") in ("1", "true", "on", "yes"):
        queryset = queryset.for_member(request.user)
    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(reference_number__icontains=query)
            | Q(venue__name__icontains=query)
            | Q(venue__building__name__icontains=query)
            | Q(off_site_venue__icontains=query)
            | Q(off_site_address__icontains=query)
        )
    return visible_instances(request.user, queryset)


def _meeting_export_urls(request):
    """The CSV/xlsx export links, carrying the calendar's current filters."""
    params = {
        key: request.GET.get(key)
        for key in ("type", "mine", "q")
        if request.GET.get(key)
    }
    base = reverse("pwms:meeting_export")
    return {
        export_format: f"{base}?{urlencode({**params, 'format': export_format})}"
        for export_format in ("xlsx", "csv")
    }


def _meeting_calendar_context(request):
    """Everything the calendar page's layouts, filters and day panel need.

    One context serves all three layouts (``?view=month|week|day``): the month
    grid reads ``week_rows``, the week/day layouts read ``days``/``hours``. The
    period shown is anchored on ``day`` (a ``YYYY-MM-DD``) or, in the month view,
    on ``month`` (a ``YYYY-MM``), so every navigation link keeps both the window
    and the reader's filters.
    """
    today = timezone.localdate()
    view = _calendar_view(request)
    week_start = _calendar_week_start(request)

    requested_day = _parse_day(request.GET.get("day"), None)
    if requested_day is not None:
        anchor = requested_day
    else:
        anchor = _parse_month(request.GET.get("month"), today)

    # The filters (type / mine / q) and the week-start preference survive every
    # navigation, so a link only ever changes the view window rather than dropping
    # the reader's selection.
    persisted = {
        key: request.GET.get(key)
        for key in ("type", "mine", "q", "week_start")
        if request.GET.get(key)
    }

    def nav_url(**overrides):
        """A full-page calendar URL carrying the filters plus the given window.

        The page boosts its own links/filters over HTMX, selecting the calendar
        region out of the response, so the same URL is both the ``href`` (the no-
        JavaScript fallback) and the ``hx-get``.
        """
        params = dict(persisted)
        params.update({key: value for key, value in overrides.items() if value})
        return f"{reverse('pwms:meetings')}?{urlencode(params)}"

    # The days the layout shows, and the date window it must fetch. Columns and
    # weeks are rotated to the configured week start.
    weekdays = tuple(
        CALENDAR_WEEKDAYS[(week_start + offset) % 7] for offset in range(7)
    )
    weeks = []
    if view == "month":
        weeks = calendar.Calendar(firstweekday=week_start).monthdatescalendar(
            anchor.year, anchor.month
        )
        days = [day for week in weeks for day in week]
        window_start, window_end = days[0], days[-1]
    elif view == "week":
        window_start = anchor - timedelta(days=(anchor.weekday() - week_start) % 7)
        window_end = window_start + timedelta(days=6)
        days = [window_start + timedelta(days=offset) for offset in range(7)]
    else:
        window_start = window_end = anchor
        days = [anchor]

    meetings = list(_meeting_query(request, window_start, window_end))

    by_day = {}
    for meeting in meetings:
        key = timezone.localtime(meeting.start_datetime).date()
        by_day.setdefault(key, []).append(meeting)

    def day_meetings(day):
        return _calendar_ordered(by_day.get(day, []), week_start)

    def in_period(meeting):
        local = timezone.localtime(meeting.start_datetime)
        if view == "month":
            return (local.year, local.month) == (anchor.year, anchor.month)
        return window_start <= local.date() <= window_end

    period_meetings = [meeting for meeting in meetings if in_period(meeting)]

    # The time axis of the week/day layouts (fitted to the meetings shown).
    start_hour, end_hour = _time_window(meetings)

    # The month day panel: a named day, else the next day this month with a
    # meeting. Only days inside the month are offered.
    selected = None
    if view == "month":
        if requested_day is not None and (
            requested_day.year,
            requested_day.month,
        ) == (anchor.year, anchor.month):
            selected = requested_day
        elif requested_day is None:
            selected = next(
                (
                    timezone.localtime(meeting.start_datetime).date()
                    for meeting in period_meetings
                    if timezone.localtime(meeting.start_datetime).date() >= today
                ),
                timezone.localtime(period_meetings[0].start_datetime).date()
                if period_meetings
                else anchor,
            )

    week_rows = []
    if view == "month":
        for week in weeks:
            week_rows.append(
                [
                    {
                        "date": day,
                        "in_month": (day.year, day.month)
                        == (anchor.year, anchor.month),
                        "is_today": day == today,
                        "is_selected": day == selected,
                        "url": nav_url(
                            view="month",
                            month=anchor.strftime("%Y-%m"),
                            day=day.isoformat(),
                        ),
                        "create_url": _meeting_create_url(day),
                        "meetings": day_meetings(day),
                    }
                    for day in week
                ]
            )

    day_blocks = []
    if view != "month":
        for day in days:
            day_meetings_for_day = day_meetings(day)
            day_blocks.append(
                {
                    "date": day,
                    "is_today": day == today,
                    "is_weekend": day.weekday() >= 5,
                    "is_selected": day == anchor,
                    "day_url": nav_url(view="day", day=day.isoformat()),
                    "create_url": _meeting_create_url(day),
                    "meetings": day_meetings_for_day,
                    "all_day": [m for m in day_meetings_for_day if m.all_day],
                    "events": _positioned_events(
                        [m for m in day_meetings_for_day if not m.all_day],
                        start_hour,
                        end_hour,
                    ),
                }
            )

    # Period navigation and the period label, per layout.
    if view == "month":
        prev_url = nav_url(
            view="month", month=_shift_month(anchor, -1).strftime("%Y-%m")
        )
        next_url = nav_url(
            view="month", month=_shift_month(anchor, 1).strftime("%Y-%m")
        )
        today_url = nav_url(view="month", month=today.strftime("%Y-%m"))
        period_label = anchor.strftime("%B %Y")
        switch_day = selected or anchor
    elif view == "week":
        prev_url = nav_url(view="week", day=(anchor - timedelta(days=7)).isoformat())
        next_url = nav_url(view="week", day=(anchor + timedelta(days=7)).isoformat())
        today_url = nav_url(view="week", day=today.isoformat())
        period_label = _range_label(window_start, window_end)
        switch_day = anchor
    else:
        prev_url = nav_url(view="day", day=(anchor - timedelta(days=1)).isoformat())
        next_url = nav_url(view="day", day=(anchor + timedelta(days=1)).isoformat())
        today_url = nav_url(view="day", day=today.isoformat())
        period_label = anchor.strftime("%A, %d %B %Y")
        switch_day = anchor

    view_urls = {
        name: nav_url(view=name, day=switch_day.isoformat()) for name in CALENDAR_VIEWS
    }

    # A legend of the meeting types actually on show, in a stable colour per type.
    legend = []
    seen_types = set()
    for meeting in sorted(period_meetings, key=lambda item: item.start_datetime):
        if meeting.meeting_type in seen_types:
            continue
        seen_types.add(meeting.meeting_type)
        legend.append(
            {
                "label": meeting.get_meeting_type_display(),
                "color": meeting.color,
            }
        )

    return {
        "view": view,
        "anchor": anchor,
        "period_label": period_label,
        "prev_url": prev_url,
        "next_url": next_url,
        "today_url": today_url,
        "view_urls": view_urls,
        # Month layout.
        "week_rows": week_rows,
        "weekdays": weekdays,
        "selected_day": selected,
        "selected_meetings": day_meetings(selected) if selected else [],
        # Week/day layout.
        "days": day_blocks,
        "hours": list(range(start_hour, end_hour)),
        "hour_px": CALENDAR_HOUR_PX,
        "has_all_day": any(block["all_day"] for block in day_blocks),
        # The day the week starts on, and the choices for the toolbar control.
        "week_start": week_start,
        "week_start_options": [
            {"value": index, "label": name}
            for index, name in enumerate(CALENDAR_WEEKDAY_NAMES)
        ],
        # Shared.
        "legend": legend,
        "meeting_count": len(period_meetings),
        "meeting_type_choices": _meeting_type_choices(),
        "selected_type": request.GET.get("type", ""),
        "mine_only": request.GET.get("mine") in ("1", "true", "on", "yes"),
        "query": request.GET.get("q", "").strip(),
        "has_filters": bool(
            request.GET.get("type") or request.GET.get("mine") or request.GET.get("q")
        ),
        "new_meeting_url": _meeting_create_url(anchor if view != "month" else None),
        "calendar_json_url": reverse("pwms:meeting_calendar_json"),
    }


def meetings(request):
    """Calendar dashboard: the Events workflow's main page.

    A colour-coded calendar of every meeting the reader may view, in a month,
    week or day layout (``?view=``), with the filters (meeting type, "my
    meetings", free text) applied, a legend, a "next up" strip and — in the
    month view — a panel for the selected day. The calendar swaps in place over
    HTMX (``pwms:meeting_calendar``); the whole page works without JavaScript
    too, because navigation is ordinary links and every layout is server-rendered.
    """
    context = _meeting_calendar_context(request)
    upcoming = list(
        visible_instances(
            request.user,
            Meeting.objects.select_related(
                "current_state", "host_group", "venue", "venue__building"
            )
            .filter(start_datetime__gte=timezone.now())
            .order_by("start_datetime"),
        )
    )[:MEETING_AGENDA_ROWS]
    context.update(
        {
            "upcoming_meetings": upcoming,
            "can_create_meeting": _can_create_form_type(request.user, MeetingForm),
            "export_urls": _meeting_export_urls(request),
        }
    )
    return render(request, "pwms/meetings.html", context)


def meeting_calendar(request):
    """The calendar body as a standalone fragment (reusable outside the page)."""
    return render(
        request,
        "pwms/partials/meetings/calendar.html",
        _meeting_calendar_context(request),
    )


def meeting_export(request):
    """Download the reader's meetings as CSV or Excel (xlsx).

    The calendar's filters (type / mine / q) and an optional ``start``/``end``
    date window select the rows; ``?format=csv|xlsx`` (default xlsx) sets the file
    type. The download is written in the same columns the import reads, so a file
    exported here can be edited and imported back.
    """
    start = _parse_day(request.GET.get("start"), None)
    end = _parse_day(request.GET.get("end"), None)
    meetings = _calendar_ordered(
        _meeting_query(request, start, end), _calendar_week_start(request)
    )
    try:
        return export_response(meetings, request.GET.get("format", "xlsx"))
    except ValueError:
        return HttpResponseBadRequest("Unknown export format.")


def meeting_import_template(request):
    """Download a blank import template (the header row, plus a xlsx Read me)."""
    try:
        return template_response(request.GET.get("format", "csv"))
    except ValueError:
        return HttpResponseBadRequest("Unknown template format.")


def meeting_import(request):
    """Bulk import meetings from a CSV/XLSX file (a create/update upsert).

    Gated on the create capability: importing makes new meetings. Each row is
    validated on its own, so a bad row is reported and skipped rather than
    stopping the file, and the outcome (created / updated / skipped / errors) is
    shown back on the page. Nothing is emailed and nothing is deleted.
    """
    if not _can_create_form_type(request.user, MeetingForm):
        messages.error(request, "You do not have a role that may import meetings.")
        return redirect("pwms:meetings")

    form = MeetingImportForm()
    report = None
    if request.method == "POST":
        form = MeetingImportForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.cleaned_data["file"]
            try:
                report = import_meetings(
                    upload,
                    actor=request.user,
                    filename=upload.name,
                    dry_run=form.cleaned_data["dry_run"],
                )
            except ImportFormatError as error:
                form.add_error("file", str(error))
    return render(
        request,
        "pwms/meeting-import.html",
        {"form": form, "report": report, "columns": MEETING_COLUMNS},
    )


def _meeting_time_label(meeting):
    """A short time label for a meeting chip ("All day" or ``09:30–11:30``)."""
    if meeting.all_day:
        return "All day"
    if meeting.start_datetime is None:
        return ""
    start = timezone.localtime(meeting.start_datetime)
    if meeting.end_datetime:
        end = timezone.localtime(meeting.end_datetime)
        return f"{start:%H:%M}\u2013{end:%H:%M}"
    return f"{start:%H:%M}"


def _meeting_subtitle(meeting):
    """A one-line summary: meeting type, venue and hosting group, joined."""
    parts = [meeting.get_meeting_type_display()]
    venue = meeting.venue_display
    if venue:
        parts.append(venue)
    if meeting.host_group_id:
        parts.append(meeting.host_group.name)
    return " \u00b7 ".join(parts)


def meeting_calendar_json(request):
    """Meetings in a date window as JSON, scoped to what the caller may view.

    ``?start=YYYY-MM-DD&end=YYYY-MM-DD`` (both inclusive), or ``?month=YYYY-MM``.
    Each entry carries the fields an interactive calendar needs; the same
    permission chain as the grid is applied, so the feed can never disclose a
    meeting the caller could not open.

    The feed follows the calendar's week start (``?week_start=`` or
    ``settings.MEETING_CALENDAR_WEEK_START``): a bare ``?month=`` window is
    widened to the whole weeks that contain the month, exactly as the on-screen
    grid draws them, and the entries are ordered day-first from that week start;
    the response reports the ``week_start`` it used so a consumer can lay out its
    own weeks to match.
    """
    today = timezone.localdate()
    week_start = _calendar_week_start(request)
    start_param = request.GET.get("start")
    end_param = request.GET.get("end")
    if start_param:
        try:
            start = date.fromisoformat(start_param)
        except ValueError:
            return JsonResponse({"detail": "Invalid start date."}, status=400)
    else:
        first = _parse_month(request.GET.get("month"), today)
        start = first
    if end_param:
        try:
            end = date.fromisoformat(end_param)
        except ValueError:
            return JsonResponse({"detail": "Invalid end date."}, status=400)
    else:
        end = _shift_month(start.replace(day=1), 1) - timedelta(days=1)
    if not start_param and not end_param:
        # A month (or default) feed is week-aligned to match the grid's coverage.
        start, end = _week_aligned_range(start, end, week_start)

    meetings = _calendar_ordered(_meeting_query(request, start, end), week_start)
    payload = [
        {
            "public_id": str(meeting.public_id),
            "title": meeting.title,
            "reference_number": meeting.reference_number,
            "meeting_type": meeting.meeting_type,
            "meeting_type_label": meeting.get_meeting_type_display(),
            "color": meeting.color,
            "subtitle": _meeting_subtitle(meeting),
            "time_label": _meeting_time_label(meeting),
            "start_datetime": meeting.start_datetime.isoformat()
            if meeting.start_datetime
            else None,
            "end_datetime": meeting.end_datetime.isoformat()
            if meeting.end_datetime
            else None,
            "all_day": meeting.all_day,
            "attendance_mode": meeting.attendance_mode,
            "attendance_mode_label": meeting.get_attendance_mode_display(),
            "venue": meeting.venue_display,
            "off_site_venue": meeting.off_site_venue,
            "off_site_address": meeting.off_site_address,
            "state": meeting.current_state.name if meeting.current_state_id else "",
            "calendar_state": meeting.calendar_state,
            "host_group": meeting.host_group.name if meeting.host_group_id else "",
            "url": _workflow_detail_url(meeting),
        }
        for meeting in meetings
    ]
    return JsonResponse(
        {
            "count": len(payload),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "week_start": week_start,
            "week_start_label": CALENDAR_WEEKDAY_NAMES[week_start],
            "results": payload,
        }
    )
