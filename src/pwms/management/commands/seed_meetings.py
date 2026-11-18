"""Seed the Events calendar with presentation-ready demo meetings.

The command creates *meetings* for the Events workflow — House plenary sittings,
committee meetings and a smaller number of staff engagements (management,
administration, task teams, workshops) — and a realistic attendance register for
each. It is aimed at demonstrations: roughly two thirds of the records are
committee business, the rest spans the other meeting types, and the schedule is
spread over the last ~18 months with a handful of upcoming sittings so the
calendar, the "next up" strip and the period filters all have something to show.

Every meeting goes through the real workflow machine, so past meetings carry
genuine ``TransitionLog`` history; only the timestamps are back-dated. Automatic
alert fan-out is suppressed — a demo seed must not mail the whole institution.

The seed is self-contained but reuses whatever is already there:

* the ``Meeting`` workflow type, states and transitions (seeded by migration
  ``0050``) must exist;
* committees, Houses and administrative groups are read from the group tree
  (``seed_structure``);
* people come from the existing accounts. When a database has too few accounts
  to tell a convincing story, a small cast of ``demo-member-*`` / ``demo-staff-*``
  accounts is created (``--no-demo-users`` disables that).

Usage::

    python manage.py seed_meetings
    python manage.py seed_meetings --count 120 --seed 7
    python manage.py seed_meetings --reset --force
"""

from __future__ import annotations

import random
import re
from datetime import datetime, time, timedelta
from unittest import mock

from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pwms.models import (
    Attachment,
    Group,
    Meeting,
    MeetingAttendance,
    Notification,
    TransitionLog,
    User,
    Venue,
    WorkflowEvent,
    WorkflowGroupAccess,
    WorkflowNote,
    WorkflowReferral,
    WorkflowRelationship,
    WorkflowType,
)

#: Meeting types by relative frequency. Committee business dominates, staff
#: engagements are a smaller slice, and plenaries are scheduled regularly.
MEETING_TYPE_WEIGHTS = [
    ("portfolio_committee", 32),
    ("select_committee", 12),
    ("committee", 14),
    ("ad_hoc_committee", 4),
    ("plenary", 9),
    ("joint_sitting", 3),
    ("management", 6),
    ("administration", 4),
    ("task_team", 3),
    ("workshop", 5),
    ("public_participation", 5),
    ("other", 3),
]

HOUSE_TYPES = {"plenary", "joint_sitting"}
COMMITTEE_TYPES = {
    "portfolio_committee",
    "select_committee",
    "committee",
    "ad_hoc_committee",
}
STAFF_TYPES = {"management", "administration", "task_team"}

#: How many people a register carries, by meeting type.
REGISTER_SIZES = {
    "plenary": (18, 30),
    "joint_sitting": (20, 32),
    "portfolio_committee": (8, 15),
    "select_committee": (7, 13),
    "committee": (7, 13),
    "ad_hoc_committee": (6, 11),
    "management": (5, 9),
    "administration": (5, 9),
    "task_team": (4, 8),
    "workshop": (6, 12),
    "public_participation": (8, 16),
    "other": (5, 9),
}

PLENARY_TOPICS = [
    "Questions to the Executive",
    "Debate on the State of the Nation Address",
    "Consideration of the Budget Vote",
    "Members' statements and motions",
    "Debate on national days and commemorations",
    "Consideration of committee reports",
    "Legislation before the House",
]

JOINT_TOPICS = [
    "Consideration of the Independent Police Investigative Directorate report",
    "Deliberation on the Fiscal Framework",
    "Joint oversight of the security cluster",
    "Consideration of joint committee reports",
]

COMMITTEE_TOPICS = [
    "Departmental briefing on the annual performance plan",
    "Public hearings on the draft Bill",
    "Consideration of the quarterly performance report",
    "Oversight visit feedback and resolutions",
    "Deliberations on the budget vote",
    "Consideration and adoption of committee minutes",
    "Stakeholder engagement on service delivery",
    "Briefing by the Auditor-General",
    "Consideration of the annual report",
    "Follow-up on previous committee resolutions",
]

STAFF_TOPICS = [
    "Operational planning and priorities",
    "Budget and expenditure review",
    "Risk register and compliance update",
    "ICT service delivery and support",
    "Quarterly performance review",
    "Project steering committee",
    "Staff wellness and capacity building",
    "Digital transformation workstream",
]

WORKSHOP_TOPICS = [
    "Induction workshop for new members",
    "Legislative drafting workshop",
    "Oversight and accountability masterclass",
    "Committee chairpersons' induction",
    "Budget analysis training",
    "Stakeholder engagement methodology",
]

PUBLIC_TOPICS = [
    "Public hearings on service delivery",
    "Community engagement: local government",
    "Public submissions on the draft Bill",
    "Constituency feedback session",
    "Youth engagement on economic opportunities",
]

OTHER_TOPICS = [
    "Multi-party caucus engagement",
    "Parliamentary Group on International Relations",
    "Inter-parliamentary briefing",
    "Members' support forum",
]

OFF_SITE_VENUES = [
    ("Western Cape Provincial Parliament", "7 Wale Street, Cape Town"),
    ("Cape Town International Convention Centre", "Convention Square, Cape Town"),
    ("Stellenbosch Municipality Chambers", "Plein Street, Stellenbosch"),
    ("National Treasury Training Centre", "240 Madiba Street, Pretoria"),
]

