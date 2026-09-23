"""Audit why a user can — or cannot — create and open a workflow type.

Access to a ``WorkflowType`` is decided by three independent gates, and adding a
``create_role`` satisfies only one of them — for the people who hold that role in
the type's *own* group:

1. the type must be ``enabled`` and owned by a ``group``;
2. the user needs an **active** ``GroupMembership`` in that exact group holding
   one of the type's ``create_roles`` — see ``WorkflowType.can_create``. Roles
   are global records, so "the role belongs to the group" (what the admin form
   checks) does not mean *this user* holds it there;
3. the user's group needs a ``WorkflowGroupAccess`` row on each **instance** to
   view or work on it — see ``AbstractLegislativeWorkflow.can``. Creating a
   record and seeing it are separate grants.

This read-only command answers "why does <user> have no access?" in one place and
flags configurations that silently deny everyone: an enabled type with no owning
group, or a ``create_role`` that no active member of the group holds. It is a
preventive check, not a repair tool — use ``sync_type_group_access`` to backfill
missing instance rows.

Usage:
    python manage.py audit_workflow_access
    python manage.py audit_workflow_access --user sbrown
    python manage.py audit_workflow_access --workflow-type "International Resolution"
    python manage.py audit_workflow_access --user sbrown --json
    python manage.py audit_workflow_access --strict

Exit status is non-zero when structural errors are found; ``--strict`` widens
that to warnings too, so the command can gate a deployment or pre-flight check.
"""

import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from pwms.models import (
    AbstractLegislativeWorkflow,
    GroupMembership,
    WorkflowGroupAccess,
    WorkflowType,
)


def concrete_workflow_models():
    """Concrete (non-abstract) subclasses of the legislative workflow base."""
    models = []
    for model in AbstractLegislativeWorkflow.__subclasses__():
        if model._meta.abstract or model in models:
            continue
        models.append(model)
    return models


