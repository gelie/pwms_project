import logging

from django.contrib import messages
from django.db.models import Case, CharField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..forms import (
    DutyDelegationForm,
    GroupForm,
    GroupMembershipForm,
)
from ..models import (
    DutyDelegation,
    Group,
    GroupMembership,
    User,
)
from ..services.permissions import (
    EDIT,
    MANAGE,
    require,
    resolve,
)

logger = logging.getLogger(__name__)

from .common import _directory_page, _text_search


def all_groups(request):
    """List every group in the organisational hierarchy (sign-in required).

    The list is narrowed by a free-text name filter (matched against the name and
    short name, as in the admin) and a group-type filter, both applied in the
    database so only the requested page's rows are read. The type options are
    derived from the types in use rather than the full choice list, so the filter
    cannot advertise a type no group uses.
    """
    groups = Group.objects.select_related("parent").order_by("name")

    query = request.GET.get("q", "").strip()
    type_filter = request.GET.get("type", "")

    if query:
        groups = groups.filter(_text_search(query, "name", "short_name"))
    if type_filter:
        groups = groups.filter(group_type=type_filter)

    in_use = set(Group.objects.values_list("group_type", flat=True))
    context = {
        **_directory_page(request, groups),
        "query": query,
        "type_choices": [
            (value, label) for value, label in Group.Type.choices if value in in_use
        ],
        "has_filters": bool(query or type_filter),
    }
    return render(request, "pwms/all-groups.html", context)


def my_groups(request):
    """List the groups the signed-in user is a member of (sign-in required)."""
    today = timezone.localdate()
    memberships = (
        request.user.get_groups_with_roles()
        .select_related("group__parent")
        .annotate(
            status=Case(
                When(is_active=False, then=Value("Inactive")),
                When(end_date__lt=today, then=Value("Expired")),
                default=Value("Active"),
                output_field=CharField(),
            )
        )
        .order_by("group__name", "role__name")
    )
    return render(request, "pwms/my-groups.html", {"memberships": memberships})


def group_detail(request, pk):
    """Show one group: hierarchy context, details and its member count.

    A committee secretary sees the edit and membership controls for their own
    committee; everyone else still sees the same read-only page. The
    ``can_*`` flags come from the shared resolver, so the controls and the
    views they post to agree on who may act.
    """
    group = get_object_or_404(Group.objects.select_related("parent"), pk=pk)
    memberships = list(
        group.members.filter(is_active=True)
        .select_related("user", "role")
        .order_by("user__last_name", "user__first_name", "role__name")
    )
    delegations = list(
        group.duty_delegations.current().select_related("delegator", "delegate")
    )
    # Reflect a current delegation on the member's row without changing the
    # membership's own state: being delegated is not the same as having left.
    delegation_by_member = {item.delegator_id: item for item in delegations}
    for membership in memberships:
        membership.active_delegation = delegation_by_member.get(membership.user_id)
    context = {
        "group": group,
        "memberships": memberships,
        # Distinct users with an active membership (the model's own helper).
        "member_count": group.get_active_members().count(),
        "child_groups": group.children.order_by("name"),
        "can_edit_group": resolve(request.user, group, EDIT),
        "can_manage_group": resolve(request.user, group, MANAGE),
        # Current delegations only: an ended one is history, not authority.
        "delegations": delegations,
    }
    return render(request, "pwms/group-detail.html", context)


def group_edit(request, pk):
    """Edit a group's own details.

    Allowed for a committee's secretary (or an administrator) through the shared
    resolver; structure fields such as the parent and group type are not part of
    the form, so administering a committee cannot reshape the tree.
    """
    group = get_object_or_404(Group.objects.select_related("parent"), pk=pk)
    require(request.user, group, EDIT)
    form = GroupForm(request.POST or None, instance=group)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f'Group "{group.name}" updated.')
        return redirect("pwms:group_detail", pk=group.pk)
    return render(request, "pwms/group-form.html", {"group": group, "form": form})


def _end_membership_delegations(group, member):
    """Stop delegations that depend on ``member`` still belonging to ``group``.

    A delegation derives its authority from the *delegator's* membership, so
    ending that membership must end it. A delegate may sit outside the group, so
    ending a membership they happen to hold there changes nothing. Saved row by
    row (rather than one bulk ``update``) so the change reaches auditlog like any
    other delegation edit.
    """
    delegations = DutyDelegation.objects.filter(
        group=group, delegator=member, is_active=True
    )
    for delegation in delegations:
        delegation.is_active = False
        delegation.save(update_fields=["is_active", "updated_at"])


def group_membership_create(request, pk):
    """Add a membership to a group the user may administer."""
    group = get_object_or_404(Group.objects.select_related("parent"), pk=pk)
    require(request.user, group, MANAGE)
    form = GroupMembershipForm(request.POST or None, group=group)
    if request.method == "POST" and form.is_valid():
        membership = form.save(commit=False)
        membership.group = group
        membership.updated_by = request.user
        membership.save()
        messages.success(
            request,
            f'{membership.user.display_name} added to "{group.name}" as '
            f"{membership.role.name}.",
        )
        return redirect("pwms:group_detail", pk=group.pk)
    return render(
        request,
        "pwms/group-membership-form.html",
        {"group": group, "form": form, "membership": None},
    )


