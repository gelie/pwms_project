import logging

from django.db.models import Case, IntegerField, Q, Value, When
from django.shortcuts import render

from ..models import (
    City,
    Country,
    DelegationReport,
    Group,
    Role,
    User,
)
from ..services.bill_committees import bill_committee_choices
from ..services.permissions import (
    EDIT,
    resolve,
)
from ..services.search import count_hits, search_site

logger = logging.getLogger(__name__)


def role_search(request):
    """Search roles for the membership form's type-ahead.

    Only roles the form may assign are offered: a committee secretary cannot
    mint a global manager by picking a role that carries
    ``can_manage_permissions``.
    """
    search_query = request.GET.get("search", "").strip()
    roles = Role.objects.filter(can_manage_permissions=False)
    if search_query:
        roles = roles.filter(
            Q(name__icontains=search_query) | Q(description__icontains=search_query)
        )
    return render(
        request=request,
        template_name="pwms/partials/role_search_results.html",
        context={"roles": roles.order_by("name")[:20]},
    )


def user_search(request):
    """Search active users and return HTML results for HTMX."""
    search_query = request.GET.get("search", "")

    users = User.objects.filter(is_active=True)

    if search_query:
        users = users.filter(
            Q(username__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
        )

    users = users.order_by("first_name", "last_name", "username")[:20]

    return render(
        request=request,
        template_name="pwms/partials/user_search_results.html",
        context={"users": users},
    )


def _group_search_response(request, groups):
    """Render the shared group-results fragment for ``groups``."""
    search_query = request.GET.get("search", "")

    if search_query:
        groups = groups.filter(Q(name__icontains=search_query))

    return render(
        request=request,
        template_name="pwms/partials/group_search_results.html",
        context={"groups": groups.order_by("name")[:20]},
    )


def group_search(request):
    """Search groups and return HTML results for HTMX."""
    return _group_search_response(request, Group.objects.all())


def bill_committee_search(request):
    """Search the committees a bill may be committed to, for HTMX."""
    return _group_search_response(request, bill_committee_choices())


def ministry_search(request):
    """Search ministries for the bill sponsor-name lookup, for HTMX."""
    search_query = request.GET.get("search", "").strip()

    ministries = Group.objects.filter(group_type=Group.Type.MINISTRY, is_active=True)
    if search_query:
        ministries = ministries.filter(
            Q(name__icontains=search_query) | Q(short_name__icontains=search_query)
        )

    return render(
        request=request,
        template_name="pwms/partials/ministry_search_results.html",
        context={"ministries": ministries.order_by("name")[:20]},
    )


def report_search(request):
    """
    Search the delegation reports the user may edit, for HTMX.

    Feeds the resolution form's parent picker: linking a resolution under a report
    edits that report's hierarchy too, so only reports the user may edit are
    offered.
    """
    search_query = request.GET.get("search", "").strip()

    reports = [
        report
        for report in DelegationReport.objects.order_by("-created_at")
        if resolve(request.user, report, EDIT)
    ]
    if search_query:
        needle = search_query.lower()
        reports = [
            report
            for report in reports
            if needle in f"{report.reference_number or ''} {report.title}".lower()
        ]

    return render(
        request=request,
        template_name="pwms/partials/report_search_results.html",
        context={"reports": reports[:20]},
    )


def country_search(request):
    """Search countries and return HTML results for HTMX."""
    search_query = request.GET.get("search", "").strip()

    countries = Country.objects.all()
    order = ["name"]
    if search_query:
        countries = countries.filter(
            Q(name__icontains=search_query) | Q(code__iexact=search_query)
        ).annotate(
            # A name that starts with the query is the likelier match: "south"
            # should offer South Africa before French Southern Territories.
            rank=Case(
                When(name__istartswith=search_query, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        order = ["rank", "name"]

    return render(
        request=request,
        template_name="pwms/partials/country_search_results.html",
        context={"countries": countries.order_by(*order)[:20]},
    )


def city_search(request):
    """
    Search cities and return HTML results for HTMX.

    The picker sends the country it is paired with as ``country``, so a city can
    only be chosen for the country that is already selected. An empty search
    lists that country's largest cities, so the box doubles as a browse control.
    """
    search_query = request.GET.get("search", "").strip()
    country_id = request.GET.get("country", "").strip()

    cities = City.objects.select_related("country")
    if country_id.isdigit():
        cities = cities.filter(country_id=int(country_id))
    if search_query:
        cities = cities.filter(
            Q(name__istartswith=search_query) | Q(ascii_name__istartswith=search_query)
        )

    # Largest places first, which also keeps village namesakes out of the way.
    return render(
        request=request,
        template_name="pwms/partials/city_search_results.html",
        context={"cities": cities.order_by("-population", "name")[:20]},
    )


def global_search(request):
    """Site-wide search across every register the reader may see.

    Returns the categorized results fragment for an HTMX request, which the
    header swaps into its search panel, and a full results page otherwise — so
    the box still works, and its results are still linkable, without JavaScript.
    """
    query = request.GET.get("q", "").strip()
    categories = search_site(request.user, query)
    context = {
        "query": query,
        "categories": categories,
        "total": count_hits(categories),
    }
    template_name = (
        "pwms/partials/global_search_results.html"
        if request.headers.get("HX-Request") == "true"
        else "pwms/search-results.html"
    )
    return render(request, template_name, context)
