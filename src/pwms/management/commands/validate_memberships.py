"""
Django management command to validate GroupMembership integrity against the
current ``pwms`` models.

The pre-``pwms`` command called ``workflows.membership.validators``, which no
longer exists. This version checks the data the current models actually hold:

* active memberships whose ``end_date`` has passed;
* inactive memberships with no ``end_date`` (no record of when they ended);
* memberships that start in the future;
* active memberships on an inactive group or for an inactive user;
* several *active* rows for the same user, group and role;
* non-container groups with no active members, and active users with no
  membership.

``--fix-duplicates`` keeps the newest active row in each duplicated
user/group/role set and ends the older ones (setting ``end_date`` to today).
Nothing else is changed automatically.

Usage:
    python manage.py validate_memberships
    python manage.py validate_memberships --report-file=validation_report.json
    python manage.py validate_memberships --fix-duplicates
    python manage.py validate_memberships --strict
"""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, F
from django.utils import timezone

from pwms.models import Group, GroupMembership, User

#: Groups that only hold other groups; an empty one is not a data problem.
CONTAINER_GROUP_TYPES = (
    Group.Type.COMMITTEES,
    Group.Type.MEMBERS,
    Group.Type.PARTIES,
)


class Command(BaseCommand):
    help = "Validate GroupMembership data integrity and correctness"

    def add_arguments(self, parser):
        parser.add_argument(
            "--report-file",
            type=str,
            help="Save the validation report to a JSON file",
        )
        parser.add_argument(
            "--fix-duplicates",
            action="store_true",
            help=(
                "Keep the newest active membership in each duplicated "
                "user/group/role set and end the older ones"
            ),
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Treat warnings (not just errors) as a failed validation",
        )

    def handle(self, *args, **options):
        today = timezone.localdate()
        fixed = (
            self._fix_duplicates(today) if options["fix_duplicates"] else 0
        )

        errors = []
        warnings = []

        memberships = GroupMembership.objects.select_related(
            "user", "group", "role"
        )

        expired_active = memberships.filter(is_active=True, end_date__lt=today)
        self._collect(
            errors,
            "active membership already ended",
            expired_active,
        )

        inactive_without_end = memberships.filter(
            is_active=False, end_date__isnull=True
        )
        self._collect(
            errors,
            "inactive membership with no end date",
            inactive_without_end,
        )

        bad_dates = memberships.filter(
            is_active=True, end_date__isnull=False, end_date__lt=F("start_date")
        )
        self._collect(errors, "end date before start date", bad_dates)

        future_start = memberships.filter(is_active=True, start_date__gt=today)
        self._collect(warnings, "active membership starts in the future", future_start)

        inactive_group = memberships.filter(is_active=True, group__is_active=False)
        self._collect(errors, "active membership in an inactive group", inactive_group)

        inactive_user = memberships.filter(is_active=True, user__is_active=False)
        self._collect(warnings, "active membership for an inactive user", inactive_user)

        duplicates = self._duplicates()
        for row in duplicates:
            errors.append(
                {
                    "check": "duplicate active membership",
                    "detail": (
                        f"user_id={row['user_id']} group_id={row['group_id']} "
                        f"role_id={row['role_id']} has {row['total']} active rows"
                    ),
                }
            )

        empty_groups = list(
            Group.objects.filter(is_active=True)
            .exclude(group_type__in=CONTAINER_GROUP_TYPES)
            .exclude(members__is_active=True)
            .order_by("name")
        )
        for group in empty_groups:
            warnings.append(
                {
                    "check": "group has no active members",
                    "detail": f"{group.name} ({group.get_group_type_display()})",
                }
            )

        users_without_membership = (
            User.objects.filter(is_active=True)
            .exclude(memberships__is_active=True)
            .count()
        )
        if users_without_membership:
            warnings.append(
                {
                    "check": "active users with no active membership",
                    "detail": f"{users_without_membership} user(s)",
                }
            )

        report = {
            "generated_at": timezone.now().isoformat(),
            "counts": {
                "memberships": memberships.count(),
                "active_memberships": GroupMembership.objects.filter(
                    is_active=True
                ).count(),
                "groups": Group.objects.count(),
                "active_groups": Group.objects.filter(is_active=True).count(),
                "users": User.objects.count(),
                "active_users": User.objects.filter(is_active=True).count(),
                "duplicates_fixed": fixed,
            },
            "errors": errors,
            "warnings": warnings,
        }

        self._print_report(report)
        if options["report_file"]:
            self._save_report(report, options["report_file"])

        if errors:
            raise CommandError(
                f"❌ Membership validation failed with {len(errors)} error(s)"
            )
        if warnings:
            message = f"⚠️  Validation completed with {len(warnings)} warning(s)"
            if options["strict"]:
                raise CommandError(message)
            self.stdout.write(self.style.WARNING(message))
        else:
            self.stdout.write(self.style.SUCCESS("✅ All validation checks passed"))

    # -- checks ------------------------------------------------------------
    @staticmethod
    def _collect(bucket, check, queryset):
        for membership in queryset:
            bucket.append(
                {
                    "check": check,
                    "detail": (
                        f"{membership.user} — {membership.role} in "
                        f"{membership.group} (membership id={membership.pk})"
                    ),
                }
            )

    @staticmethod
    def _duplicates():
        return list(
            GroupMembership.objects.filter(is_active=True)
            .values("user_id", "group_id", "role_id")
            .annotate(total=Count("pk"))
            .filter(total__gt=1)
            .order_by("user_id", "group_id", "role_id")
        )

    def _fix_duplicates(self, today):
        """End every duplicated active row except the newest in its set."""
        fixed = 0
        for row in self._duplicates():
            rows = list(
                GroupMembership.objects.filter(
                    is_active=True,
                    user_id=row["user_id"],
                    group_id=row["group_id"],
                    role_id=row["role_id"],
                ).order_by("-start_date", "-pk")
            )
            for duplicate in rows[1:]:
                duplicate.is_active = False
                duplicate.end_date = duplicate.end_date or today
                duplicate.save(update_fields=["is_active", "end_date", "updated_at"])
                fixed += 1
        if fixed:
            self.stdout.write(
                self.style.WARNING(
                    f"🔧 Ended {fixed} duplicate active membership row(s)"
                )
            )
        return fixed

    # -- output ------------------------------------------------------------
    def _print_report(self, report):
        self.stdout.write("Membership validation")
        for key, value in report["counts"].items():
            self.stdout.write(f"   {key.replace('_', ' ').capitalize()}: {value}")

        for label, entries in (
            ("Errors", report["errors"]),
            ("Warnings", report["warnings"]),
        ):
            if not entries:
                continue
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(f"{label} ({len(entries)}):"))
            for entry in entries[:20]:
                self.stdout.write(f"   • {entry['check']}: {entry['detail']}")
            if len(entries) > 20:
                self.stdout.write(
                    f"   … and {len(entries) - 20} more (see --report-file)"
                )

    def _save_report(self, report, filename):
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, default=str)
        except OSError as exc:
            raise CommandError(f"Could not write {filename}: {exc}") from exc
        self.stdout.write(f"📁 Report saved to: {filename}")
