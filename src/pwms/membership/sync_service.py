"""
Membership synchronisation service for the Oracle user sync.

Encapsulates the decision of which ``GroupMembership`` rows a synced user
needs:

* Members of Parliament get a membership in their House's members group
  ("NA Members" / "NCOP Members") and in their Party group.
* Staff members get memberships in their organisational unit and in the level
  above it (division / office). The Oracle ``(child, parent)`` pair is resolved
  against the tree ``seed_structure`` creates, through
  :class:`~pwms.membership.resolver.GroupResolver`. An unresolved pair creates
  nothing and leaves the existing memberships alone.
* Both sides are reconciled, never deleted: a party change, or an organisational
  unit move, ends the stale membership (``is_active=False`` + ``end_date``) so
  the history survives. Only employment-hierarchy groups are reconciled, so
  executive appointments and other group kinds cannot be ended here.

Party, house-members and parties-umbrella groups, as well as the
"Member of Parliament" / "Staff Member" roles, are created on demand when they
are missing; everything else is looked up from the caches pre-loaded by the
sync command. All writes respect the ``dry_run`` flag.
"""

import logging

from django.db import IntegrityError
from django.utils import timezone

from pwms.membership.resolver import GroupResolver
from pwms.models import Group, GroupMembership, Role

logger = logging.getLogger(__name__)

MP_ROLE_NAME = "Member of Parliament"
STAFF_ROLE_NAME = "Staff Member"

HOUSE_MEMBERS_GROUPS = {
    "NA": "NA Members",
    "NCOP": "NCOP Members",
}

HOUSE_PARTIES_GROUPS = {
    "NA": "NA Parties",
    "NCOP": "NCOP Parties",
}

#: Group types that form the employment hierarchy. Staff reconciliation only
#: looks at these, so ending a moved assignment can never touch a house,
#: committee, party or executive appointment.
ORG_GROUP_TYPES = (
    Group.Type.ADMINISTRATION,
    Group.Type.BRANCH,
    Group.Type.DIVISION,
    Group.Type.OFFICE,
    Group.Type.SECTION,
    Group.Type.UNIT,
)

#: Types that make a sensible second ("division") membership for a unit. The
#: Oracle pair names the unit and its section; the seeded tree already encodes
#: that parent, so it is read from the tree rather than resolved again.
ORG_PARENT_TYPES = (
    Group.Type.BRANCH,
    Group.Type.DIVISION,
    Group.Type.OFFICE,
    Group.Type.SECTION,
)


