"""Empty the ``Group`` and ``GroupMembership`` tables, keeping every other table.

This is a *reset*, not a data migration: it is meant for development and UAT
databases where the group tree has drifted and is about to be rebuilt with
``seed_structure`` (plus ``scrape_parliament_committees`` or ``import_groups``).
Everything else — users, workflows, meetings, bills, roles, terms — is left in
place, but some of it is affected by the group deletion, so the command resolves
the two relations that would otherwise block it and reports the rest:

* ``WorkflowReferral.referred_to`` is ``PROTECT`` **and** non-nullable, so a
  referral cannot outlive its group: referral rows are deleted.
* ``WorkflowType.group`` is ``PROTECT`` but nullable, so the type survives and
  its ``group`` is set to ``NULL``. Re-point the types after re-seeding — until
  then ``WorkflowType.can_create`` closes creation to everyone but superusers.

The cascade then removes ``WorkflowGroupAccess`` rows (and their
``WorkflowRolePermission`` / ``WorkflowStatePermission`` overrides) and blanks the
remaining columns that point at a group: ``User.department``,
``Meeting.host_group``, ``Bill.responsible_committee`` and
``InternationalResolution.responsible_group``. None of these models are
registered with ``auditlog``, so no audit rows are produced by the wipe.

The whole reset runs in one transaction, so a ``ProtectedError`` raised by a
future ``PROTECT`` field leaves the database untouched. ``--dry-run`` performs
the same work and then rolls it back, so the reported counts are the real ones.

Usage:
    python manage.py reset_groups --dry-run
    python manage.py reset_groups
    python manage.py reset_groups --noinput
    python manage.py reset_groups --memberships-only
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import ProtectedError

from pwms.models import (
    Bill,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Meeting,
    User,
    WorkflowReferral,
    WorkflowType,
)


def _m2m_link_count(model, field_name):
    """Rows in the join table behind ``model.field_name``."""
    through = model._meta.get_field(field_name).remote_field.through
    return through.objects.count()


class Command(BaseCommand):
    help = "Delete every Group and GroupMembership row without touching other data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Run the reset inside the transaction and roll it back, so the "
                "reported counts are exact and nothing is written."
            ),
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not prompt for confirmation.",
        )
        parser.add_argument(
            "--memberships-only",
            action="store_true",
            help=(
                "Only empty GroupMembership. Groups, workflow types, referrals "
                "and every other table are left untouched."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        memberships_only = options["memberships_only"]

        if Group.objects.count() == 0 and GroupMembership.objects.count() == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to reset."))
            return

        if not dry_run and options["interactive"]:
            self._confirm(memberships_only)

        try:
            # One transaction for the whole reset: `--dry-run` executes the same
            # work and then rolls it back, so the counts it prints are the ones a
            # real run would produce.
            with transaction.atomic():
                result = self._reset(memberships_only=memberships_only)
                if dry_run:
                    transaction.set_rollback(True)
        except ProtectedError as exc:
            labels = sorted({obj._meta.label for obj in exc.protected_objects})
            raise CommandError(
                "Delete is blocked by PROTECT relations this command does not "
                f"resolve: {', '.join(labels)}. Remove those rows first, or "
                "extend reset_groups to handle them."
            ) from exc

        self._report(result, dry_run=dry_run, memberships_only=memberships_only)

    def _confirm(self, memberships_only):
        self.stdout.write(
            self.style.WARNING(
                "This permanently deletes every Group and GroupMembership row.\n"
                + (
                    "Only memberships are removed; groups and their foreign keys "
                    "are untouched."
                    if memberships_only
                    else (
                        "Group deletion also removes WorkflowGroupAccess grants "
                        "(and their role/state overrides), deletes every "
                        "WorkflowReferral, and sets WorkflowType.group to NULL."
                    )
                )
            )
        )
        confirm = input("Type 'yes' to continue, or 'no' to cancel: ")
        if confirm != "yes":
            raise CommandError("reset_groups cancelled.")

    def _reset(self, *, memberships_only):
        """Resolve the PROTECT references, then delete groups and memberships."""
        nulled = {} if memberships_only else self._null_targets()

        referrals_deleted = 0
        types_unlinked = 0
        if not memberships_only:
            referrals_deleted, types_unlinked = self._resolve_protected_references()

        _, membership_detail = GroupMembership.objects.all().delete()

        group_detail = {}
        if not memberships_only:
            _, group_detail = Group.objects.all().delete()

        access_deleted = group_detail.get("pwms.WorkflowGroupAccess", 0)
        access_overrides = group_detail.get(
            "pwms.WorkflowRolePermission", 0
        ) + group_detail.get("pwms.WorkflowStatePermission", 0)
        return {
            "memberships_deleted": membership_detail.get("pwms.GroupMembership", 0),
            "groups_deleted": group_detail.get("pwms.Group", 0),
            "referrals_deleted": referrals_deleted,
            "types_unlinked": types_unlinked,
            "access_deleted": access_deleted,
            "access_overrides_deleted": access_overrides,
            "nulled": nulled,
        }

    def _resolve_protected_references(self):
        """
        Clear the two ``PROTECT`` references that block wiping the group table.

        A referral names a group in a non-nullable column, so it cannot survive
        the group; a workflow type's group is nullable, so the type is kept and
        left unpointed for the re-seed to reconnect.

        Returns the number of referral rows deleted and types unlinked.
        """
        referrals_deleted = WorkflowReferral.objects.all().delete()[0]
        types_unlinked = WorkflowType.objects.exclude(group_id=None).update(group=None)
        return referrals_deleted, types_unlinked

    def _null_targets(self):
        """Count the ``SET_NULL`` links the group cascade will blank."""
        return {
            "User.department": User.objects.exclude(department_id=None).count(),
            "Meeting.host_group": Meeting.objects.exclude(host_group_id=None).count(),
            "Bill.responsible_committee": Bill.objects.exclude(
                responsible_committee_id=None
            ).count(),
            "InternationalResolution.responsible_group": (
                InternationalResolution.objects.exclude(
                    responsible_group_id=None
                ).count()
            ),
            "WorkflowType.viewer_groups": _m2m_link_count(
                WorkflowType, "viewer_groups"
            ),
            "InternationalAgreement.referral_committees": _m2m_link_count(
                InternationalAgreement, "referral_committees"
            ),
        }

    def _report(self, result, *, dry_run, memberships_only):
        heading = "DRY RUN – nothing written." if dry_run else "Groups reset."
        self.stdout.write(
            self.style.WARNING(heading) if dry_run else self.style.SUCCESS(heading)
        )

        if memberships_only:
            self.stdout.write(
                f"  GroupMembership: {result['memberships_deleted']} deleted; "
                "groups and their references untouched"
            )
            return

        self.stdout.write(
            f"  Group: {result['groups_deleted']} deleted, "
            f"{result['memberships_deleted']} GroupMembership row(s)"
        )
        self.stdout.write(
            f"  WorkflowReferral: {result['referrals_deleted']} deleted "
            "(referred_to is non-null, so a referral cannot outlive its group)"
        )
        if result["types_unlinked"]:
            self.stdout.write(
                f"  WorkflowType.group: {result['types_unlinked']} set to NULL "
                "— re-point them after re-seeding"
            )
        else:
            self.stdout.write("  WorkflowType.group: no types were linked")
        self.stdout.write(
            f"  Cascade-deleted: {result['access_deleted']} WorkflowGroupAccess "
            f"grant(s) with {result['access_overrides_deleted']} role/state override(s)"
        )

        nulled = {label: count for label, count in result["nulled"].items() if count}
        if nulled:
            links = ", ".join(f"{label} {count}" for label, count in nulled.items())
            self.stdout.write(f"  Set to NULL: {links}")