def group_membership_edit(request, pk, membership_pk):
    """Change one membership (role, dates, active state or notes)."""
    group = get_object_or_404(Group.objects.select_related("parent"), pk=pk)
    membership = get_object_or_404(GroupMembership, pk=membership_pk, group=group)
    require(request.user, group, MANAGE)
    form = GroupMembershipForm(request.POST or None, instance=membership, group=group)
    if request.method == "POST" and form.is_valid():
        membership = form.save(commit=False)
        membership.updated_by = request.user
        membership.save()
        if not membership.is_active:
            _end_membership_delegations(group, membership.user)
        messages.success(request, "Membership updated.")
        return redirect("pwms:group_detail", pk=group.pk)
    return render(
        request,
        "pwms/group-membership-form.html",
        {"group": group, "form": form, "membership": membership},
    )


def group_membership_end(request, pk, membership_pk):
    """Confirm (GET) then end (POST) an active membership.

    The row is kept as history. A Committee Secretary membership cannot be
    ended: the role administers the committee, so it must be changed first if
    the person should stop administering it.
    """
    group = get_object_or_404(Group.objects.select_related("parent"), pk=pk)
    membership = get_object_or_404(GroupMembership, pk=membership_pk, group=group)
    require(request.user, group, MANAGE)

    if membership.is_active and membership.is_committee_secretary:
        messages.error(request, Group.COMMITTEE_SECRETARY_PROTECTED_MESSAGE)
        return redirect("pwms:group_detail", pk=group.pk)

    if not membership.is_active:
        messages.info(request, "That membership has already ended.")
        return redirect("pwms:group_detail", pk=group.pk)

    if request.method == "POST":
        membership.is_active = False
        membership.updated_by = request.user
        membership.save(update_fields=["is_active", "updated_by", "updated_at"])
        _end_membership_delegations(group, membership.user)
        messages.success(
            request, f'{membership.user.display_name} removed from "{group.name}".'
        )
        return redirect("pwms:group_detail", pk=group.pk)

    return render(
        request,
        "pwms/group-membership-confirm-end.html",
        {"group": group, "membership": membership},
    )


def duty_delegation_create(request, pk):
    """Record a member's delegation of their duties in this group.

    Members do not necessarily sign in, so the committee secretary records the
    delegation on their behalf: they choose the group member whose duties are
    being lent, the stand-in (who may belong to another group), and the day the
    delegation lapses.
    """
    group = get_object_or_404(Group.objects.select_related("parent"), pk=pk)
    require(request.user, group, MANAGE)
    form = DutyDelegationForm(request.POST or None, group=group)
    if request.method == "POST" and form.is_valid():
        delegation = form.save(commit=False)
        delegation.created_by = request.user
        delegation.save()
        messages.success(
            request,
            f"Duties of {delegation.delegator.display_name} are delegated to "
            f"{delegation.delegate.display_name} until "
            f"{delegation.end_date:%Y-%m-%d}.",
        )
        return redirect("pwms:group_detail", pk=group.pk)
    return render(
        request,
        "pwms/duty-delegation-form.html",
        {"group": group, "form": form},
    )


@require_POST
def duty_delegation_end(request, pk, delegation_pk):
    """End a delegation early (its delegator, or whoever administers the group)."""
    group = get_object_or_404(Group.objects.select_related("parent"), pk=pk)
    delegation = get_object_or_404(DutyDelegation, pk=delegation_pk, group=group)
    if delegation.delegator_id != request.user.pk:
        require(request.user, group, MANAGE)
    if delegation.is_active:
        delegation.is_active = False
        delegation.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "Delegation ended.")
    return redirect("pwms:group_detail", pk=group.pk)


def group_member_search(request, pk):
    """Search a group's active members, for the delegation's member picker.

    The delegator must already belong to the group, so the picker must not
    suggest people the form will reject on submit. The delegate picker is a
    different search (see :func:`duty_delegate_search`).
    """
    group = get_object_or_404(Group, pk=pk)
    search_query = request.GET.get("search", "").strip()
    users = User.objects.filter(
        is_active=True,
        memberships__group=group,
        memberships__is_active=True,
    ).distinct()
    if search_query:
        users = users.filter(
            Q(username__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
        )
    return render(
        request=request,
        template_name="pwms/partials/user_search_results.html",
        context={"users": users.order_by("first_name", "last_name", "username")[:20]},
    )


def duty_delegate_search(request, pk):
    """Search possible stand-ins for a member's delegation, for HTMX.

    The delegate must be the delegator's peer (MP to MP, staff to staff), must
    not already belong to the group, and must not already be an active delegate
    there. Until a valid delegator is chosen there is nothing to offer.
    """
    group = get_object_or_404(Group, pk=pk)
    delegator = None
    raw_delegator = request.GET.get("delegator", "")
    if raw_delegator.isdigit():
        delegator = User.objects.filter(pk=int(raw_delegator)).first()

    if (
        delegator is None
        or not GroupMembership.objects.filter(
            group=group, user=delegator, is_active=True
        ).exists()
    ):
        users = User.objects.none()
    else:
        users = User.delegation_peers(delegator)
        users = users.exclude(
            id__in=GroupMembership.objects.filter(group=group, is_active=True).values(
                "user_id"
            )
        )
        users = users.exclude(
            id__in=DutyDelegation.objects.current()
            .filter(group=group)
            .values("delegate_id")
        )
        search_query = request.GET.get("search", "").strip()
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
