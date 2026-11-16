import logging
from urllib.parse import quote

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


async def get_drive(token_data: dict, drive_id: str):
    """Get one drive's metadata (name, driveType and parent site reference)."""
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        response = None
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise Exception(
                f"Drive request failed: {e!s}, response: {getattr(response, 'text', 'no response') if response else 'no response'}"
            )


async def get_site(token_data: dict, site_id: str):
    """Get one SharePoint site's core metadata (id, name and web URL)."""
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    url = f"https://graph.microsoft.com/v1.0/sites/{site_id}?$select=id,name,webUrl"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        response = None
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise Exception(
                f"Site request failed: {e!s}, response: {getattr(response, 'text', 'no response') if response else 'no response'}"
            )


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


async def get_item(token_data: dict, drive_id: str, item_id: str):
    """Get the metadata for a single drive item (file or folder)."""
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        response = None
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise Exception(
                f"Item metadata request failed: {e!s}, response: {getattr(response, 'text', 'no response') if response else 'no response'}"
            )
        except Exception as e:
            raise Exception(f"Unexpected error in item metadata request: {e!s}")


async def get_item_versions(token_data: dict, drive_id: str, item_id: str):
    """List the version history of a drive item, following ``@odata.nextLink``.

    SharePoint answers with one entry per version, newest first by convention,
    carrying ``id`` (the version label, e.g. ``"2.0"``), ``size``,
    ``lastModifiedDateTime`` and ``lastModifiedBy``.
    """
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/versions"
    versions = []

    async with httpx.AsyncClient() as client:
        while url:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            versions.extend(data.get("value") or [])
            url = data.get("@odata.nextLink")

    return {"value": versions}


async def get_version_download_url(
    token_data: dict, drive_id: str, item_id: str, version_id: str
):
    """Resolve the pre-authenticated download URL for one version of an item.

    Graph answers ``GET .../versions/{id}/content`` with a redirect to a
    short-lived, pre-authenticated URL. Rather than streaming the file through
    the app, the caller hands that URL to the browser; an empty string means
    Graph answered directly and no link could be resolved.
    """
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    url = (
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
        f"/versions/{quote(str(version_id), safe='')}/content"
    )
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        location = response.headers.get("location")
        if response.is_redirect and location:
            return location
        response.raise_for_status()
        return ""


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
            response = None
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
            # Bound before the requests so the error handler can report the body
            # even when the failure is a connection error (no response object).
            response = None
            try:
                # Create upload session
                response = await client.post(upload_url, headers=headers)
                response.raise_for_status()
                session_data = response.json()
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

                    response = await client.put(
                        upload_url, headers=chunk_headers, content=chunk
                    )

                    if i + len(chunk) >= total_size:
                        # Last chunk - return the final response
                        response.raise_for_status()
                        return response.json()
                    else:
                        # Intermediate chunk - should return 202 Accepted
                        if response.status_code != 202:
                            response.raise_for_status()

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


async def create_child_folder(
    token_data: dict, drive_id: str, parent_item_id: str, folder_name: str
):
    """Create a folder via ``POST /drives/{drive-id}/items/{parent-item-id}/children``.

    Unlike :func:`create_folder` this never renames a colliding name away:
    Graph answers 409 ``nameAlreadyExists`` when the name is taken, and the
    folder that is already there is resolved and returned, giving the caller
    idempotent "create it if it does not exist" semantics.

    Returns ``(item_dict, created)`` — the existing item with ``created=False``
    on a conflict. Because a *file* triggers the very same 409, a conflict that
    resolves against a non-folder (or cannot be resolved) raises instead: files
    take the name and nothing folder-shaped exists to file documents into.
    """
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    url = (
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
        f"/items/{parent_item_id}/children"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    data = {"name": folder_name, "folder": {}}

    async with httpx.AsyncClient() as client:
        response = None
        try:
            response = await client.post(url, headers=headers, json=data)
            if response.status_code == 409:
                try:
                    error_code = response.json().get("error", {}).get("code", "")
                except ValueError:
                    error_code = ""
                if error_code == "nameAlreadyExists":
                    existing = await _child_by_name(
                        token_data, drive_id, parent_item_id, folder_name
                    )
                    if existing is None or "folder" not in existing:
                        raise Exception(
                            f"A non-folder item named {folder_name!r} already "
                            f"exists under the parent folder: {response.text}"
                        )
                    return existing, False
            response.raise_for_status()
            return response.json(), True
        except httpx.HTTPError as e:
            raise Exception(
                f"Folder creation failed: {e!s}, response: {getattr(response, 'text', 'no response')}"
            )


async def _child_by_name(
    token_data: dict, drive_id: str, parent_item_id: str, name: str
):
    """Find a direct child of a folder by its exact name (paged)."""
    access_token = token_data.get("access_token")
    if not access_token:
        raise Exception("No access_token found in token data")

    url = (
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
        f"/items/{parent_item_id}/children"
    )
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        while url:
            response = None
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
            except httpx.HTTPError as e:
                raise Exception(
                    f"Folder lookup failed: {e!s}, response: {getattr(response, 'text', 'no response')}"
                )
            payload = response.json()
            for item in payload.get("value", []):
                if item.get("name") == name:
                    return item
            url = payload.get("@odata.nextLink")
    return None
