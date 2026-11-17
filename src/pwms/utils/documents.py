"""Readable labels for document URLs.

Most document links in PWMS point at a file in SharePoint. Showing the file's
name is more useful than a generic "Download" or "Document", but not every URL
has one: a folder link, a bare site, or Graph's ``download.aspx?UniqueId=…``
endpoint all need the caller's own fallback label.
"""

from __future__ import annotations

import posixpath
from urllib.parse import unquote, urlsplit

#: Paths that name a download endpoint rather than a document.
_GENERIC_NAMES = {"download", "download.aspx", "open", "open.aspx"}


def document_name(url: str) -> str:
    """The file name at the end of ``url``'s path, or ``""`` when it has none.

    Percent-encoding is decoded (SharePoint escapes spaces and punctuation), the
    query and fragment are ignored, and generic download endpoints are treated as
    unnamed so the template's own label is used instead.
    """
    path = urlsplit(url or "").path
    name = posixpath.basename(unquote(path)).strip()
    if not name or name in {".", ".."} or name.casefold() in _GENERIC_NAMES:
        return ""
    return name
