"""DRF permission classes backed by the :mod:`pwms.services.permissions` resolver."""

from django.shortcuts import get_object_or_404
from rest_framework.permissions import BasePermission

from ..models import AbstractLegislativeWorkflow
from ..services.permissions import (
    DELETE,
    EDIT,
    TRANSITION,
    VIEW,
    has_global_manage_role,
    resolve,
)


class ResourcePermission(BasePermission):
    """
    Authenticated access that maps DRF viewset actions onto the shared resolver.

    Any signed-in user may *read* the identity/structure/registry resources; only
    a holder of the global manage capability (``Role.can_manage_permissions``)
    — or a superuser — may create, edit or delete them. Workflow instances are
    the exception: they resolve through their own group/Role/State RBAC chain,
    so a listing only ever returns records the caller could open.

    Creation has no object to test yet, so ``has_permission`` decides it:
    managers for administrative resources, and any authenticated caller for
    workflow instances (whose viewset then checks ``WorkflowType.can_create`` in
    ``perform_create``), signalled by ``create_uses_workflow_rules``.
    """

    message = "You do not have permission to perform this action."

    #: viewset action -> capability name understood by ``resolve``.
    action_capabilities = {
        "list": VIEW,
        "retrieve": VIEW,
        "update": EDIT,
        "partial_update": EDIT,
        "destroy": DELETE,
        "transition": TRANSITION,
    }

    def has_permission(self, request, view):
        user = request.user
        if user is None or not user.is_authenticated:
            return False
        if getattr(view, "action", None) == "create":
            if getattr(view, "create_uses_workflow_rules", False):
                return True
            return self._may_manage(user)
        return True

    def has_object_permission(self, request, view, obj):
        user = request.user
        action = getattr(view, "action", None)
        capability = self.action_capabilities.get(action)
        if capability is None:
            capability = getattr(view, "permission_action", None) or VIEW

        if isinstance(obj, AbstractLegislativeWorkflow):
            return resolve(user, obj, capability)

        # Administrative / reference resources: readable by any signed-in user,
        # mutable only by the global manage capability.
        if capability == VIEW:
            return True
        return self._may_manage(user)

    @staticmethod
    def _may_manage(user) -> bool:
        return bool(user.is_superuser or has_global_manage_role(user))


class WorkflowViewPermission(BasePermission):
    """
    Allow a request only when the resolver grants ``view`` on the workflow.

    The view must expose ``model_class`` (the concrete workflow model) and carry
    a ``public_id`` URL kwarg. The instance is fetched here so a missing UUID
    still returns 404 while an existing-but-forbidden instance returns 403.
    """

    message = "You do not have permission to view this workflow."

    def has_permission(self, request, view):
        user = request.user
        if user is None or not user.is_authenticated:
            return False
        model_class = getattr(view, "model_class", None)
        public_id = view.kwargs.get("public_id")
        if model_class is None or public_id is None:
            # Not an instance-scoped endpoint; leave the decision to the view.
            return True
        instance = get_object_or_404(model_class, public_id=public_id)
        return resolve(user, instance, VIEW)
