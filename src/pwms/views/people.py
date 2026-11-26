import logging

from django.db.models import Case, CharField, Q, Value, When
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from ..models import (
    User,
)

logger = logging.getLogger(__name__)

from .common import _directory_page, _text_search


def user_detail(request, pk):
    """Show one person's profile: who they are and the groups they belong to.

    A directory card, not an HR record. The User model also holds identity and
    personnel fields (ID number, date of birth, gender, employee id, supervisor,
    termination date, identity source) that are deliberately left off the page:
    it is readable by any signed-in user — the same audience that may view a
    group's member list — so only the professional details are shown.
    """
    person = get_object_or_404(User.objects.select_related("department"), pk=pk)
    memberships = (
        person.get_groups_with_roles()
        .select_related("group__parent", "role")
        .annotate(
            status=Case(
                When(is_active=False, then=Value("Ended")),
                When(end_date__lt=timezone.localdate(), then=Value("Ended")),
                default=Value("Active"),
                output_field=CharField(),
            )
        )
        .order_by("-is_active", "-start_date", "group__name", "role__name")
    )
    context = {
        "person": person,
        "memberships": memberships,
        # Current groups the person sits in; ended rows still show below, but the
        # headline and the status badges count only the live ones.
        "active_membership_count": sum(1 for m in memberships if m.status == "Active"),
        "current_portfolio": person.current_portfolio,
    }
    return render(request, "pwms/user-detail.html", context)


#: Kind filter for the people directory: value -> a ``Q`` narrowing to that kind.
PEOPLE_KIND_FILTERS = {
    "mp": Q(is_mp=True),
    "staff": Q(is_staff_member=True),
}

#: Status filter: value -> a ``Q`` narrowing to that status.
PEOPLE_STATUS_FILTERS = {
    "active": Q(is_active=True),
    "former": Q(is_active=False),
}


def people(request):
    """Browsable directory of the people in PWMS (sign-in required).

    Mirrors the group list: a free-text filter (name, username, position or
    department) plus kind and status selects, all applied in the database so only
    the requested page's rows are read. Current accounts show by default, so
    former staff do not clutter the browse — they stay reachable through the
    status filter and from any record that names them.
    """
    everyone = User.objects.select_related("department").order_by(
        "last_name", "first_name", "username"
    )

    query = request.GET.get("q", "").strip()
    kind = request.GET.get("kind", "")
    status = request.GET.get("status", "active")

    kind_q = PEOPLE_KIND_FILTERS.get(kind)
    if kind_q is not None:
        everyone = everyone.filter(kind_q)
    status_q = PEOPLE_STATUS_FILTERS.get(status)
    if status_q is not None:
        everyone = everyone.filter(status_q)
    if query:
        everyone = everyone.filter(
            _text_search(
                query,
                "first_name",
                "last_name",
                "username",
                "positiondesc",
                "department__name",
            )
        )

    context = {
        **_directory_page(request, everyone),
        "query": query,
        "kind": kind,
        "status": status,
        "kind_choices": (
            ("", "All people"),
            ("mp", "Members of Parliament"),
            ("staff", "Staff"),
        ),
        "status_choices": (
            ("active", "Current only"),
            ("all", "Current and former"),
            ("former", "Former only"),
        ),
        "has_filters": bool(query or kind or status != "active"),
    }
    return render(request, "pwms/all-people.html", context)