class Command(BaseCommand):
    help = (
        "Audit why a user can (or cannot) create and open workflows, and flag "
        "workflow types whose create roles no active member of the group holds."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            type=str,
            default=None,
            help="Diagnose this user's access (username).",
        )
        parser.add_argument(
            "--workflow-type",
            type=str,
            default=None,
            help="Only audit this WorkflowType (name or slug).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit the report as JSON instead of formatted text.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit non-zero on warnings as well as errors.",
        )

    def handle(self, *args, **options):
        workflow_types = self._workflow_types(options["workflow_type"])
        user = self._resolve_user(options["user"])

        report = {
            "user": user.username if user is not None else None,
            "types": [],
            "errors": 0,
            "warnings": 0,
        }
        for workflow_type in workflow_types:
            entry = self._audit_type(workflow_type, user)
            report["types"].append(entry)
            report["errors"] += len(entry["errors"])
            report["warnings"] += len(entry["warnings"])

        if options["as_json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
        else:
            self._print_report(report, user)

        if report["errors"]:
            raise CommandError(f"❌ {report['errors']} error(s) found")
        if options["strict"] and report["warnings"]:
            raise CommandError(f"❌ {report['warnings']} warning(s) found (--strict)")

    # -- input resolution ----------------------------------------------------
    def _workflow_types(self, ref):
        queryset = WorkflowType.objects.order_by("name")
        if ref is None:
            return queryset
        matching = queryset.filter(Q(name__iexact=ref) | Q(slug__iexact=ref))
        if not matching.exists():
            raise CommandError(f"WorkflowType '{ref}' not found.")
        return matching

    def _resolve_user(self, username):
        if username is None:
            return None
        User = get_user_model()
        try:
            return User.objects.get(username__iexact=username)
        except User.DoesNotExist:
            raise CommandError(f"No user with username '{username}'.")

    # -- per-type audit ------------------------------------------------------
    def _audit_type(self, workflow_type, user):
        entry = {
            "name": workflow_type.name,
            "slug": workflow_type.slug,
            "enabled": workflow_type.enabled,
            "group": (workflow_type.group.name if workflow_type.group_id else None),
            "create_roles": [],
            "errors": [],
            "warnings": [],
            "user": None,
        }
        roles = list(workflow_type.create_roles.all().order_by("name"))

        if workflow_type.group_id is None:
            entry["create_roles"] = [
                {"role": role.name, "active_members": None, "held_elsewhere": None}
                for role in roles
            ]
            if workflow_type.enabled:
                entry["errors"].append(
                    "enabled type has no owning group: nobody but a superuser "
                    "can create instances of it"
                )
            if user is not None:
                entry["user"], user_warnings = self._audit_user(workflow_type, user)
                entry["warnings"].extend(user_warnings)
            return entry

        group = workflow_type.group
        for role in roles:
            in_group = GroupMembership.objects.filter(
                group_id=group.pk, role=role, is_active=True
            ).count()
            # The same global Role can be held by members of other groups; knowing
            # that count is what turns "the role exists" into "the wrong people
            # hold it, and the type's group has nobody".
            elsewhere = (
                GroupMembership.objects.filter(role=role, is_active=True)
                .exclude(group_id=group.pk)
                .values("user_id")
                .distinct()
                .count()
            )
            entry["create_roles"].append(
                {
                    "role": role.name,
                    "active_members": in_group,
                    "held_elsewhere": elsewhere,
                }
            )
            if workflow_type.enabled and in_group == 0:
                hint = (
                    f" (held by {elsewhere} active member(s) of other groups)"
                    if elsewhere
                    else ""
                )
                entry["errors"].append(
                    f"create role '{role.name}' has no active member in "
                    f"'{group.name}'{hint}"
                )

        if workflow_type.enabled and not roles:
            entry["warnings"].append(
                f"'{workflow_type.name}' declares no create roles: only "
                "superusers can create instances of it"
            )
        if (
            workflow_type.enabled
            and not GroupMembership.objects.filter(
                group_id=group.pk, is_active=True
            ).exists()
        ):
            entry["warnings"].append(
                f"owning group '{group.name}' has no active members at all"
            )

        if user is not None:
            entry["user"], user_warnings = self._audit_user(workflow_type, user)
            entry["warnings"].extend(user_warnings)
        return entry

    def _audit_user(self, workflow_type, user):
        """Per-user diagnosis for one type: the user dict plus any warnings."""
        can_create, reason, roles_in_group = self._create_diagnosis(workflow_type, user)
        records = self._record_access(workflow_type, user)
        active_groups = sorted(
            set(
                GroupMembership.objects.filter(user=user, is_active=True).values_list(
                    "group__name", flat=True
                )
            )
        )
        result = {
            "username": user.username,
            "can_create": can_create,
            "reason": reason,
            "roles_in_group": roles_in_group,
            "active_groups": active_groups,
            "records": records,
        }

        warnings = []
        if (
            not user.is_superuser
            and workflow_type.enabled
            and not can_create
            and workflow_type.create_roles.exists()
        ):
            warnings.append(
                f"{user.username} cannot create '{workflow_type.name}': {reason}"
            )
        if can_create and any(
            not record["user_groups_hold_access"] for record in records.values()
        ):
            warnings.append(
                f"{user.username} can create '{workflow_type.name}' but cannot see "
                "all of its existing records; run: python manage.py "
                f'sync_type_group_access --workflow-type "{workflow_type.name}"'
            )
        return result, warnings

    def _create_diagnosis(self, workflow_type, user):
        """Whether ``user`` may create, why, and their role in the owning group."""
        if user.is_superuser:
            return True, "superuser — all enabled types", []
        group = workflow_type.group
        if not workflow_type.enabled:
            return False, "the type is disabled", []
        if group is None:
            return False, "the type has no owning group (superusers only)", []

        create_role_ids = set(workflow_type.create_roles.values_list("pk", flat=True))
        if not create_role_ids:
            return False, "no create roles declared (superusers only)", []

        memberships = list(
            GroupMembership.objects.filter(user=user).select_related("role", "group")
        )
        in_group = [m for m in memberships if m.group_id == group.pk]
        active_in_group = [m for m in in_group if m.is_active]
        active_create_roles = [
            m.role.name for m in active_in_group if m.role_id in create_role_ids
        ]
        if active_create_roles:
            return (
                True,
                f"active member of '{group.name}' as {', '.join(active_create_roles)}",
                [m.role.name for m in active_in_group],
            )

        if in_group and not active_in_group:
            roles = ", ".join(m.role.name for m in in_group)
            return (
                False,
                f"membership in '{group.name}' ({roles}) is not active",
                [],
            )
        if in_group:
            roles = ", ".join(m.role.name for m in active_in_group)
            return (
                False,
                f"holds {roles} in '{group.name}', which is not a create role",
                [m.role.name for m in active_in_group],
            )

        elsewhere = [
            m for m in memberships if m.is_active and m.role_id in create_role_ids
        ]
        if elsewhere:
            where = "; ".join(f"{m.role.name} in '{m.group.name}'" for m in elsewhere)
            reason = (
                f"does not belong to '{group.name}' (holds a create role "
                f"elsewhere: {where})"
            )
            return False, reason, []
        return (
            False,
            f"no active membership of '{group.name}' holding a create role",
            [],
        )

    def _record_access(self, workflow_type, user):
        """Per concrete model: instance count and whether the user's groups see them."""
        user_group_ids = set(
            GroupMembership.objects.filter(user=user, is_active=True).values_list(
                "group_id", flat=True
            )
        )
        records = {}
        for model in concrete_workflow_models():
            instance_ids = list(
                model.objects.filter(workflow_type=workflow_type).values_list(
                    "pk", flat=True
                )
            )
            if not instance_ids:
                continue
            content_type = ContentType.objects.get_for_model(model)
            rows = WorkflowGroupAccess.objects.filter(
                content_type=content_type, object_id__in=instance_ids
            )
            access_group_ids = set(rows.values_list("group_id", flat=True))
            records[str(model._meta.verbose_name)] = {
                "instances": len(instance_ids),
                "groups_with_access": sorted(
                    set(rows.values_list("group__name", flat=True))
                ),
                "user_groups_hold_access": bool(user_group_ids & access_group_ids),
            }
        return records

    # -- presentation --------------------------------------------------------
    def _print_report(self, report, user):
        scope = f"user {user.username}" if user is not None else "configuration"
        self.stdout.write(
            self.style.MIGRATE_HEADING(f"\nWorkflow access audit — {scope}")
        )
        self.stdout.write("=" * 60)

        if not report["types"]:
            self.stdout.write(self.style.WARNING("No workflow types in scope."))

        for entry in report["types"]:
            status = "enabled" if entry["enabled"] else "DISABLED"
            self.stdout.write(f"\n{entry['name']}  [{status}]")
            self.stdout.write(f"  owning group : {entry['group'] or '(none)'}")

            if entry["create_roles"]:
                self.stdout.write("  create roles :")
                for role in entry["create_roles"]:
                    if role["active_members"] is None:
                        self.stdout.write(f"    • {role['role']}")
                        continue
                    detail = f"{role['active_members']} active member(s) in the group"
                    if role["held_elsewhere"]:
                        detail += f", {role['held_elsewhere']} elsewhere"
                    marker = (
                        ""
                        if role["active_members"]
                        else self.style.ERROR("  ← nobody can create")
                    )
                    self.stdout.write(f"    • {role['role']} — {detail}{marker}")
            else:
                self.stdout.write("  create roles : (none declared)")

            for error in entry["errors"]:
                self.stdout.write(self.style.ERROR(f"  ✗ ERROR  {error}"))
            for warning in entry["warnings"]:
                self.stdout.write(self.style.WARNING(f"  ⚠ WARNING  {warning}"))

            user_entry = entry["user"]
            if user_entry is not None:
                verdict = (
                    self.style.SUCCESS("yes")
                    if user_entry["can_create"]
                    else self.style.ERROR("no")
                )
                self.stdout.write(
                    f"  user {user_entry['username']}: can create = {verdict}"
                )
                self.stdout.write(f"      reason   : {user_entry['reason']}")
                if user_entry["active_groups"]:
                    groups = ", ".join(
                        f"'{name}'" for name in user_entry["active_groups"]
                    )
                    self.stdout.write(f"      groups   : active in {groups}")
                else:
                    self.stdout.write("      groups   : no active memberships")
                for label, record in user_entry["records"].items():
                    access = "yes" if record["user_groups_hold_access"] else "no"
                    self.stdout.write(
                        f"      records  : {label} — {record['instances']} "
                        f"instance(s); user's group(s) hold access: {access}"
                    )

        self.stdout.write("\n" + "=" * 60)
        if report["errors"]:
            self.stdout.write(
                self.style.ERROR(
                    f"{report['errors']} error(s), {report['warnings']} warning(s)"
                )
            )
        elif report["warnings"]:
            self.stdout.write(
                self.style.WARNING(f"No errors, {report['warnings']} warning(s)")
            )
        else:
            self.stdout.write(self.style.SUCCESS("✅ No access problems found"))