CANCELLED_REASON = "Cancelled because the House programme was changed."

POSTPONED_REASON = "Postponed pending the availability of the accounting officer."

#: A small named cast used only when the database has too few accounts.
DEMO_MEMBER_NAMES = [
    ("Thandiwe", "Mokoena"),
    ("Sipho", "Ndlovu"),
    ("Amina", "Patel"),
    ("Johannes", "van Wyk"),
    ("Naledi", "Khumalo"),
    ("Bongani", "Dlamini"),
    ("Lerato", "Mahlangu"),
    ("Pieter", "Marais"),
    ("Zanele", "Mthembu"),
    ("Riaan", "Jacobs"),
]

DEMO_STAFF_NAMES = [
    ("Karen", "Botha"),
    ("Musa", "Sithole"),
    ("Elmarie", "Pretorius"),
]


class Command(BaseCommand):
    help = (
        "Seed the Events calendar with demo meetings (plenaries, committee "
        "meetings and staff engagements) and attendance registers."
    )

    #: Stands in for the client address on the seeded audit rows.
    IP_ADDRESS = "10.10.4.21"

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=100,
            help="How many meetings to create (default: 100).",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=20261118,
            help="Random seed, so a demo database is reproducible (default: 20261118).",
        )
        parser.add_argument(
            "--actor",
            type=str,
            default="",
            help=(
                "Username the seeded meetings are owned by (default: a member of "
                "the workflow type's owning group, else a superuser)."
            ),
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete every existing meeting before seeding (asks for confirmation).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Skip the confirmation prompt used by --reset.",
        )
        parser.add_argument(
            "--no-demo-users",
            action="store_true",
            help=(
                "Never create the fallback demo-member/demo-staff accounts; fail "
                "instead when there are too few accounts to build a register."
            ),
        )

    # -- entry point --------------------------------------------------------
    def handle(self, *args, **options):
        self.count = max(1, options["count"])
        self.rng = random.Random(options["seed"])
        self.now = timezone.now()
        self.today = timezone.localdate()
        self.with_demo_users = not options["no_demo_users"]
        self._group_member_cache: dict[int, list[User]] = {}
        self._group_code_cache: dict[int, str] = {}
        self._house_cache: dict[int, str] = {}

        self._load_workflow()
        self.actor = self._resolve_actor(options["actor"])
        self._load_reference_data()
        self._meeting_types = self._available_meeting_types()

        if options["reset"]:
            self._confirm(options["force"])
            with transaction.atomic():
                self._purge()

        with (
            transaction.atomic(),
            # A demo seed must not raise an alert for every seeded transition.
            mock.patch("pwms.notifications.notify_transition", return_value=[]),
        ):
            self.created = self._seed()
            self._ensure_member_coverage(self.created)

        self._report()

    # -- reference data -----------------------------------------------------
    def _load_workflow(self):
        self.workflow_type = WorkflowType.objects.filter(name="Meeting").first()
        if self.workflow_type is None:
            raise CommandError(
                "The 'Meeting' workflow type is not configured. Run migrations "
                "first (0050 seeds the Events workflow)."
            )

        self.states = {state.name: state for state in self.workflow_type.states.all()}
        required_states = {
            "Scheduled",
            "In Progress",
            "Completed",
            "Postponed",
            "Cancelled",
        }
        missing = sorted(required_states - set(self.states))
        if missing:
            raise CommandError(
                "The Meeting workflow is missing state(s): "
                + ", ".join(missing)
                + ". Re-run migrations."
            )

        self.transitions = {
            (transition.from_state.name, transition.to_state.name): transition
            for transition in self.workflow_type.transitions.select_related(
                "from_state", "to_state"
            )
        }
        for from_state, to_state in (
            ("Scheduled", "In Progress"),
            ("In Progress", "Completed"),
            ("Scheduled", "Postponed"),
            ("Scheduled", "Cancelled"),
        ):
            if (from_state, to_state) not in self.transitions:
                raise CommandError(
                    f"The Meeting workflow is missing the '{from_state} → "
                    f"{to_state}' transition. Re-run migrations."
                )

    def _resolve_actor(self, username: str) -> User:
        if username:
            actor = User.objects.filter(username__iexact=username).first()
            if actor is None:
                raise CommandError(f"No user with username '{username}'.")
            if not actor.is_active:
                raise CommandError(f"User '{username}' is not active.")
            return actor

        # Prefer someone in the unit that owns the workflow, so the calendar's
        # owner permissions and the audit trail look right.
        if self.workflow_type.group_id is not None:
            actor = self.workflow_type.group.get_active_members().order_by("pk").first()
            if actor is not None:
                return actor

        actor = (
            User.objects.filter(is_superuser=True, is_active=True)
            .order_by("pk")
            .first()
        )
        if actor is None:
            actor = User.objects.filter(is_active=True).order_by("pk").first()
        if actor is None:
            raise CommandError(
                "There is no active user to attribute the meetings to. Create a "
                "superuser (createsuperuser) or pass --actor USERNAME."
            )
        return actor

    def _load_reference_data(self):
        self.committees = list(
            Group.objects.filter(group_type="committee", is_active=True).order_by(
                "name"
            )
        )
        self.houses = list(
            Group.objects.filter(group_type="house", is_active=True).order_by("name")
        )
        self.admin_groups = list(
            Group.objects.filter(
                group_type__in=(
                    "administration",
                    "branch",
                    "division",
                    "office",
                    "section",
                    "unit",
                    "department",
                ),
                is_active=True,
            ).order_by("name")
        )
        if not (self.committees or self.houses or self.admin_groups):
            raise CommandError(
                "No committees, Houses or administrative groups found. Run "
                "`manage.py seed_structure` before seeding meetings."
            )

        self.venues = list(
            Venue.objects.select_related("building").filter(is_active=True)
        )
        self.members, self.staff = self._resolve_people()

    def _resolve_people(self) -> tuple[list[User], list[User]]:
        active = User.objects.filter(is_active=True).order_by("pk")
        members = list(active.filter(is_mp=True))
        staff = list(active.filter(is_staff_member=True))
        member_ids = {user.pk for user in members}
        staff = [user for user in staff if user.pk not in member_ids]

        # Members of the unit that owns the workflow are the natural organisers
        # and secretaries, even when their profile is not flagged as staff.
        if self.workflow_type.group_id is not None:
            owners = list(self.workflow_type.group.get_active_members())
            known = {user.pk for user in members + staff}
            staff.extend(user for user in owners if user.pk not in known)

        others = [
            user
            for user in active
            if user.pk not in member_ids and user.pk not in {u.pk for u in staff}
        ]

        # ``is_mp`` is not always set on imported accounts, and a committee
        # member is a meeting participant whether or not the flag was synced.
        # Take everyone holding a legislative membership, not just the flagged
        # MPs, so the registers use the real people.
        legislative = list(
            active.filter(
                memberships__is_active=True,
                memberships__group__group_type__in=(
                    "committee",
                    "committees",
                    "house",
                    "delegation",
                    "members",
                    "parties",
                    "party",
                    "parliament",
                ),
            ).distinct()
        )
        known = {user.pk for user in members}
        members.extend(user for user in legislative if user.pk not in known)

        if len(members) < 6:
            known = {user.pk for user in members}
            staff_ids = {user.pk for user in staff}
            members.extend(
                user
                for user in others
                if user.pk not in known and user.pk not in staff_ids
            )

        # Likewise, a staff designation may be missing on an imported account;
        # fall back to the remaining active people before creating demo staff.
        if len(staff) < 3:
            known = {user.pk for user in staff}
            member_ids = {user.pk for user in members}
            staff.extend(
                user
                for user in others
                if user.pk not in known and user.pk not in member_ids
            )

        if self.with_demo_users and len(members) < 6:
            members.extend(self._create_demo_people("member", 6 - len(members)))
        if self.with_demo_users and len(staff) < 3:
            staff.extend(self._create_demo_people("staff", 3 - len(staff)))

        if not members:
            members = staff or others
        if not staff:
            staff = members

        if not members or not staff:
            raise CommandError(
                "Not enough accounts to seed meetings. Import/sync users, pass "
                "--actor, or drop --no-demo-users so the command can create the "
                "fallback demo accounts."
            )
        return members, staff

    def _create_demo_people(self, kind: str, needed: int) -> list[User]:
        names = DEMO_MEMBER_NAMES if kind == "member" else DEMO_STAFF_NAMES
        created = []
        for index in range(needed):
            first_name, last_name = names[index % len(names)]
            username = f"demo-{kind}-{index + 1:02d}"
            user = User.objects.filter(username=username).first()
            if user is None:
                user = User.objects.create_user(
                    username=username,
                    email=f"{username}@demo.parliament.gov.za",
                    password=None,
                    first_name=first_name,
                    last_name=last_name,
                    is_mp=kind == "member",
                    is_staff_member=kind == "staff",
                    employee_type="member" if kind == "member" else "staff",
                )
                self.stdout.write(
                    self.style.WARNING(
                        f"Created demo account '{username}' for the attendance "
                        "registers."
                    )
                )
            created.append(user)
        return created

    def _available_meeting_types(self) -> list[tuple[str, int]]:
        """Drop meeting types the group tree cannot host, keeping the weights."""
        available = []
        for meeting_type, weight in MEETING_TYPE_WEIGHTS:
            if meeting_type in HOUSE_TYPES and not (self.houses or self.committees):
                continue
            if meeting_type in COMMITTEE_TYPES and not self.committees:
                continue
            if meeting_type in STAFF_TYPES and not (
                self.admin_groups
                or self.committees
                or self.workflow_type.group_id is not None
            ):
                continue
            if not (self.committees or self.houses or self.admin_groups):
                continue
            available.append((meeting_type, weight))
        if not available:
            raise CommandError(
                "None of the meeting types can be hosted by the groups in this "
                "database. Run `manage.py seed_structure` first."
            )
        return available

    # -- purge --------------------------------------------------------------
    def _confirm(self, force: bool):
        existing = Meeting.objects.count()
        if existing == 0:
            return
        if force:
            return
        self.stdout.write(
            self.style.WARNING(
                f"--reset will DELETE all {existing} meeting(s), their attendance "
                "registers, documents, event logs, access rows and alerts."
            )
        )
        try:
            answer = input("Type 'yes' to continue: ")
        except EOFError:
            raise CommandError("Aborted (no input; pass --force to run unattended).")
        if answer.strip().lower() not in ("y", "yes"):
            raise CommandError("Aborted.")

    def _purge(self):
        """Delete every meeting and the generic rows that point at them."""
        content_type = ContentType.objects.get_for_model(Meeting)

        # Attachments have a GenericRelation and would be collected anyway, but
        # deleting them first keeps the order explicit (and their versions with
        # them).
        Attachment.objects.filter(content_type=content_type).delete()
        WorkflowEvent.objects.filter(content_type=content_type).delete()
        TransitionLog.objects.filter(content_type=content_type).delete()
        WorkflowGroupAccess.objects.filter(content_type=content_type).delete()
        WorkflowReferral.objects.filter(content_type=content_type).delete()
        WorkflowNote.objects.filter(content_type=content_type).delete()
        Notification.objects.filter(content_type=content_type).delete()
        WorkflowRelationship.objects.filter(
            Q(parent_content_type=content_type) | Q(child_content_type=content_type)
        ).delete()
        LogEntry.objects.filter(content_type=content_type).delete()

        Meeting.objects.all().delete()

    # -- seeding ------------------------------------------------------------
    def _seed(self) -> list[Meeting]:
        content_type = ContentType.objects.get_for_model(Meeting)
        plan = self._meeting_type_plan()
        seeded = []
        for index, meeting_type in enumerate(plan):
            meeting = self._create_meeting(index, meeting_type)
            self._create_register(meeting)
            self._backdate_transitions(meeting, content_type)
            seeded.append(meeting)
        return seeded

    def _meeting_type_plan(self) -> list[str]:
        """Allocate the requested count across the weighted types up front.

        Drawing each meeting's type independently leaves small runs (and small
        ``--count`` values) open to a lopsided mix. Allocating the weighted share
        first — and then shuffling the plan — keeps committee business dominant
        at any count while still spreading the types through the calendar.
        """
        total_weight = sum(weight for _kind, weight in self._meeting_types)
        allocations = []
        for kind, weight in self._meeting_types:
            exact = self.count * weight / total_weight
            allocations.append([kind, int(exact), exact - int(exact)])

        assigned = sum(count for _kind, count, _fraction in allocations)
        ranked = sorted(
            range(len(allocations)),
            key=lambda position: allocations[position][2],
            reverse=True,
        )
        for position in ranked:
            if assigned >= self.count:
                break
            allocations[position][1] += 1
            assigned += 1

        plan = [kind for kind, count, _fraction in allocations for _ in range(count)]
        self.rng.shuffle(plan)
        return plan

    def _ensure_member_coverage(self, seeded: list[Meeting]) -> int:
        """Give every member and staff account at least one seeded meeting.

        Random sampling over a large institution can leave an individual member
        with no register row at all, which makes an otherwise valid ``?user=``
        search for that person return an empty page. A demo calendar should not
        have invisible people, so any account still uncovered is placed on a
        meeting (round-robin, invitee capacity). Existing rows are left alone and
        the ``(meeting, user)`` constraint is respected.
        """
        if not seeded:
            return 0

        people = list(
            dict.fromkeys(
                self.members
                + self.staff
                # Superusers are the demo operators; make sure the account used
                # to inspect the calendar can find itself in the per-user filter.
                + list(User.objects.filter(is_active=True, is_superuser=True))
            )
        )
        now = self.now
        added = 0
        for position, user in enumerate(people):
            if Meeting.objects.for_member(user).exists():
                continue

            meeting = seeded[position % len(seeded)]
            if MeetingAttendance.objects.filter(meeting=meeting, user=user).exists():
                continue

            end = meeting.end_datetime or meeting.start_datetime
            state_name = meeting.current_state.name
            is_past = end <= now
            invited_at = now - timedelta(days=10)
            if state_name in ("Cancelled", "Postponed"):
                rsvp_status, attendance_status = "pending", "not_recorded"
                responded_at = recorded_at = recorded_by = None
            elif is_past:
                rsvp_status, attendance_status = "accepted", "present"
                responded_at = meeting.start_datetime - timedelta(days=2)
                recorded_at = min(end + timedelta(hours=1), now - timedelta(minutes=30))
                recorded_by = meeting.secretary
            else:
                rsvp_status, attendance_status = "accepted", "not_recorded"
                responded_at = now - timedelta(days=1)
                recorded_at = recorded_by = None

            MeetingAttendance.objects.create(
                meeting=meeting,
                user=user,
                source=MeetingAttendance.SOURCE_INVITEE,
                capacity="member",
                is_required=False,
                rsvp_status=rsvp_status,
                attendance_status=attendance_status,
                invited_at=invited_at,
                responded_at=responded_at,
                recorded_at=recorded_at,
                recorded_by=recorded_by,
            )
            MeetingAttendance.objects.filter(meeting=meeting, user=user).update(
                created_at=invited_at, updated_at=invited_at
            )
            added += 1

        if added:
            self.stdout.write(
                f"  added {added} coverage place(s) so every account has a meeting"
            )
        return added

    def _create_meeting(self, index: int, meeting_type: str) -> Meeting:
        group = self._host_group(meeting_type)
        start, end = self._schedule(index)
        state_name = self._state_for(start, end, index)
        mode = self._attendance_mode(meeting_type)
        venue, off_site_venue, off_site_address = self._location(
            meeting_type, group, mode
        )
        chair = self._chair_for(group, meeting_type)
        secretary = self._secretary_for(chair)
        invited_at = (
            self.now - timedelta(days=self.rng.randint(1, 10))
            if start > self.now
            else start - timedelta(days=self.rng.randint(7, 16))
        )

        past_and_completed = state_name == "completed"
        minutes_status = self._minutes_status(state_name, past_and_completed)
        meeting = Meeting(
            workflow_type=self.workflow_type,
            current_state=self.states["Scheduled"],
            owner=self.actor,
            assigned_to=secretary,
            title=self._title(meeting_type, group),
            description=self._description(meeting_type, group),
            reference_number=self._reference(meeting_type, group, start, index),
            meeting_type=meeting_type,
            start_datetime=start,
            end_datetime=end,
            all_day=False,
            venue=venue,
            off_site_venue=off_site_venue,
            off_site_address=off_site_address,
            attendance_mode=mode,
            meeting_link=(
                f"https://teams.microsoft.com/l/meetup-join/demo-meeting-{index + 1}"
                if mode in ("hybrid", "virtual")
                else ""
            ),
            host_group=group,
            chaired_by=chair,
            secretary=secretary,
            quorum=max(2, self._register_size(meeting_type) // 2),
            agenda_summary=self._agenda(meeting_type, group),
            notes=(
                "Members are requested to submit apologies to the secretary by "
                "16:00 on the day before the meeting."
            ),
            minutes_status=minutes_status,
            minutes_filed_at=(
                end + timedelta(days=self.rng.randint(1, 4))
                if past_and_completed and minutes_status in ("filed", "approved")
                else None
            ),
            priority=self.rng.choice(["medium", "medium", "high", "low"]),
            register_generated_at=invited_at,
        )
        meeting.save()

        # Back-date the row itself so the calendar history reads naturally.
        created_at = (
            start - timedelta(days=self.rng.randint(10, 28))
            if start <= self.now
            else self.now - timedelta(days=self.rng.randint(2, 20))
        )
        Meeting.objects.filter(pk=meeting.pk).update(created_at=created_at)

        self._apply_state(meeting, state_name, secretary)
        return meeting

    def _apply_state(self, meeting: Meeting, state_name: str, actor: User):
        if state_name == "completed":
            meeting.perform_transition(
                self.transitions[("Scheduled", "In Progress")],
                actor=actor,
                ip_address=self.IP_ADDRESS,
            )
            meeting.perform_transition(
                self.transitions[("In Progress", "Completed")],
                actor=actor,
                ip_address=self.IP_ADDRESS,
            )
        elif state_name == "in_progress":
            meeting.perform_transition(
                self.transitions[("Scheduled", "In Progress")],
                actor=actor,
                ip_address=self.IP_ADDRESS,
            )
        elif state_name == "cancelled":
            meeting.perform_transition(
                self.transitions[("Scheduled", "Cancelled")],
                actor=actor,
                comment=CANCELLED_REASON,
                ip_address=self.IP_ADDRESS,
            )
        elif state_name == "postponed":
            meeting.perform_transition(
                self.transitions[("Scheduled", "Postponed")],
                actor=actor,
                comment=POSTPONED_REASON,
                ip_address=self.IP_ADDRESS,
            )

    def _create_register(self, meeting: Meeting):
        rows = []
        seen_users: set[int] = set()
        now = self.now
        start = meeting.start_datetime
        end = meeting.end_datetime or start
        is_past = end <= now
        is_shelved = meeting.current_state.name in ("Cancelled", "Postponed")

        def add(
            user: User | None,
            *,
            source: str,
            capacity: str,
            required: bool,
            guest_name: str = "",
        ):
            if user is not None:
                if user.pk in seen_users:
                    return
                seen_users.add(user.pk)
            invited_at = (
                now - timedelta(days=self.rng.randint(1, 10))
                if start > now
                else start - timedelta(days=self.rng.randint(7, 16))
            )
            rsvp_status = "pending"
            attendance_status = "not_recorded"
            responded_at = None
            recorded_at = None
            recorded_by = None

            if is_shelved:
                # Cancelled or postponed: invitations stand, nobody attended.
                rsvp_status = self.rng.choice(["pending", "pending", "accepted"])
                if rsvp_status != "pending":
                    responded_at = invited_at + timedelta(days=self.rng.randint(1, 3))
            elif is_past:
                if capacity in ("chairperson", "secretary"):
                    rsvp_status = "accepted"
                    attendance_status = "present"
                else:
                    rsvp_status = self.rng.choices(
                        ["accepted", "declined", "tentative"],
                        weights=[88, 5, 7],
                        k=1,
                    )[0]
                    if rsvp_status == "declined":
                        attendance_status = "excused"
                    else:
                        attendance_status = self.rng.choices(
                            [
                                "present",
                                "virtual",
                                "late",
                                "left_early",
                                "excused",
                                "absent",
                            ],
                            weights=[58, 16, 8, 4, 8, 6],
                            k=1,
                        )[0]
                        if meeting.attendance_mode == "on_premise" and (
                            attendance_status == "virtual"
                        ):
                            attendance_status = "present"
                responded_at = invited_at + timedelta(
                    days=self.rng.randint(1, 3), hours=self.rng.randint(1, 8)
                )
            elif start > now:
                rsvp_status = self.rng.choices(
                    ["accepted", "tentative", "pending"], weights=[58, 12, 30], k=1
                )[0]

            if is_past and attendance_status != "not_recorded":
                recorded_at = end + timedelta(hours=self.rng.randint(0, 4))
                if recorded_at > now:
                    recorded_at = now - timedelta(minutes=self.rng.randint(5, 90))
                recorded_by = meeting.secretary

            rows.append(
                MeetingAttendance(
                    meeting=meeting,
                    user=user,
                    guest_name=guest_name,
                    source=source,
                    capacity=capacity,
                    is_required=required,
                    rsvp_status=rsvp_status,
                    attendance_status=attendance_status,
                    invited_at=invited_at,
                    responded_at=responded_at,
                    recorded_at=recorded_at,
                    recorded_by=recorded_by,
                )
            )

        # The chair and secretary always have a place at the table.
        add(
            meeting.chaired_by,
            source=self._source_for(meeting, meeting.chaired_by),
            capacity="chairperson",
            required=True,
        )
        add(
            meeting.secretary,
            source=self._source_for(meeting, meeting.secretary),
            capacity="secretary",
            required=True,
        )

        size = self._register_size(meeting.meeting_type)
        host_members = self._host_members(meeting.host_group)
        member_pool, staff_pool = self._attendee_pools(meeting.meeting_type)

        # Committee business is mostly the committee's own members; staff
        # engagements are mostly staff. Both get a few cross-cutting invitees so a
        # member's own calendar spans several groups.
        if meeting.meeting_type in STAFF_TYPES:
            first_pool, second_pool = staff_pool, member_pool
        else:
            first_pool, second_pool = member_pool, staff_pool

        host_quota = int(size * 0.7) if host_members else 0
        for user in self._sample(host_members, host_quota):
            add(
                user,
                source=MeetingAttendance.SOURCE_HOST_GROUP,
                capacity="member",
                required=True,
            )

        for user in self._sample(first_pool, size):
            if len(seen_users) >= size:
                break
            add(
                user,
                source=self._source_for(meeting, user),
                capacity="member",
                required=user.pk in {u.pk for u in host_members},
            )

        for user in self._sample(second_pool, size):
            if len(seen_users) >= size:
                break
            add(
                user,
                source=MeetingAttendance.SOURCE_INVITEE,
                capacity="member",
                required=False,
            )

        # A guest expert now and then, for the public-facing meeting types.
        if meeting.meeting_type in {"public_participation", "workshop"} and (
            self.rng.random() < 0.6
        ):
            organisation = self.rng.choice(
                ["National Treasury", "Auditor-General", "Civil society", "DPWI"]
            )
            add(
                None,
                source=MeetingAttendance.SOURCE_INVITEE,
                capacity="guest",
                required=False,
                guest_name=f"Ms N. Mahlangu ({organisation})",
            )

        MeetingAttendance.objects.bulk_create(rows)
        MeetingAttendance.objects.filter(meeting=meeting).update(
            created_at=meeting.register_generated_at,
            updated_at=meeting.register_generated_at,
        )

    def _backdate_transitions(self, meeting: Meeting, content_type):
        """Move the machine's 'now' timestamps onto the meeting's own timeline."""
        logs = list(
            TransitionLog.objects.filter(
                content_type=content_type, object_id=meeting.pk
            ).order_by("timestamp", "pk")
        )
        start = meeting.start_datetime
        end = meeting.end_datetime or start
        for position, log in enumerate(logs):
            if log.to_state and log.to_state.name == "Completed":
                timestamp = end + timedelta(minutes=15)
            elif log.to_state and log.to_state.name == "In Progress":
                timestamp = start - timedelta(minutes=5)
            else:
                timestamp = max(start - timedelta(days=1), self.now - timedelta(days=2))
            TransitionLog.objects.filter(pk=log.pk).update(timestamp=timestamp)

        LogEntry.objects.filter(
            content_type=content_type, object_id=str(meeting.pk)
        ).update(timestamp=start - timedelta(days=20))

    # -- schedule / content -------------------------------------------------
    def _schedule(self, index: int):
        if index == 0:
            # One meeting is underway right now, so the calendar's "running"
            # state has something to show.
            start = self.now - timedelta(minutes=30)
            return start, start + timedelta(hours=2)

        roll = self.rng.random()
        if roll < 0.15:
            day = self.today + timedelta(days=self.rng.randint(1, 84))
        elif roll < 0.28:
            day = self.today - timedelta(days=self.rng.randint(0, 20))
        else:
            day = self.today - timedelta(days=self.rng.randint(21, 720))

        while day.weekday() >= 5:
            day += timedelta(days=1)

        start_time = self.rng.choice(
            [
                time(8, 30),
                time(9, 0),
                time(9, 30),
                time(10, 0),
                time(10, 30),
                time(11, 0),
                time(13, 0),
                time(14, 0),
            ]
        )
        start = timezone.make_aware(datetime.combine(day, start_time))
        duration = self.rng.choice([90, 120, 120, 150, 180, 240])
        return start, start + timedelta(minutes=duration)

    def _state_for(self, start, end, index: int) -> str:
        if index == 0:
            return "in_progress"
        if start > self.now:
            return "scheduled"
        roll = self.rng.random()
        if roll < 0.035:
            return "cancelled"
        if roll < 0.065:
            return "postponed"
        return "completed"

    def _host_group(self, meeting_type: str) -> Group:
        if meeting_type in HOUSE_TYPES and self.houses:
            return self.rng.choice(self.houses)
        if meeting_type in COMMITTEE_TYPES and self.committees:
            return self.rng.choice(self.committees)
        if meeting_type in STAFF_TYPES:
            pool = self.admin_groups
            if not pool and self.workflow_type.group_id is not None:
                pool = [self.workflow_type.group]
            if not pool:
                pool = self.committees
            return self.rng.choice(pool)
        pool = self.committees or self.houses or self.admin_groups
        return self.rng.choice(pool)

    def _title(self, meeting_type: str, group: Group) -> str:
        if meeting_type == "joint_sitting":
            title = f"Joint Sitting of the Houses — {self.rng.choice(JOINT_TOPICS)}"
        elif meeting_type == "plenary":
            title = f"{group.name} Plenary Sitting — {self.rng.choice(PLENARY_TOPICS)}"
        elif meeting_type in COMMITTEE_TYPES:
            title = f"{group.name} — {self.rng.choice(COMMITTEE_TOPICS)}"
        elif meeting_type in STAFF_TYPES:
            title = f"{group.name} — {self.rng.choice(STAFF_TOPICS)}"
        elif meeting_type == "workshop":
            title = f"{group.name} — {self.rng.choice(WORKSHOP_TOPICS)}"
        elif meeting_type == "public_participation":
            title = f"{group.name} — {self.rng.choice(PUBLIC_TOPICS)}"
        else:
            title = f"{group.name} — {self.rng.choice(OTHER_TOPICS)}"
        return title[:500]

    def _description(self, meeting_type: str, group: Group) -> str:
        if meeting_type in HOUSE_TYPES:
            return (
                f"A sitting of the {group.name}, convened in terms of the Rules "
                "of the House."
            )
        if meeting_type in COMMITTEE_TYPES:
            return (
                f"A scheduled meeting of the {group.name} to consider business "
                "referred to it by the House."
            )
        return (
            f"A {meeting_type.replace('_', ' ')} engagement convened by {group.name}."
        )

    def _agenda(self, meeting_type: str, group: Group) -> str:
        if meeting_type in COMMITTEE_TYPES:
            items = [
                "Opening and apologies",
                "Consideration and adoption of the minutes of the previous meeting",
                self.rng.choice(COMMITTEE_TOPICS),
                "Committee resolutions and follow-up actions",
                "Closure",
            ]
        elif meeting_type in HOUSE_TYPES:
            items = [
                "Prayers and opening remarks",
                "Members' statements",
                self.rng.choice(PLENARY_TOPICS),
                "Notices of motion",
                "Adjournment",
            ]
        elif meeting_type in STAFF_TYPES:
            items = [
                "Welcome and apologies",
                "Progress against the operational plan",
                self.rng.choice(STAFF_TOPICS),
                "Risks, budget and resources",
                "Actions and next steps",
            ]
        else:
            items = [
                "Welcome and purpose of the engagement",
                self.rng.choice(WORKSHOP_TOPICS + PUBLIC_TOPICS),
                "Facilitated discussion",
                "Way forward",
                "Closure",
            ]
        return "".join(f"<p>{index}. {item}</p>" for index, item in enumerate(items, 1))

    def _minutes_status(self, state_name: str, past_and_completed: bool) -> str:
        if not past_and_completed:
            return "not_recorded"
        roll = self.rng.random()
        if roll < 0.55:
            return "approved"
        if roll < 0.85:
            return "filed"
        return "draft"

    def _attendance_mode(self, meeting_type: str) -> str:
        if meeting_type in STAFF_TYPES:
            modes = [("hybrid", 5), ("virtual", 4), ("on_premise", 1)]
        elif meeting_type in HOUSE_TYPES:
            modes = [("on_premise", 5), ("hybrid", 4), ("virtual", 1)]
        else:
            modes = [("on_premise", 6), ("hybrid", 3), ("virtual", 1)]
        return self.rng.choices(
            [mode for mode, _ in modes], weights=[weight for _, weight in modes], k=1
        )[0]

    def _location(self, meeting_type: str, group: Group, mode: str):
        if mode == "virtual":
            return None, "", ""

        # Occasionally meet off the precinct (public hearings, workshops).
        if self.rng.random() < 0.08 and meeting_type in {
            "workshop",
            "public_participation",
            "other",
        }:
            venue_name, address = self.rng.choice(OFF_SITE_VENUES)
            return None, venue_name, address

        venue = None
        if meeting_type in HOUSE_TYPES:
            wanted = "National Assembly Chamber"
            if self._house_code(group, meeting_type) == "NCOP":
                wanted = "NCOP Chamber"
            venue = next((item for item in self.venues if item.name == wanted), None)
        elif meeting_type in COMMITTEE_TYPES:
            rooms = [
                item for item in self.venues if "Committee Room" in item.name
            ] or self.venues
            venue = self.rng.choice(rooms) if rooms else None
        else:
            venue = self.rng.choice(self.venues) if self.venues else None

        if venue is None:
            return (
                None,
                "Parliamentary Precinct",
                "Parliament Street, Cape Town",
            )
        return venue, "", ""

    def _chair_for(self, group: Group, meeting_type: str) -> User:
        if group is not None:
            chair = (
                User.objects.filter(
                    is_active=True,
                    memberships__group=group,
                    memberships__is_active=True,
                    memberships__role__name__icontains="chair",
                )
                .distinct()
                .order_by("pk")
                .first()
            )
            if chair is not None:
                return chair
            host_members = self._host_members(group)
            if host_members:
                return self.rng.choice(host_members)
        pool = self.staff if meeting_type in STAFF_TYPES else self.members
        return self.rng.choice(pool)

    def _secretary_for(self, chair: User) -> User:
        candidates = [user for user in self.staff if user.pk != chair.pk]
        if not candidates:
            candidates = [user for user in self.members if user.pk != chair.pk]
        if not candidates:
            candidates = [chair]
        return self.rng.choice(candidates)

    # -- helpers ------------------------------------------------------------
    def _host_members(self, group: Group | None) -> list[User]:
        if group is None:
            return []
        if group.pk not in self._group_member_cache:
            self._group_member_cache[group.pk] = list(
                group.get_active_members().order_by("pk")
            )
        return self._group_member_cache[group.pk]

    def _attendee_pools(self, meeting_type: str) -> tuple[list[User], list[User]]:
        if meeting_type in STAFF_TYPES:
            return self.staff, self.members
        if meeting_type in {"workshop", "public_participation", "other"}:
            return self.members, self.staff
        return self.members, self.staff

    def _source_for(self, meeting: Meeting, user: User | None) -> str:
        if user is None or meeting.host_group_id is None:
            return MeetingAttendance.SOURCE_INVITEE
        if user.pk in {member.pk for member in self._host_members(meeting.host_group)}:
            return MeetingAttendance.SOURCE_HOST_GROUP
        return MeetingAttendance.SOURCE_INVITEE

    def _sample(self, pool: list[User], count: int) -> list[User]:
        if count <= 0 or not pool:
            return []
        return self.rng.sample(pool, min(count, len(pool)))

    def _register_size(self, meeting_type: str) -> int:
        low, high = REGISTER_SIZES.get(meeting_type, (6, 12))
        return self.rng.randint(low, high)

    def _group_code(self, group: Group) -> str:
        if group.pk not in self._group_code_cache:
            if group.short_name:
                code = re.sub(r"[^A-Z0-9-]", "", group.short_name.upper())
            else:
                name = re.sub(
                    r"^(Portfolio|Select|Joint|Standing|Ad Hoc)\s+Committee\s+(on\s+)?",
                    "",
                    group.name,
                    flags=re.IGNORECASE,
                )
                words = re.findall(r"[A-Za-z]+", name)
                code = "-".join(word.upper()[:4] for word in words[:2])
            self._group_code_cache[group.pk] = (code or f"GRP{group.pk}")[:24]
        return self._group_code_cache[group.pk]

    def _house_code(self, group: Group, meeting_type: str) -> str:
        if group.pk in self._house_cache:
            return self._house_cache[group.pk]
        code = "NA"
        if meeting_type in HOUSE_TYPES:
            if "NCOP" in group.short_name.upper() or "Council of Provinces" in (
                group.name
            ):
                code = "NCOP"
        else:
            path = group.get_ancestors(include_self=True)
            if any("National Council of Provinces" in item.name for item in path):
                code = "NCOP"
        self._house_cache[group.pk] = code
        return code

    def _reference(self, meeting_type: str, group: Group, start, index: int) -> str:
        house = self._house_code(group, meeting_type)
        code = self._group_code(group)
        reference = f"{house}/{code}/{start:%Y-%m-%d}"
        if Meeting.objects.filter(reference_number__iexact=reference).exists():
            reference = f"{reference}/{index + 1}"
        return reference[:100]

    # -- report -------------------------------------------------------------
    def _report(self):
        seeded = self.created
        by_type = {}
        for meeting_type, _weight in self._meeting_types:
            by_type[meeting_type] = sum(
                1 for meeting in seeded if meeting.meeting_type == meeting_type
            )
        by_state = {
            name: sum(1 for meeting in seeded if meeting.current_state.name == name)
            for name in (
                "Scheduled",
                "In Progress",
                "Completed",
                "Postponed",
                "Cancelled",
            )
        }
        seeded_pks = [meeting.pk for meeting in seeded]
        attendance = MeetingAttendance.objects.filter(meeting_id__in=seeded_pks).count()

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Seeded {len(seeded)} meetings for the demo calendar.")
        )
        self.stdout.write(f"  attendance rows: {attendance}")
        self.stdout.write("  by type:")
        for meeting_type, count in by_type.items():
            if count:
                self.stdout.write(f"    {meeting_type:<22} {count}")
        self.stdout.write("  by state:")
        for state_name, count in by_state.items():
            if count:
                self.stdout.write(f"    {state_name:<22} {count}")

        next_up = (
            Meeting.objects.filter(
                pk__in=seeded_pks, start_datetime__gte=timezone.now()
            )
            .order_by("start_datetime")
            .first()
        )
        if next_up is not None:
            self.stdout.write(
                f"  next up: {next_up.start_datetime:%Y-%m-%d %H:%M} — {next_up.title}"
            )
