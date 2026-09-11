"""Unified permission resolver, shared by the web UI and the API.

Answers one question — *may ``user`` perform ``action`` on ``resource``?* — for a
fixed set of capabilities: view, edit, delete, share, comment, manage (plus
transition, which only applies to workflow instances). The same call sites can be
used from Django views (templates and ``require``) and from DRF/ninja endpoints.

Resolution is resource-type aware:

* workflow instances (subclasses of ``AbstractLegislativeWorkflow``) use their
  three-layer group/Role/State RBAC chain (``instance.can(...)``);
* any other model falls back to a conservative default, and additional resource
  types can register custom resolvers with :func:`register_resolver`.

Superusers always pass. A workflow instance's ``owner`` is always granted
view / edit / delete on it. The ``manage`` action additionally honours the global
``Role.can_manage_permissions`` capability, so a manager role grants the ability
to administer any resource even without a per-instance grant.
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


def resolve(user, resource, action: str) -> bool:
    """
    Return whether ``user`` may perform ``action`` on ``resource``.

    Unauthenticated users are denied everything; superusers are granted
    everything. ``action`` must be one of :data:`ALL_ACTIONS`.
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
        return _resolve_workflow_instance(user, resource, action)

    resolver = _RESOLVERS.get(type(resource), _default_resolver)
    return resolver(user, resource, action)


def _resolve_workflow_instance(user, instance, action: str) -> bool:
    """Resolve an action against a concrete workflow instance's RBAC chain."""
    if action == CREATE:
        # An instance already exists; "create" is a type-level capability.
        return False
    # The workflow's owner may always view, edit and delete their own record;
    # the remaining capabilities (share / comment / manage / transition) still
    # follow the instance RBAC chain.
    if action in (VIEW, EDIT, DELETE) and user.pk == instance.owner_id:
        return True
    return instance.can(user, action)


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


def require(user, resource, action: str) -> None:
    """Raise :class:`PermissionDenied` unless ``user`` may ``action`` ``resource``."""
    if not resolve(user, resource, action):
        raise PermissionDenied(f"Permission denied: {action} on {resource}.")
