"""Unified permission resolver, shared by the web UI and the API.

Answers one question — *may ``user`` perform ``action`` on ``resource``?* — for a
fixed set of capabilities: view, edit, delete, share, comment, manage (plus
transition, which only applies to workflow instances). The same call sites can be
used from Django views (templates and ``require``) and from DRF/ninja endpoints.

Resolution is resource-type aware:

* workflow instances (subclasses of ``AbstractLegislativeWorkflow``) use their
  three-layer group/Role/State RBAC chain (``instance.can(...)``), and a child
  additionally inherits its parent's *view* access (see
  :func:`_resolve_workflow_instance`);
* any other model falls back to a conservative default, and additional resource
  types can register custom resolvers with :func:`register_resolver`.

Superusers always pass. A workflow instance's ``owner`` is always granted
view / edit / delete on it, and its ``assigned_to`` user is always granted
**view** — that field names who owes the next action, and naming someone who
cannot open the record is a dead end. Only the view right (from either grant or
the group chain) flows down to the instance's descendants. The ``manage`` action
additionally honours the global ``Role.can_manage_permissions`` capability, so a
manager role grants the ability to administer any resource even without a
per-instance grant.

A caller may also ask about a workflow instance *as if no referral had conferred
anything* (``resolve(..., ignore_referral_grants=True)``): a referral lends the
referred group edit so its members can contribute to the record, which is not
authority to administer it. The Referrals tab uses that to keep a referred group
from raising or withdrawing referrals on a record it was asked to advise on.
"""

from __future__ import annotations

from collections.abc import Callable

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models import Model

from ..models import AbstractLegislativeWorkflow

User = get_user_model()

VIEW = "view"
EDIT = "edit"
DELETE = "delete"
SHARE = "share"
COMMENT = "comment"
MANAGE = "manage"
TRANSITION = "transition"
CREATE = "create"

#: The six capabilities every resource supports.
RESOURCE_ACTIONS = (VIEW, EDIT, DELETE, SHARE, COMMENT, MANAGE)

#: Every action the resolver knows about (workflow instances add ``transition``).
ALL_ACTIONS = RESOURCE_ACTIONS + (TRANSITION, CREATE)

#: Model class -> resolver callable, populated by :func:`register_resolver`.
_RESOLVERS: dict[type[Model], Callable[..., bool]] = {}


def register_resolver(*models: type[Model]) -> Callable:
    """Decorator registering a custom resolver for one or more model classes."""

    def decorator(func: Callable[..., bool]) -> Callable[..., bool]:
        for model in models:
            _RESOLVERS[model] = func
        return func

    return decorator


def has_global_manage_role(user) -> bool:
    """True when ``user`` holds the manage-permissions capability in any group."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return user.memberships.filter(
        is_active=True, role__can_manage_permissions=True
    ).exists()


def resolve(
    user, resource, action: str, *, ignore_referral_grants: bool = False
) -> bool:
    """
    Return whether ``user`` may perform ``action`` on ``resource``.

    Unauthenticated users are denied everything; superusers are granted
    everything. ``action`` must be one of :data:`ALL_ACTIONS`.

    ``ignore_referral_grants`` resolves a workflow instance as though no referral
    had conferred anything on the user's groups (see
    ``AbstractLegislativeWorkflow.can``); it makes no difference to any other
    resource.
    """
    action = action.lower()
    if action not in ALL_ACTIONS:
        raise ValueError(f"Unknown permission action: {action!r}")

    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True

    # Global manager capability overrides the per-resource chain for manage.
    if action == MANAGE and has_global_manage_role(user):
        return True

    if isinstance(resource, AbstractLegislativeWorkflow):
        return _resolve_workflow_instance(
            user, resource, action, ignore_referral_grants=ignore_referral_grants
        )

    resolver = _RESOLVERS.get(type(resource), _default_resolver)
    return resolver(user, resource, action)


def _has_direct_view_grant(user, instance) -> bool:
    """
    True when ``user`` may view ``instance`` without any access row.

    Two people are named on the record itself: the ``owner`` who produced it, and
    the ``assigned_to`` user who owes the next action. Neither should have to
    belong to some group to find their own work.
    """
    return user.pk in (instance.owner_id, instance.assigned_to_id)


def _resolve_workflow_instance(
    user, instance, action: str, *, ignore_referral_grants: bool = False
) -> bool:
    """
    Resolve an action against a workflow instance's RBAC chain.

    Every action is resolved on the instance itself: its owner (view/edit/delete),
    its assignee (view only) and its own group/Role/State chain. In addition,
    **view** access cascades *down* the hierarchy: a child is viewable when its
    parent — or, transitively, any ancestor — is. A grant on a delegation report
    therefore lets its readers see the international resolutions it contains,
    while edit/delete/transition and the rest stay governed by each instance's own
    permissions.

    Traversal goes through :class:`WorkflowRelationship` (``get_ancestors()``),
    so it works for any parent/child :class:`WorkflowType` pair, including ones
    added later, rather than being tied to report/resolution.

    ``ignore_referral_grants`` is passed down to every group-chain check, so the
    answer rests on anything *but* a referral's own grant (see ``resolve``).
    """
    if action == CREATE:
        # An instance already exists; "create" is a type-level capability.
        return False

    # The owning user may always view, edit and delete their own record; the
    # remaining capabilities follow the instance's own group/Role/State chain.
    if action in (VIEW, EDIT, DELETE) and user.pk == instance.owner_id:
        return True
    # Assignment is a work queue, so it confers *view only*: whoever the work is
    # assigned to has to be able to open it, but acting on it still takes a real
    # group/Role/State grant — assigning work must not widen anyone's authority.
    if action == VIEW and user.pk == instance.assigned_to_id:
        return True
    if instance.can(user, action, ignore_referral_grants=ignore_referral_grants):
        return True

    # Only the view right is inherited from ancestors (owner, assignee or the
    # group chain).
    if action == VIEW:
        for ancestor in instance.get_ancestors():
            if _has_direct_view_grant(user, ancestor) or ancestor.can(
                user, action, ignore_referral_grants=ignore_referral_grants
            ):
                return True
    return False


def _default_resolver(user, resource, action: str) -> bool:
    """
    Conservative fallback for resource types without a registered resolver.

    Viewing and commenting are open to any signed-in user; every mutating
    action requires the global manage capability.
    """
    if action in (VIEW, COMMENT):
        return True
    if action == MANAGE:
        return has_global_manage_role(user)
    return False


def permissions_for(user, resource) -> dict[str, bool]:
    """Map each of the six resource actions to its resolved boolean."""
    return {action: resolve(user, resource, action) for action in RESOURCE_ACTIONS}


def visible_instances(user, queryset) -> list:
    """
    Return the workflow instances in ``queryset`` that ``user`` may view.

    Order is preserved. Uses the same :func:`resolve` chain as the detail views,
    so a listing can never surface a row whose own page would raise 403.
    """
    return [obj for obj in queryset if resolve(user, obj, VIEW)]


def require(
    user, resource, action: str, *, ignore_referral_grants: bool = False
) -> None:
    """Raise :class:`PermissionDenied` unless ``user`` may ``action`` ``resource``."""
    if not resolve(
        user, resource, action, ignore_referral_grants=ignore_referral_grants
    ):
        raise PermissionDenied(f"Permission denied: {action} on {resource}.")
