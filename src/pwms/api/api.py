"""Django-Ninja API for external (partner application) integrations.

Mounted at ``/api/v1/`` from :mod:`pwms.root_urls`. Four read-only resource
families are exposed — users, memberships, bills and meetings — and access to
each is gated by the scope registered on the calling :class:`ApiClient`.

The endpoints live in :mod:`pwms.api.endpoints`, one module per resource; this
module only wires their routers onto the shared ``api`` object.
"""

from .app import (
    api,
    auth_router,
    bill_updates_router,
    bills_router,
    groups_router,
    meetings_router,
    memberships_router,
    users_router,
)

# Importing an endpoint module registers its routes on the router it decorates.
from .endpoints.auth import obtain_token, refresh_token, revoke_token
from .endpoints.bill_updates import (
    receive_bill_update,
    receive_bill_update_rejection,
)
from .endpoints.bills import get_bill, list_bills
from .endpoints.discovery import api_root, whoami
from .endpoints.groups import get_group, list_groups
from .endpoints.meetings import get_meeting, list_meetings
from .endpoints.memberships import get_membership, list_memberships
from .endpoints.search import SEARCH_RESOURCES, search
from .endpoints.users import get_user, list_user_meetings, list_users

api.add_router("/auth", auth_router)
api.add_router("/users", users_router)
api.add_router("/groups", groups_router)
api.add_router("/memberships", memberships_router)
api.add_router("/bills", bills_router)
api.add_router("/bill-updates", bill_updates_router)
api.add_router("/meetings", meetings_router)

__all__ = [
    "SEARCH_RESOURCES",
    "api",
    "api_root",
    "get_bill",
    "get_group",
    "get_meeting",
    "get_membership",
    "get_user",
    "list_bills",
    "list_groups",
    "list_meetings",
    "list_memberships",
    "list_user_meetings",
    "list_users",
    "obtain_token",
    "receive_bill_update",
    "receive_bill_update_rejection",
    "refresh_token",
    "revoke_token",
    "search",
    "whoami",
]
