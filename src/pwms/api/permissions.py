"""DRF permission classes backed by the :mod:`pwms.services.permissions` resolver."""

from django.shortcuts import get_object_or_404
from rest_framework.permissions import BasePermission

from ..services.permissions import VIEW, resolve


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
