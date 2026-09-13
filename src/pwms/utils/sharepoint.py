import logging

import httpx
from django.conf import settings
from django.utils import timezone

from pwms.models import SharepointToken

logger = logging.getLogger(__name__)

_TOKEN_BUFFER_SECONDS = 300  # Refresh 5 minutes before the real expiry.


def get_application_token():
    """
    Get SharePoint access token using application credentials (client credentials flow).
    Returns the parsed JSON response containing the access token.
    """
    logger.debug("get_application_token() called")

    # Check if we have a valid cached token
    cached_token = SharepointToken.objects.filter(is_active=True).first()
    if cached_token and not cached_token.is_expired():
        logger.debug("Using cached token")
        return {"access_token": cached_token.access_token, "token_type": "Bearer"}

    # Get new token from Microsoft
    url = settings.SHAREPOINT_TOKEN_URL
    if not url:
        raise Exception("SHAREPOINT_TOKEN_URL not configured")

    payload = {
        "client_id": settings.SHAREPOINT_CLIENT_ID,
        "client_secret": settings.SHAREPOINT_CLIENT_SECRET,
        "grant_type": "client_credentials",
        "scope": settings.SHAREPOINT_SCOPE,
    }

    # Check that required settings are configured
    if not payload["client_id"]:
        raise Exception("SHAREPOINT_CLIENT_ID not configured")
    if not payload["client_secret"]:
        raise Exception("SHAREPOINT_CLIENT_SECRET not configured")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    with httpx.Client() as client:
        try:
            response = client.post(url, data=payload, headers=headers)
            response.raise_for_status()
            token_data = response.json()
            logger.info("SharePoint token received and cached")

            # Cache the new token (with a safety buffer before real expiry).
            expires_in = token_data.get("expires_in", 3600)
            expires_at = timezone.now() + timezone.timedelta(
                seconds=max(expires_in - _TOKEN_BUFFER_SECONDS, 60)
            )

            # Deactivate old tokens
            SharepointToken.objects.filter(is_active=True).update(is_active=False)

            # Create new token record
            SharepointToken.objects.create(
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token", ""),
                expires_at=expires_at,
                is_active=True,
            )

            return token_data

        except httpx.HTTPStatusError as e:
            raise Exception(
                f"Token request failed with HTTP error: {e!s}, response: {e.response.text}"
            )
        except httpx.HTTPError as e:
            raise Exception(f"Token request failed with HTTP error: {e!s}")


def get_token():
    """
    Legacy function for backward compatibility.
    Uses the new application token method.
    """
    return get_application_token()


async def get_all_sites(token_data: dict):
    """List every SharePoint site, following ``@odata.nextLink`` pagination.

    Graph returns at most ~200 rows per page regardless of ``top``, so the
    previous single request silently truncated large tenants. Pages are
    followed until exhausted and returned as a single ``{"value": [...]}``.
    """
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    headers = {"Authorization": f"Bearer {access_token}"}
    base_url = (
        "https://graph.microsoft.com/v1.0/sites/getAllSites"
        "?$top=200&$select=id,name,displayName,webUrl,isPersonalSite,lastModifiedDateTime"
    )
    all_sites = []

    async with httpx.AsyncClient() as client:
        url = base_url
        while url:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            all_sites.extend(data.get("value") or [])
            url = data.get("@odata.nextLink")

    return {"value": all_sites}


async def get_site_details(token_data: dict, site_id: str):
    # url = f"https://graph.microsoft.com/v1.0/sites/{site_id}"
    url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root/children?$select=id,name,file,folder"

    # Extract access token from the token data
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        response = None
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise Exception(
                f"Site details request failed: {e!s}, response: {getattr(response, 'text', 'no response') if response else 'no response'}"
            )
        except Exception as e:
            raise Exception(f"Unexpected error in site details request: {e!s}")


async def get_site_drives(token_data: dict, site_id: str):
    """Get all drives for a specific site, following ``@odata.nextLink``."""
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives?$select=id,name"
    drives = []

    async with httpx.AsyncClient() as client:
        while url:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            drives.extend(data.get("value") or [])
            url = data.get("@odata.nextLink")

    return {"value": drives}


