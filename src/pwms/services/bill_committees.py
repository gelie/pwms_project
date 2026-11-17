"""Committee choices for a bill's responsible-committee field.

A bill is committed to a committee of the National Assembly or the NCOP. The
legislative tree keeps each House's committees below a "… Committees" container
(with rules committees and their subcommittees nested deeper), while a joint
committee hangs off Parliament, not either House, so it is not a valid choice.
"""

from ..membership.resolver import HOUSE_BY_CODE
from ..models import Group


def bill_committee_choices():
    """Queryset of the committees below the National Assembly and NCOP houses."""
    houses = Group.objects.filter(
        group_type=Group.Type.HOUSE, name__in=HOUSE_BY_CODE.values()
    )
    committee_ids = set()
    for house in houses:
        committee_ids.update(
            house.get_descendants()
            .filter(group_type=Group.Type.COMMITTEE)
            .values_list("pk", flat=True)
        )
    return Group.objects.filter(pk__in=committee_ids).order_by("name")