class MembershipSyncService:
    """Create / reactivate the memberships a single user should hold."""

    def __init__(
        self,
        role_cache: dict,
        strip_group_code_prefix,
        normalize_role_name,
        resolver: GroupResolver,
    ):
        self._role_cache = role_cache
        self._resolver = resolver
        self._house_cache: dict[str, Group] = {}
        #: Containers are keyed by ``(parent_id, name)``: the seeded tree repeats
        #: names (``House Plenaries & Procedure`` sits under both Houses), so a
        #: name-only cache would hand back another branch's group.
        self._container_cache: dict[tuple[int | None, str], Group] = {}
        self.strip_group_code_prefix = strip_group_code_prefix
        self.normalize_role_name = normalize_role_name

    def sync_user_membership(
        self,
        user,
        oracle_row,
        existing_memberships=None,
        dry_run: bool = False,
    ) -> dict:
        """Ensure the user's memberships and return aggregate counters.

        ``existing_memberships`` is accepted for interface compatibility with
        the sync command's batch cache, but memberships are checked by their
        exact ``(user, group, role)`` natural key.
        """
        row = self._parse_row(oracle_row)
        is_mp = (row["employeetype"] or "").strip() == "Member"

        targets = (
            self._mp_targets(row, dry_run)
            if is_mp
            else self._staff_targets(row, dry_run)
        )

        ended = self._end_stale_party_memberships(user, row.get("partycode"), dry_run)
        if not is_mp:
            ended += self._end_stale_org_memberships(user, targets, dry_run)

        created = updated = skipped = 0
        for group, role in targets:
            if group is None or role is None:
                skipped += 1
                continue

            action = self._ensure_membership(user, group, role, dry_run)
            if action == "created":
                created += 1
            elif action == "reactivated":
                updated += 1

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "ended": ended,
        }

    # ------------------------------------------------------------------ #
    #  Target selection                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_row(oracle_row) -> dict:
        columns = [
            "title",
            "lastname",
            "firstname",
            "middlenames",
            "email",
            "idno",
            "sex",
            "positiondesc",
            "employeetype",
            "partycode",
            "username",
            "child_org",
            "parent_org",
        ]
        values = list(oracle_row or [])
        values += [""] * (len(columns) - len(values))
        return dict(zip(columns, values[: len(columns)]))

    def _mp_targets(self, row: dict, dry_run: bool) -> list:
        positiondesc = (row.get("positiondesc") or "").strip()
        house_code = self._house_code_from_position(positiondesc)

        role = self._get_or_create_role(
            MP_ROLE_NAME, "Elected Member of Parliament", dry_run
        )

        targets = []
        members_group = self._get_or_create_house_members_group(house_code, dry_run)
        targets.append((members_group, role))

        party_group = self._get_or_create_party_group(
            house_code, row.get("partycode"), dry_run
        )
        if party_group is not None:
            targets.append((party_group, role))

        return targets

    def _staff_targets(self, row: dict, dry_run: bool) -> list:
        role = self._resolve_staff_role(
            row.get("positiondesc"), row.get("employeetype"), dry_run
        )
        return self._seeded_staff_targets(row, role)

    def _seeded_staff_targets(self, row: dict, role: Role | None) -> list:
        """Resolve the Oracle org pair against the seeded tree.

        The pair resolves to the most specific unit (through the alias table),
        and the level above it supplies the broader membership RBAC expects.
        An unresolved pair yields no targets, which leaves the user's existing
        memberships untouched — the same rule the department link follows.
        """
        resolution = self._resolver.resolve_org(
            row.get("child_org"), row.get("parent_org")
        )
        if not resolution.resolved:
            return []

        targets = [(resolution.group, role)]
        parent = self._organisation_parent(resolution.group)
        if parent is not None:
            targets.append((parent, role))
        return targets

    @staticmethod
    def _organisation_parent(group: Group) -> Group | None:
        """The division / office a unit reports to, for the broader membership."""
        if group.group_type not in (Group.Type.UNIT, Group.Type.SECTION):
            return None
        parent = group.parent
        if parent is None or parent.group_type not in ORG_PARENT_TYPES:
            return None
        return parent

    # ------------------------------------------------------------------ #
    #  Group / role resolution (create on demand when appropriate)       #
    # ------------------------------------------------------------------ #

    def _house_code_from_position(self, positiondesc: str) -> str:
        code = positiondesc.split(":")[-1].strip() if ":" in positiondesc else ""
        if code in ("NA", "NCOP"):
            return code
        logger.warning(
            "Unclear house assignment from position %r; defaulting to NA",
            positiondesc,
        )
        return "NA"

    def _get_house(self, house_code: str) -> Group | None:
        house = self._house_cache.get(house_code)
        if house is not None:
            return house
        house = self._resolver.resolve_house(house_code)
        if house is not None:
            self._house_cache[house_code] = house
        return house

    def _get_or_create_house_members_group(
        self, house_code: str, dry_run: bool
    ) -> Group | None:
        house = self._get_house(house_code)
        name = HOUSE_MEMBERS_GROUPS[house_code]
        return self._get_or_create_group(
            name, parent=house, group_type="members", dry_run=dry_run
        )

    def _get_or_create_party_group(
        self, house_code: str, partycode, dry_run: bool
    ) -> Group | None:
        partycode = (partycode or "").strip()
        if not partycode:
            return None
        house = self._get_house(house_code)
        parties = self._get_or_create_group(
            HOUSE_PARTIES_GROUPS[house_code],
            parent=house,
            group_type="parties",
            dry_run=dry_run,
        )
        return self._get_or_create_group(
            partycode, parent=parties, group_type="party", dry_run=dry_run
        )

    def _end_stale_party_memberships(self, user, partycode, dry_run: bool) -> int:
        """End active party memberships the Oracle row no longer names.

        Party is Oracle-owned for ERP users: when a member changes party, or
        stops being an MP, the previous membership has to stop being current —
        otherwise the user sits in two parties at once and ``current_party``
        picks whichever happens to be older. The row is ended, not deleted, so
        the history of the previous party survives.

        Only party groups are touched; memberships of houses, committees and
        organisational units are reconciled by their own passes.
        """
        if user.pk is None:
            return 0

        partycode = (partycode or "").strip()
        stale = GroupMembership.objects.filter(
            user=user,
            is_active=True,
            group__group_type=Group.Type.PARTY,
        )
        if partycode:
            stale = stale.exclude(group__name=partycode)

        if dry_run:
            for membership in stale.select_related("group"):
                logger.info(
                    "[DRY RUN] Would end party membership: %s -> %s",
                    user.username,
                    membership.group.name,
                )
            return stale.count()

        return stale.update(is_active=False, end_date=timezone.now().date())

    def _end_stale_org_memberships(self, user, targets, dry_run: bool) -> int:
        """End organisational memberships the current row no longer implies.

        A staff member whose Oracle unit changes keeps a history: the previous
        membership is ended, never deleted. Only employment-hierarchy groups
        (``ORG_GROUP_TYPES``) are considered, so an executive appointment is out
        of reach even when the user also holds a payroll post. When the pair did
        not resolve there is no new assignment to compare against, and the
        existing memberships are left untouched.
        """
        if user.pk is None:
            return 0

        target_ids = {group.pk for group, _role in targets if group is not None}
        if not target_ids:
            return 0

        stale = GroupMembership.objects.filter(
            user=user,
            is_active=True,
            group__group_type__in=ORG_GROUP_TYPES,
        ).exclude(group_id__in=target_ids)

        if dry_run:
            for membership in stale.select_related("group"):
                logger.info(
                    "[DRY RUN] Would end org membership: %s -> %s",
                    user.username,
                    membership.group.name,
                )
            return stale.count()

        return stale.update(is_active=False, end_date=timezone.now().date())

    def _get_or_create_group(
        self,
        name: str,
        parent: Group | None,
        group_type: str,
        dry_run: bool,
    ) -> Group | None:
        if not name:
            return None

        key = (parent.pk if parent is not None else None, name)
        cached = self._container_cache.get(key)
        if cached is not None:
            return cached

        if parent is not None:
            group = Group.objects.filter(name=name, parent=parent).first()
        else:
            group = Group.objects.filter(name=name, parent__isnull=True).first()

        if group is None:
            if dry_run:
                logger.info("[DRY RUN] Would create group: %s", name)
                return None
            try:
                group = Group.objects.create(
                    name=name,
                    short_name=name[:50],
                    group_type=group_type,
                    description="",
                    is_active=True,
                    parent=parent,
                )
            except IntegrityError:
                # Lost a race / concurrent run; reuse the existing row.
                group = Group.objects.filter(
                    name=name,
                    parent=parent if parent is not None else None,
                ).first()
                if group is None:
                    raise

        self._container_cache[key] = group
        return group

    def _get_or_create_role(
        self, name: str, description: str, dry_run: bool
    ) -> Role | None:
        role = self._find_role(name)
        if role is not None:
            return role
        if dry_run:
            logger.info("[DRY RUN] Would create role: %s", name)
            return None
        role, _ = Role.objects.get_or_create(
            name=name, defaults={"description": description or f"Role for {name}"}
        )
        self._role_cache[name] = role
        return role

    def _find_role(self, name: str) -> Role | None:
        role = self._role_cache.get(name)
        if role is not None:
            return role
        # Case-insensitive fallback so "Ict Technician" still resolves to the
        # canonical "ICT Technician" role created by sync_roles_oracle.
        role = Role.objects.filter(name__iexact=name).first()
        if role is not None:
            self._role_cache[name] = role
        return role

    def _resolve_staff_role(
        self, positiondesc, employeetype, dry_run: bool
    ) -> Role | None:
        name = self.normalize_role_name(positiondesc or "", employeetype or "")
        if not name or name == MP_ROLE_NAME:
            name = STAFF_ROLE_NAME
        role = self._find_role(name)
        if role is None:
            role = self._get_or_create_role(
                STAFF_ROLE_NAME, "General Parliamentary Staff Member", dry_run
            )
        return role

    # ------------------------------------------------------------------ #
    #  Membership upsert                                                 #
    # ------------------------------------------------------------------ #

    def _ensure_membership(self, user, group: Group, role: Role, dry_run: bool) -> str:
        membership = GroupMembership.objects.filter(
            user=user, group=group, role=role
        ).first()

        username = getattr(user, "username", "?")

        if membership is None:
            if dry_run:
                logger.info(
                    "[DRY RUN] Would create membership: %s -> %s (%s)",
                    username,
                    group.name,
                    role.name,
                )
            else:
                GroupMembership.objects.create(
                    user=user,
                    group=group,
                    role=role,
                    start_date=timezone.now().date(),
                    is_active=True,
                )
            return "created"

        if not membership.is_active:
            if dry_run:
                logger.info(
                    "[DRY RUN] Would reactivate membership: %s -> %s (%s)",
                    username,
                    group.name,
                    role.name,
                )
            else:
                membership.is_active = True
                membership.end_date = None
                membership.save(update_fields=["is_active", "end_date"])
            return "reactivated"

        return "exists"
