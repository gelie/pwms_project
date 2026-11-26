from ..models import (
    PublicStatus,
    State,
)


def make_state(**kwargs):
    """A ``State`` with a default public status, for these ad-hoc test machines."""
    kwargs.setdefault("public_status", PublicStatus.objects.get(name="New"))
    return State.objects.create(**kwargs)
