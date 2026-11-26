"""Shared pagination helpers for the partner API."""

from django.conf import settings


def _max_page_size() -> int:
    return getattr(settings, "EXTERNAL_API_MAX_PAGE_SIZE", 100)


def _page_url(request, limit: int, offset: int) -> str:
    """Build the absolute URL for a page, preserving the other query params."""
    params = request.GET.copy()
    params["limit"] = str(limit)
    params["offset"] = str(offset)
    return f"{request.build_absolute_uri(request.path)}?{params.urlencode()}"


def _paginate(request, queryset, limit: int, offset: int) -> dict:
    limit = max(1, min(int(limit), _max_page_size()))
    offset = max(0, int(offset))
    count = queryset.count()
    results = list(queryset[offset : offset + limit])
    return {
        "count": count,
        "limit": limit,
        "offset": offset,
        "next": (
            _page_url(request, limit, offset + limit)
            if offset + limit < count
            else None
        ),
        "previous": (
            _page_url(request, limit, max(0, offset - limit)) if offset > 0 else None
        ),
        "results": results,
    }


def _empty_page(limit: int, offset: int) -> dict:
    return {
        "count": 0,
        "limit": limit,
        "offset": offset,
        "next": None,
        "previous": None,
        "results": [],
    }


def _clamp_limit(limit: int, *, default: int = 10) -> int:
    return max(1, min(int(limit or default), _max_page_size()))
