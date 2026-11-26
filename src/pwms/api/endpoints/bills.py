"""Bill resource endpoints."""

from uuid import UUID

from django.db.models import Q
from django.shortcuts import get_object_or_404

from ..app import bills_router
from ..auth import require_scope
from ..querysets import _attach_bill_relations, _bill_queryset, _bills_page
from ..schemas import BillOut, BillPage, ErrorOut
from ..scopes import Scope
from ..search import BILL_SEARCH_FIELDS, apply_search, group_filter_q

BILL_ORDERING = {
    "bill_number",
    "title",
    "introduced_date",
    "created_at",
    "updated_at",
}


def _bill_ordering(value: str) -> str:
    field = value.removeprefix("-")
    if field not in BILL_ORDERING:
        return "-created_at"
    return value


@bills_router.get(
    "/",
    response={200: BillPage, 400: ErrorOut, 403: ErrorOut},
    summary="List bills",
)
def list_bills(
    request,
    limit: int = 20,
    offset: int = 0,
    search: str = "",
    search_fields: str | None = None,
    bill_type: str | None = None,
    house_of_origin: str | None = None,
    public_status: str | None = None,
    responsible_committee: str | None = None,
    ordering: str = "-created_at",
):
    require_scope(request, Scope.BILLS_READ)

    queryset = apply_search(_bill_queryset(), search, BILL_SEARCH_FIELDS, search_fields)
    if bill_type:
        queryset = queryset.filter(bill_type=bill_type)
    if house_of_origin:
        queryset = queryset.filter(house_of_origin=house_of_origin)
    if public_status:
        queryset = queryset.filter(
            Q(current_state__public_status__slug__iexact=public_status)
            | Q(current_state__public_status__name__iexact=public_status)
        )
    if responsible_committee:
        queryset = queryset.filter(
            group_filter_q("responsible_committee", responsible_committee)
        )

    return _bills_page(
        request, queryset.order_by(_bill_ordering(ordering)), limit, offset
    )


@bills_router.get(
    "/{public_id}/",
    response={200: BillOut, 403: ErrorOut, 404: ErrorOut},
    summary="Retrieve a bill",
)
def get_bill(request, public_id: UUID):
    require_scope(request, Scope.BILLS_READ)
    bill = get_object_or_404(_bill_queryset(), public_id=public_id)
    _attach_bill_relations([bill])
    return bill
