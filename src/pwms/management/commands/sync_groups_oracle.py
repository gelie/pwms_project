"""Retired: PWMS's group tree no longer comes from Oracle.

The structure is curated by ``seed_structure`` and stored in the database;
``sync_users_oracle`` maps ERP organisations onto it through
``pwms/membership/resolver.py`` and ``pwms/membership/group_aliases.csv``. This
command remains only so an old script fails with a useful message instead of
``Unknown command``.
"""

import argparse

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Retired — use seed_structure (the tree is no longer built from Oracle)."

    def add_arguments(self, parser):
        # Accepted and ignored so an old invocation still reaches the message.
        parser.add_argument(
            "--infer-org-parents", action="store_true", help=argparse.SUPPRESS
        )
        parser.add_argument(
            "--infer-min-votes", type=int, default=2, help=argparse.SUPPRESS
        )
        parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("--verbose", action="store_true", help=argparse.SUPPRESS)

    def handle(self, *args, **options):
        raise CommandError(
            "sync_groups_oracle is retired: the group tree is curated by "
            "'manage.py seed_structure', not built from Oracle. Run that, then "
            "keep 'manage.py audit_group_mapping' and "
            "'pwms/membership/group_aliases.csv' in step with the ERP."
        )
