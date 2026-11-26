"""API root and caller-identity endpoints."""

from ..app import api
from ..schemas import ClientInfoOut, ResourceLink, RootOut
from ..scopes import Scope


@api.get("/", response=RootOut, summary="Describe the API for the calling key")
def api_root(request):
    principal = request.auth
    resources = []
    if principal.has_scope(Scope.USERS_READ):
        resources.append(ResourceLink(name="users", url="/api/v1/users/"))
    if principal.has_scope(Scope.GROUPS_READ):
        resources.append(ResourceLink(name="groups", url="/api/v1/groups/"))
    if principal.has_scope(Scope.MEMBERSHIPS_READ):
        resources.append(ResourceLink(name="memberships", url="/api/v1/memberships/"))
    if principal.has_scope(Scope.BILLS_READ):
        resources.append(ResourceLink(name="bills", url="/api/v1/bills/"))
    if principal.has_scope(Scope.MEETINGS_READ):
        resources.append(ResourceLink(name="meetings", url="/api/v1/meetings/"))
    if resources:
        resources.append(ResourceLink(name="search", url="/api/v1/search/"))

    return {
        "api": "PWMS API",
        "version": "1.0.0",
        "client": principal.client.name,
        "scopes": list(principal.scopes),
        "documentation": "/api/v1/docs",
        "resources": resources,
    }


@api.get("/me/", response=ClientInfoOut, summary="The calling client registration")
def whoami(request):
    client = request.auth.client
    return {
        "name": client.name,
        "client_id": client.client_id,
        "description": client.description,
        "contact_name": client.contact_name,
        "contact_email": client.contact_email,
        "scopes": client.enabled_scopes,
        "rate_limit_per_minute": client.rate_limit_per_minute,
        "last_used_at": client.last_used_at,
    }