async def get_site_permissions(token_data: dict, site_id: str):
    """List the principals granted access to a site.

    Uses ``GET /sites/{site-id}/permissions`` and follows pagination. Requires
    an application permission of at least ``Sites.Manage.All`` (or
    ``Sites.FullControl.All``). If the app only holds ``Sites.Read.All`` the
    call will fail with a 403; callers should catch and report that.
    """
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/permissions?$top=100"
    permissions = []

    async with httpx.AsyncClient() as client:
        while url:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            permissions.extend(data.get("value") or [])
            url = data.get("@odata.nextLink")

    return {"value": permissions}


def iter_permission_identities(permission: dict):
    """Yield normalised principal dicts from one Graph permission object.

    Handles both the ``grantedTo``/``grantedToIdentities`` (v1) and
    ``grantedToV2``/``grantedToIdentitiesV2`` shapes. Each identity describes
    a ``user``, ``group``, ``siteUser`` or ``siteGroup`` that holds a role on
    the site.

    Yielded dicts look like::

        {"kind": "user", "graph_id": ..., "display_name": ...,
         "email": ..., "upn": ...}
        {"kind": "group", "graph_id": ..., "display_name": ..., "email": ...}
    """
    identities = list(permission.get("grantedToIdentitiesV2") or [])
    identities += list(permission.get("grantedToIdentities") or [])
    for container in (permission.get("grantedToV2"), permission.get("grantedTo")):
        if isinstance(container, dict):
            identities.append(container)

    seen = set()
    # Site users are individual people; site groups / site-user groups are
    # collections of people and are treated like groups (not individuals).
    user_kinds = {"user", "siteUser"}
    group_kinds = {"group", "siteGroup", "siteUserGroup"}
    for container in identities:
        if not isinstance(container, dict):
            continue
        # An identity container holds exactly one principal key.
        for kind, principal in container.items():
            if kind not in user_kinds | group_kinds:
                continue
            if not isinstance(principal, dict):
                continue
            graph_id = principal.get("id")
            if graph_id in seen:
                continue
            seen.add(graph_id)
            if kind in group_kinds:
                yield {
                    "kind": "group",
                    "graph_id": graph_id,
                    "display_name": principal.get("displayName") or "",
                    "email": (
                        principal.get("mail")
                        or principal.get("email")
                        or principal.get("userPrincipalName")
                        or ""
                    ),
                }
            else:
                yield {
                    "kind": "user",
                    "graph_id": graph_id,
                    "display_name": principal.get("displayName") or "",
                    "upn": principal.get("userPrincipalName") or "",
                    "email": (
                        principal.get("mail")
                        or principal.get("email")
                        or principal.get("userPrincipalName")
                        or ""
                    ),
                }


async def get_user_list(token_data: dict, site_id: str):
    """List the ``User Information List`` entries for a site, with pagination.

    Unlike ``GET /sites/{site-id}/permissions`` (which only exposes direct role
    assignments and requires ``Sites.Manage.All``), the User Information List is
    readable with ``Sites.Read.All`` and contains every principal that has been
    resolved on the site. Items are returned with their ``fields`` expanded so
    callers can inspect the display name, e-mail and login name.
    """
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    headers = {"Authorization": f"Bearer {access_token}"}
    url = (
        f"https://graph.microsoft.com/v1.0/sites/{site_id}"
        "/lists/User%20Information%20List/items?expand=fields&$top=100"
    )
    items = []

    async with httpx.AsyncClient() as client:
        while url:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            items.extend(data.get("value") or [])
            url = data.get("@odata.nextLink")

    return {"value": items}


def _user_list_content_type(item: dict) -> str:
    """Return the content-type name for a User Information List item."""
    return (
        (item.get("contentType") or {}).get("name")
        or (item.get("fields") or {}).get("ContentType")
        or ""
    )


def is_human_member(item: dict) -> bool:
    """Return ``True`` when a User Information List item is a real person.

    Excludes the System Account, service accounts and app principals, which are
    also typed as ``Person`` but have no mailbox or a non-membership login name.
    """
    fields = item.get("fields") or {}
    return (
        _user_list_content_type(item) == "Person"
        and fields.get("Title") != "System Account"
        and bool(fields.get("EMail"))
        and str(fields.get("Name", "")).startswith("i:0#.f|membership|")
    )


