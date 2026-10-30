"""Scopes an external client can be granted.

Scopes are deliberately fine-grained per resource and *read-only* today: the
external API exists to publish selected registers to partner systems (parking,
the public website, eNominations), never to let them mutate PWMS. A client is
granted a subset at registration time (see ``ApiClient.scopes``) and every
endpoint asserts the scope it needs with :func:`require_scope`.

The values are the strings embedded in access tokens and shown in the OpenAPI
document, so renaming one is a breaking change.
"""

from django.db import models


class Scope(models.TextChoices):
    USERS_READ = "users:read", "Read users"
    GROUPS_READ = "groups:read", "Read groups"
    MEMBERSHIPS_READ = "memberships:read", "Read memberships"
    BILLS_READ = "bills:read", "Read bills"


#: Every scope the API knows about. Anything else in an ``ApiClient`` row is
#: rejected at save/validation time and ignored when a token is issued.
ALL_SCOPES: tuple[str, ...] = tuple(Scope.values)

#: Scopes that only read data. Kept as a guard for future write scopes.
READ_SCOPES = frozenset(
    {Scope.USERS_READ, Scope.GROUPS_READ, Scope.MEMBERSHIPS_READ, Scope.BILLS_READ}
)