def iter_user_list_members(response: dict):
    """Yield the human members from a User Information List response.

    Each yielded dict contains the fields needed to match a member against a
    local :class:`~pwms.models.User` (``email``/``username`` normalised to lower
    case) plus ``display_name`` and ``is_site_admin`` for reporting.
    """
    for item in response.get("value") or []:
        if not is_human_member(item):
            continue
        fields = item.get("fields") or {}
        yield {
            "display_name": fields.get("Title") or "",
            "name": fields.get("Name") or "",
            "email": (fields.get("EMail") or "").strip().lower(),
            "username": (fields.get("UserName") or "").strip().lower(),
            "is_site_admin": bool(fields.get("IsSiteAdmin")),
        }


async def get_drive_items(token_data: dict, drive_id: str):
    """Get all items for a specific drive."""
    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"

    # Extract access token from the token data
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise Exception(
                f"Drive items request failed: {e!s}, response: {getattr(response, 'text', 'no response')}"
            )
        except Exception as e:
            raise Exception(f"Unexpected error in drive items request: {e!s}")


async def get_folder_items(token_data: dict, drive_id: str, folder_id: str):
    """Get all items for a specific folder."""
    # Handle root folder case
    folder_path = "/root" if folder_id == "root" else f"/items/{folder_id}"
    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}{folder_path}/children"

    # Extract access token from the token data
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise Exception(
                f"Folder items request failed: {e!s}, response: {getattr(response, 'text', 'no response')}"
            )
        except Exception as e:
            raise Exception(f"Unexpected error in folder items request: {e!s}")


async def upload_file(
    token_data: dict, drive_id: str, folder_id: str, filename: str, file_content: bytes
):
    """
    Upload a file to SharePoint using Graph API.
    For files >4MB, use resumable upload session.
    """
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    headers = {"Authorization": f"Bearer {access_token}"}

    # For small files (<4MB), use simple upload
    if len(file_content) < 4 * 1024 * 1024:
        # Handle root folder case
        if folder_id == "root":
            url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{filename}:/content"
        else:
            url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{folder_id}:/{filename}:/content"
        headers["Content-Type"] = "application/octet-stream"

        async with httpx.AsyncClient() as client:
            try:
                response = await client.put(url, headers=headers, content=file_content)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as e:
                raise Exception(
                    f"File upload failed: {e!s}, response: {getattr(response, 'text', 'no response')}"
                )
    else:
        # For large files, create upload session
        # Handle root folder case
        if folder_id == "root":
            upload_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{filename}:/createUploadSession"
        else:
            upload_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{folder_id}:/{filename}:/createUploadSession"

        async with httpx.AsyncClient() as client:
            try:
                # Create upload session
                session_response = await client.post(upload_url, headers=headers)
                session_response.raise_for_status()
                session_data = session_response.json()
                upload_url = session_data["uploadUrl"]

                # Upload file in chunks (recommended chunk size: 320KB * 3 = 960KB)
                chunk_size = 960 * 1024
                total_size = len(file_content)

                for i in range(0, total_size, chunk_size):
                    chunk = file_content[i : i + chunk_size]
                    content_range = f"bytes {i}-{min(i + len(chunk) - 1, total_size - 1)}/{total_size}"

                    chunk_headers = {
                        "Authorization": f"Bearer {access_token}",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": content_range,
                    }

                    chunk_response = await client.put(
                        upload_url, headers=chunk_headers, content=chunk
                    )

                    if i + len(chunk) >= total_size:
                        # Last chunk - return the final response
                        chunk_response.raise_for_status()
                        return chunk_response.json()
                    else:
                        # Intermediate chunk - should return 202 Accepted
                        if chunk_response.status_code != 202:
                            chunk_response.raise_for_status()

            except httpx.HTTPError as e:
                raise Exception(
                    f"Large file upload failed: {e!s}, response: {getattr(response, 'text', 'no response')}"
                )


async def create_folder(
    token_data: dict, drive_id: str, parent_folder_id: str, folder_name: str
):
    """
    Create a new folder in SharePoint.
    """
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    # Handle root folder case
    if parent_folder_id == "root":
        url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"
    else:
        url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{parent_folder_id}/children"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    data = {
        "name": folder_name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "rename",
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise Exception(
                f"Folder creation failed: {e!s}, response: {getattr(response, 'text', 'no response')}"
            )
