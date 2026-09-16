"""Seed the workflow registers with presentation-ready mock data.

The command empties the four legislative workflow registers — delegation
reports, international resolutions, international agreements and bills — plus
everything attached to them (RBAC rows, audit trail, events, referrals,
notifications, documents and auditlog entries), then repopulates them with a
realistic spread of records sitting at different states in each type's machine.

It exists for demos and mockup walkthroughs, so it is destructive by design: the
registers it owns are recreated from scratch on every run. Nothing outside those
registers is touched — users, groups, roles, workflow types, states and
transitions are left exactly as they are, so the command can be re-run after any
amount of manual poking.

The records are walked through their real state machines with
``perform_transition``, so every state change, document and referral on the
detail pages is genuine; only the *timestamps* are back-dated, spread over the
last few months so the history and activity feeds read like a live system
rather than a single batch insert.

Automatic alert fan-out is suppressed while seeding (the four registers would
otherwise mail every IR officer on every transition); a curated set of in-app
alerts is written instead so the bell and the Alerts page have something to show.

Usage::

    python manage.py seed_demo_data --force
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from unittest import mock

from auditlog.context import disable_auditlog, set_actor
from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pwms.models import (
    Attachment,
    AttachmentVersion,
    Bill,
    BillVersion,
    City,
    Country,
    DelegationParticipant,
    DelegationReport,
    DelegationReportUpdate,
    EventType,
    Group,
    InternationalAgreement,
    InternationalResolution,
    Notification,
    Transition,
    TransitionLog,
    User,
    WorkflowEvent,
    WorkflowGroupAccess,
    WorkflowReferral,
    WorkflowRelationship,
    WorkflowType,
)


def normalise_state_name(name):
    """Fold the different dashes in seeded state names ("–" vs "-") for matching."""
    return name.replace("\u2013", "-").replace("\u2014", "-").strip().lower()


@contextlib.contextmanager
def silenced_notifications():
    """Suppress the automatic alert fan-out while seeding.

    ``notify_transition`` / ``notify_referral_*`` are resolved from
    ``pwms.notifications`` lazily inside the models, so patching the package's
    public names is enough to stop the queue (and the email background tasks)
    without touching the models themselves.
    """
    with mock.patch.multiple(
        "pwms.notifications",
        notify_transition=mock.DEFAULT,
        notify_referral_created=mock.DEFAULT,
        notify_referral_closed=mock.DEFAULT,
        notify_referral_deadline=mock.DEFAULT,
    ) as patched:
        for patched_callable in patched.values():
            patched_callable.return_value = []
        yield


@dataclass
class SeededWorkflow:
    """One seeded instance plus the back-dated times for its story.

    ``created_at`` is when the row was created and ``steps`` holds one datetime
    per state change, in the order they were performed. The timeline is rebuilt
    from these after seeding (``TransitionLog`` and auditlog rows are written by
    the models with "now" and are moved onto these times in a single pass).
    """

    model: type
    instance: object
    created_at: datetime
    steps: list[datetime] = field(default_factory=list)


class Command(BaseCommand):
    help = (
        "Empty the workflow registers (and related data) and reseed them with "
        "presentation-ready mock records spread across each type's states."
    )

    #: The concrete registers this command owns and rebuilds.
    WORKFLOW_MODELS = (
        DelegationReport,
        InternationalResolution,
        InternationalAgreement,
        Bill,
    )

    #: Stands in for the client address on the seeded audit rows.
    IP_ADDRESS = "10.10.4.21"

    #: Actor usernames, keyed by the short name the seed specs use. These are the
    #: members of the two groups that own the workflow types (IRP and LSO).
    PEOPLE = {
        "sonjica": "nsonjica",
        "gelie": "gelie",
        "cpaulse": "cpaulse",
        "nkayi": "nskitasi",
        "cawe": "zcawe",
        "paulsen": "jpaulsen",
        "mhlambiso": "smhlambiso",
        "dlomo": "adlomo",
        "khuzwayo": "jkhuzwayo",
        "jardine": "zjardine",
        "mbatha": "tmbatha",
        "cupido": "acupido",
        "cele": "zcele",
        "ebrahim": "febrahim",
        "arnold": "aarnold",
        "kassan": "dkassan",
        "loots": "bloots",
        "mcgivern": "tmcgivern",
        "starkey": "thalley-starkey",
        "gangen": "tgangen",
        "isaac": "sisaac",
        "jenkins": "fjenkins",
        "mahumapelo": "sormahumapelo",
        "kumbaca": "akumbaca",
        "chetty": "mchetty",
        "litchfield": "klitchfield-tshabalala",
        "kwankwa": "nkwankwa",
        "mhlongo": "nmhlongo",
        "admin": "admin",
    }

    #: Committees and administrative groups the seed specs refer to by short key.
    GROUPS = {
        "ir": "Portfolio Committee on International Relations and Cooperation",
        "justice": "Portfolio Committee on Justice and Constitutional Development",
        "trade": "Portfolio Committee on Trade, Industry and Competition",
        "trade_select": "Select Committee on Economic Development and Trade",
        "security_justice": "Select Committee on Security and Justice",
        "health": "Portfolio Committee on Health",
        "basic_education": "Portfolio Committee on Basic Education",
        "electricity": "Portfolio Committee on Electricity and Energy",
        "forestry": "Portfolio Committee on Forestry, Fisheries and Environment",
        "minerals": "Portfolio Committee on Mineral and Petroleum Resources",
        "transport": "Portfolio Committee on Transport",
        "cogta": "Portfolio Committee on Cooperative Governance and Traditional Affairs",
        "defence": "Portfolio Committee on Defence and Military Veterans",
    }

    # -- entry point --------------------------------------------------------
    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Skip the confirmation prompt (the command empties the registers).",
        )

    def handle(self, *args, **options):
        if not options["force"]:
            self._confirm()

        self.now = timezone.now()
        self.today = timezone.localdate()
        self.seeded: list[SeededWorkflow] = []

        self._load_reference_data()

        with transaction.atomic(), silenced_notifications():
            self._purge()
            self._seed_workflows()
            self._seed_notifications()

        self._backdate()
        self._report()

    def _confirm(self):
        self.stdout.write(
            self.style.WARNING(
                "This will DELETE every delegation report, international "
                "resolution, international agreement and bill (plus their "
                "documents, referrals, alerts and audit history), then recreate "
                "them as mock data."
            )
        )
        try:
            answer = input("Type 'yes' to continue: ")
        except EOFError:
            raise CommandError("Aborted (no input; pass --force to run unattended).")
        if answer.strip().lower() not in ("y", "yes"):
            raise CommandError("Aborted.")

    # -- reference data -----------------------------------------------------
    def _load_reference_data(self):
        """Resolve the workflow types, groups, users and event types by natural key."""
        self.types = {wt.name: wt for wt in WorkflowType.objects.all()}
        for name in (
            "Delegation Report",
            "International Resolution",
            "International Agreement",
            "Bill",
        ):
            if name not in self.types:
                raise CommandError(
                    f"Workflow type '{name}' is not registered. Import the "
                    "workflow types before seeding."
                )

        self.groups = {}
        for key, name in self.GROUPS.items():
            group = Group.objects.filter(name=name).first()
            if group is None:
                raise CommandError(
                    f"Group '{name}' not found (missing groups import?)."
                )
            self.groups[key] = group

        self.people = {}
        missing = []
        for key, username in self.PEOPLE.items():
            user = User.objects.filter(username=username).first()
            if user is None:
                missing.append(username)
            else:
                self.people[key] = user
        if missing:
            raise CommandError(
                "Missing the accounts the seed data is attributed to: "
                + ", ".join(sorted(missing))
            )

        self.events = {event.slug: event for event in EventType.objects.all()}

    # -- purge --------------------------------------------------------------
    def _purge(self):
        """Delete the four registers and everything keyed to them.

        The workflow tables link to each other (and to RBAC, alerts and
        documents) through generic foreign keys, so no database cascade
        removes them: every generic table is emptied by content type first, then
        the typed children, and finally the instances.
        """
        ct_ids = [
            ContentType.objects.get_for_model(model).pk
            for model in self.WORKFLOW_MODELS
        ]

        WorkflowEvent.objects.filter(content_type_id__in=ct_ids).delete()
        WorkflowReferral.objects.filter(content_type_id__in=ct_ids).delete()
        TransitionLog.objects.filter(content_type_id__in=ct_ids).delete()
        WorkflowRelationship.objects.filter(
            Q(parent_content_type_id__in=ct_ids) | Q(child_content_type_id__in=ct_ids)
        ).delete()
        # Cascades to WorkflowRolePermission / WorkflowStatePermission.
        WorkflowGroupAccess.objects.filter(content_type_id__in=ct_ids).delete()
        Notification.objects.filter(content_type_id__in=ct_ids).delete()
        Attachment.objects.filter(content_type_id__in=ct_ids).delete()
        LogEntry.objects.filter(content_type_id__in=ct_ids).delete()

        DelegationReportUpdate.objects.all().delete()
        DelegationParticipant.objects.all().delete()
        BillVersion.objects.all().delete()

        Bill.objects.all().delete()
        InternationalAgreement.objects.all().delete()
        InternationalResolution.objects.all().delete()
        DelegationReport.objects.all().delete()

    # -- creation helpers ---------------------------------------------------
    def person(self, key):
        return self.people[key]

    def date(self, days_ago):
        return self.today - timedelta(days=days_ago)

    def moment(self, days_ago, *, hours=9):
        return (self.now - timedelta(days=days_ago)).replace(
            hour=hours, minute=15, second=0, microsecond=0
        )

    def states_for(self, workflow_type):
        """The type's states keyed by a dash-insensitive name."""
        return {normalise_state_name(s.name): s for s in workflow_type.states.all()}

    def create(self, model, *, actor, created_days_ago, state=None, **fields):
        """Create one instance and register it for back-dating."""
        workflow_type = self.types[str(model._meta.verbose_name)]
        instance = model(
            workflow_type=workflow_type,
            current_state=state or workflow_type.get_initial_state(),
            **fields,
        )
        with set_actor(actor, remote_addr=self.IP_ADDRESS):
            instance.save()
        record = SeededWorkflow(
            model=model,
            instance=instance,
            created_at=self.moment(created_days_ago),
        )
        self.seeded.append(record)
        return record

    def advance(self, record, to_state, *, actor, days_ago, comment):
        """Perform the transition into ``to_state`` and record its intended time."""
        instance = record.instance
        target = self.states_for(instance.workflow_type).get(
            normalise_state_name(to_state)
        )
        transition = (
            Transition.objects.filter(
                workflow_type=instance.workflow_type,
                from_state=instance.current_state,
                to_state=target,
            )
            .order_by("order")
            .first()
        )
        if transition is None:
            raise CommandError(
                f"No transition '{instance.current_state.name}' -> '{to_state}' "
                f"for {instance.workflow_type.name}."
            )
        with set_actor(actor, remote_addr=self.IP_ADDRESS):
            instance.perform_transition(
                transition,
                actor=actor,
                comment=comment,
                ip_address=self.IP_ADDRESS,
            )
        record.steps.append(self.moment(days_ago))
        return instance

    def record_event(self, instance, slug, *, actor, days_ago, notes="", payload=None):
        event_type = self.events.get(slug)
        if event_type is None:
            return None
        return WorkflowEvent.objects.create(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
            event_type=event_type,
            actor=actor,
            origin="user" if actor else "system",
            payload=payload or {},
            notes=notes,
            occurred_at=self.moment(days_ago),
        )

    def attach(self, instance, *, name, kind, actor, days_ago, size, versions=()):
        """File a mock SharePoint document (and its mirrored versions) on a record."""
        content_type = ContentType.objects.get_for_model(instance)
        when = self.moment(days_ago)
        attachment = Attachment(
            content_type=content_type,
            object_id=str(instance.pk),
            name=name,
            drive_id="b!mock-demo-drive",
            item_id=f"demo-{instance.pk}-{name}",
            mimetype="application/pdf",
            size=size,
            type=kind,
            uploaded_by=actor,
            sharepoint_web_url=(
                "https://parliament.sharepoint.com/sites/PWMS/Shared%20Documents/"
                + name.replace(" ", "%20")
            ),
        )
        attachment.save()
        Attachment.objects.filter(pk=attachment.pk).update(
            created_at=when, updated_at=when
        )

        for index, version in enumerate(versions, start=1):
            AttachmentVersion.objects.create(
                attachment=attachment,
                version_id=str(index),
                size=version.get("size", size),
                modified_at=self.moment(version["days_ago"]),
                modified_by=version.get("modified_by", actor.display_name),
                is_current=index == len(versions),
            )

        # Keep the record's document activity in step with the attachment.
        self.record_event(
            instance,
            "document-attached",
            actor=actor,
            days_ago=days_ago,
            notes=f"{name} attached.",
        )
        return attachment

    def add_participants(self, record, *, members, officials):
        """Record a delegation's members and support officials (BR02.3.7/8)."""
        order = 0
        for participant_type, people in (
            (DelegationParticipant.MEMBER, members),
            (DelegationParticipant.OFFICIAL, officials),
        ):
            for key, delegation_role in people:
                user = self.person(key)
                DelegationParticipant.objects.create(
                    delegation_report=record.instance,
                    participant_type=participant_type,
                    title=user.title,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    delegation_role=delegation_role,
                    user=user,
                    order=order,
                )
                order += 1

    def grant_view(self, instance, *group_keys):
        """Share an instance read-only with committees that have an interest in it."""
        content_type = ContentType.objects.get_for_model(instance)
        for key in group_keys:
            WorkflowGroupAccess.objects.get_or_create(
                content_type=content_type,
                object_id=instance.pk,
                group=self.groups[key],
                defaults={"can_view": True},
            )

    def set_m2m(self, instance, field_name, values):
        """Set a workflow's many-to-many field without writing an audit entry.

        auditlog logs m2m changes as an extra update on the row, which would
        break the one-log-per-transition pairing the back-dating relies on; the
        change is still visible on the detail page.
        """
        with disable_auditlog():
            getattr(instance, field_name).set(values)

    def refer(
        self, instance, group_key, *, actor, days_ago, due_in_days=None, notes=""
    ):
        referral = instance.refer(
            self.groups[group_key],
            referred_by=actor,
            due_date=(
                self.now + timedelta(days=due_in_days)
                if due_in_days is not None
                else None
            ),
            notes=notes,
        )
        when = self.moment(days_ago)
        WorkflowReferral.objects.filter(pk=referral.pk).update(
            referred_at=when, created_at=when, updated_at=when
        )
        # The referral-created event is written with "now" by the model.
        WorkflowEvent.objects.filter(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
            event_type=self.events.get("referral-created"),
        ).order_by("-pk").update(occurred_at=when)
        return referral

    # -- the mock registers -------------------------------------------------
    def _seed_workflows(self):
        self._seed_delegation_reports()
        self._seed_international_agreements()
        self._seed_bills()
        self._renumber_references()

    def _renumber_references(self):
        """Number the registers in date order.

        Delegation reports and agreements mint their reference on save, in
        insertion order; the bills carry Parliament's own B-number. The records
        are seeded in narrative order rather than chronological order, so without
        this the references would run backwards through the year. Renumbering by
        the story's dates makes the registers read as if they had grown over time.
        """
        for model, prefix in ((DelegationReport, "DR"), (InternationalAgreement, "IA")):
            records = sorted(
                (r for r in self.seeded if r.model is model),
                key=lambda r: r.created_at,
            )
            # Park the current values so the unique constraint never collides.
            model.objects.filter(pk__in=[r.instance.pk for r in records]).update(
                reference_number=None
            )
            for index, record in enumerate(records, start=1):
                value = f"{prefix}-{record.created_at.year}-{index:04d}"
                model.objects.filter(pk=record.instance.pk).update(
                    reference_number=value
                )
                record.instance.reference_number = value

        bills = sorted(
            (r for r in self.seeded if r.model is Bill), key=lambda r: r.created_at
        )
        old_numbers = {r.instance.pk: r.instance.bill_number for r in bills}
        for record in bills:  # bill_number is not nullable: park, then assign.
            Bill.objects.filter(pk=record.instance.pk).update(
                bill_number=f"B {record.instance.pk}-PENDING"
            )
        for index, record in enumerate(bills):
            old_number = old_numbers[record.instance.pk]
            new_number = f"B {12 + 3 * index}\u20142026"
            Bill.objects.filter(pk=record.instance.pk).update(bill_number=new_number)
            for version in record.instance.versions.all():
                BillVersion.objects.filter(pk=version.pk).update(
                    version_label=version.version_label.replace(old_number, new_number)
                )
            record.instance.bill_number = new_number

    def _seed_delegation_reports(self):
        report_type = self.types["Delegation Report"]
        states = self.states_for(report_type)

        # 1. Newly received by the IR section, still awaiting PGIR approval.
        unga = self.create(
            DelegationReport,
            actor=self.person("khuzwayo"),
            created_days_ago=14,
            owner=self.person("sonjica"),
            assigned_to=self.person("gelie"),
            title="80th Session of the United Nations General Assembly",
            description=(
                "Report on Parliament's participation in the general debate and "
                "the resolutions adopted on climate finance, peacekeeping and "
                "Security Council reform."
            ),
            engagement_name="80th Session of the United Nations General Assembly",
            engagement_start_date=self.date(150),
            engagement_end_date=self.date(141),
            location_country=self._country("United States"),
            location_city=self._city("New York", "United States"),
            priority="high",
            deadline=self.now + timedelta(days=10),
            notes="Draft report and delegation list prepared by the IR section.",
        )
        self.grant_view(unga.instance, "ir")
        self.add_participants(
            unga,
            members=[
                ("mahumapelo", "Leader of the Delegation"),
                ("kumbaca", "Delegate"),
                ("chetty", "Delegate"),
                ("litchfield", "Delegate"),
            ],
            officials=[
                ("gelie", "Delegation Secretary"),
                ("khuzwayo", "Administrative Support"),
            ],
        )

        # 2. Approved by the section and submitted for tabling.
        au = self.create(
            DelegationReport,
            actor=self.person("cpaulse"),
            created_days_ago=40,
            owner=self.person("cpaulse"),
            assigned_to=self.person("paulsen"),
            title="38th Ordinary Session of the African Union Assembly",
            description=(
                "Report on the Assembly's decisions on the African Continental "
                "Free Trade Area and the AU's peace and security agenda."
            ),
            engagement_name="38th Ordinary Session of the African Union Assembly",
            engagement_start_date=self.date(175),
            engagement_end_date=self.date(172),
            location_country=self._country("Ethiopia"),
            location_city=self._city("Addis Ababa", "Ethiopia"),
            priority="medium",
            deadline=self.now + timedelta(days=21),
            notes="Submitted to the Presiding Officers for tabling.",
        )
        self.advance(
            au,
            "Submitted for tabling",
            actor=self.person("cpaulse"),
            days_ago=34,
            comment="Report finalised and submitted to the Presiding Officers.",
        )
        self.grant_view(au.instance, "ir")
        self.add_participants(
            au,
            members=[
                ("kwankwa", "Leader of the Delegation"),
                ("mhlongo", "Delegate"),
                ("kumbaca", "Delegate"),
            ],
            officials=[
                ("paulsen", "Delegation Secretary"),
                ("jardine", "Administrative Support"),
            ],
        )

        # 3. Tabled and referred to the Portfolio Committee, decision pending.
        sadc = self.create(
            DelegationReport,
            actor=self.person("nkayi"),
            created_days_ago=45,
            owner=self.person("nkayi"),
            assigned_to=self.person("cawe"),
            title="54th Plenary Assembly of the SADC Parliamentary Forum",
            description=(
                "Report on the Forum's resolutions on election observation, "
                "regional trade and parliamentary strengthening."
            ),
            engagement_name="54th Plenary Assembly of the SADC Parliamentary Forum",
            engagement_start_date=self.date(110),
            engagement_end_date=self.date(104),
            location_country=self._country("Namibia"),
            location_city=self._city("Windhoek", "Namibia"),
            priority="high",
            deadline=self.now + timedelta(days=5),
            notes="Awaiting the Committee's report to the House.",
        )
        self.advance(
            sadc,
            "Submitted for tabling",
            actor=self.person("nkayi"),
            days_ago=40,
            comment="Signed off by the Section Manager.",
        )
        self.advance(
            sadc,
            "Tabled and referred to Committee",
            actor=self.person("sonjica"),
            days_ago=34,
            comment="Tabled and referred to the Portfolio Committee on IR and Cooperation.",
        )
        self.refer(
            sadc.instance,
            "ir",
            actor=self.person("sonjica"),
            days_ago=34,
            due_in_days=4,
            notes="Consider the report and recommend whether the House should adopt it.",
        )
        self.grant_view(sadc.instance, "ir")
        self.add_participants(
            sadc,
            members=[
                ("mahumapelo", "Leader of the Delegation"),
                ("chetty", "Delegate"),
                ("litchfield", "Delegate"),
                ("kwankwa", "Delegate"),
            ],
            officials=[
                ("cawe", "Delegation Secretary"),
                ("nkayi", "Policy Support"),
                ("mhlambiso", "Administrative Support"),
            ],
        )

        # 4. Closed: tabled, referred, adopted by the House and ATC-published.
        ipu = self.create(
            DelegationReport,
            actor=self.person("sonjica"),
            created_days_ago=120,
            owner=self.person("sonjica"),
            assigned_to=self.person("cpaulse"),
            title="150th Inter-Parliamentary Union Assembly",
            description=(
                "Report on the Assembly's deliberations on parliamentary "
                "oversight of the Sustainable Development Goals."
            ),
            engagement_name="150th Inter-Parliamentary Union Assembly",
            engagement_start_date=self.date(230),
            engagement_end_date=self.date(224),
            location_country=self._country("Uzbekistan"),
            location_city=self._city("Tashkent", "Uzbekistan"),
            priority="medium",
            deadline=self.now - timedelta(days=45),
            report_document_url=(
                "https://parliament.sharepoint.com/sites/PWMS/Shared%20Documents/"
                "IPU%20150%20-%20Delegation%20Report.pdf"
            ),
            notes="Adopted by the House; implementation monitored by the IR section.",
        )
        self.advance(
            ipu,
            "Submitted for tabling",
            actor=self.person("sonjica"),
            days_ago=112,
            comment="Report finalised by the delegation.",
        )
        self.advance(
            ipu,
            "Tabled and referred to Committee",
            actor=self.person("sonjica"),
            days_ago=104,
            comment="Tabled and referred to the Portfolio Committee on IR and Cooperation.",
        )
        self.record_event(
            ipu.instance,
            "report-document-attached",
            actor=self.person("cpaulse"),
            days_ago=104,
            notes="Delegation report filed in SharePoint.",
        )
        self.record_update(
            ipu.instance,
            actor=self.person("cpaulse"),
            days_ago=66,
            resulting_state=states[
                normalise_state_name("Tabled and referred to Committee")
            ],
            atc_reference="ATC, 12 June, p. 14",
            atc_publication_date=self.date(66),
            atc_page_number="14",
            atc_document_url=(
                "https://parliament.sharepoint.com/sites/PWMS/Shared%20Documents/"
                "ATC%2012%20June.pdf"
            ),
            notes="Report and ATC extract published.",
        )
        self.advance(
            ipu,
            "Closed",
            actor=self.person("sonjica"),
            days_ago=60,
            comment="House adopted the report; ATC publication recorded.",
        )
        self.attach(
            ipu.instance,
            name="IPU 150 - Delegation Report.pdf",
            kind="report",
            actor=self.person("cpaulse"),
            days_ago=104,
            size=2_480_113,
            versions=[
                {"days_ago": 110, "size": 2_310_004},
                {"days_ago": 104},
            ],
        )
        self.attach(
            ipu.instance,
            name="ATC 12 June - Extract.pdf",
            kind="gazette",
            actor=self.person("cpaulse"),
            days_ago=66,
            size=184_220,
        )
        self.grant_view(ipu.instance, "ir")
        self.add_participants(
            ipu,
            members=[
                ("kumbaca", "Leader of the Delegation"),
                ("chetty", "Delegate"),
                ("mhlongo", "Delegate"),
                ("mahumapelo", "Delegate"),
            ],
            officials=[
                ("cpaulse", "Delegation Secretary"),
                ("gelie", "Policy Support"),
                ("jardine", "Administrative Support"),
            ],
        )

        # 5. Tabled and referred, response overdue (BR12 identifier shows).
        cpc = self.create(
            DelegationReport,
            actor=self.person("dlomo"),
            created_days_ago=60,
            owner=self.person("dlomo"),
            assigned_to=self.person("nkayi"),
            title="Commonwealth Parliamentary Conference 2025",
            description=(
                "Report on the Conference's outcomes on parliamentary diplomacy "
                "and climate legislation."
            ),
            engagement_name="Commonwealth Parliamentary Conference",
            engagement_start_date=self.date(140),
            engagement_end_date=self.date(134),
            location_country=self._country("United Kingdom"),
            location_city=self._city("London", "United Kingdom"),
            priority="urgent",
            deadline=self.now - timedelta(days=3),
            notes="Committee response outstanding; follow up with the Committee Secretary.",
        )
        self.advance(
            cpc,
            "Submitted for tabling",
            actor=self.person("dlomo"),
            days_ago=54,
            comment="Submitted for tabling.",
        )
        self.advance(
            cpc,
            "Tabled and referred to Committee",
            actor=self.person("sonjica"),
            days_ago=48,
            comment="Tabled and referred to the Portfolio Committee on IR and Cooperation.",
        )
        self.refer(
            cpc.instance,
            "ir",
            actor=self.person("sonjica"),
            days_ago=48,
            due_in_days=-10,
            notes="Committee response is overdue.",
        )
        self.grant_view(cpc.instance, "ir")
        self.add_participants(
            cpc,
            members=[
                ("kwankwa", "Leader of the Delegation"),
                ("mhlongo", "Delegate"),
                ("litchfield", "Delegate"),
            ],
            officials=[
                ("dlomo", "Delegation Secretary"),
                ("mhlambiso", "Administrative Support"),
            ],
        )

        # -- resolutions captured from the reports (child instances) ----------
        def resolution(record, *, number, title, responsible, actor, days_ago, **spec):
            child = self.create(
                InternationalResolution,
                actor=actor,
                created_days_ago=days_ago,
                owner=actor,
                title=title,
                resolution_number=number,
                responsible_group=responsible,
                **spec,
            )
            with disable_auditlog():
                record.instance.add_sub_workflow(
                    child.instance, order=child.instance.pk
                )
            return child

        r_climate = resolution(
            unga,
            number="R-2026-UNGA-01",
            title="Mobilise climate finance for developing economies",
            responsible=self.groups["forestry"],
            actor=self.person("gelie"),
            days_ago=13,
            resolution_text=(
                "The Assembly calls on member states to double adaptation "
                "finance by 2030 and to report annually on delivery."
            ),
            implementation_progress="Referred to the Department for a funding position.",
        )
        self.grant_view(r_climate.instance, "forestry")

        r_peace = resolution(
            unga,
            number="R-2026-UNGA-02",
            title="Strengthen parliamentary oversight of peacekeeping mandates",
            responsible=self.groups["defence"],
            actor=self.person("gelie"),
            days_ago=13,
            resolution_text=(
                "Parliaments should receive quarterly briefings on peacekeeping "
                "mandates and their funding."
            ),
        )
        self.advance(
            r_peace,
            "Assigned",
            actor=self.person("sonjica"),
            days_ago=11,
            comment="Assigned to the Joint Standing Committee on Defence.",
        )
        self.record_event(
            r_peace.instance,
            "implementation-reported",
            actor=self.person("gelie"),
            days_ago=8,
            notes="Committee Secretariat briefed on the reporting cycle.",
        )
        self.grant_view(r_peace.instance, "defence")

        r_afcfta = resolution(
            au,
            number="R-2026-AU-01",
            title="Accelerate implementation of the African Continental Free Trade Area",
            responsible=self.groups["trade"],
            actor=self.person("cpaulse"),
            days_ago=38,
            resolution_text=(
                "The Assembly urges member states to finalise tariff schedules "
                "and to report on the protocols in force."
            ),
            implementation_progress=(
                "The Department of Trade, Industry and Competition has briefed the "
                "Committee on the tariff phase-down."
            ),
        )
        self.advance(
            r_afcfta,
            "Assigned",
            actor=self.person("sonjica"),
            days_ago=36,
            comment="Assigned to the Portfolio Committee on Trade, Industry and Competition.",
        )
        self.advance(
            r_afcfta,
            "In Progress",
            actor=self.person("cpaulse"),
            days_ago=30,
            comment="Committee engagement with the Department commenced.",
        )
        self.record_event(
            r_afcfta.instance,
            "implementation-reported",
            actor=self.person("cpaulse"),
            days_ago=26,
            notes="First quarterly implementation note received.",
        )
        self.grant_view(r_afcfta.instance, "trade")

        r_elections = resolution(
            sadc,
            number="R-2026-SADC-01",
            title="Deploy an election observation mission to the 2026 regional polls",
            responsible=self.groups["ir"],
            actor=self.person("nkayi"),
            days_ago=42,
            resolution_text=(
                "The Forum resolves to deploy a short-term observer mission and "
                "to publish its preliminary statement within 48 hours."
            ),
            implementation_progress="Mission budget and observer list under preparation.",
        )
        self.advance(
            r_elections,
            "Assigned",
            actor=self.person("nkayi"),
            days_ago=40,
            comment="Assigned to the IR section.",
        )
        self.advance(
            r_elections,
            "In Progress",
            actor=self.person("cawe"),
            days_ago=33,
            comment="Observer accreditation under way.",
        )

        r_sdgs = resolution(
            ipu,
            number="R-2026-IPU-01",
            title="Institutionalise SDG oversight in committee work programmes",
            responsible=self.groups["ir"],
            actor=self.person("cpaulse"),
            days_ago=118,
            resolution_text=(
                "Parliaments should map the Sustainable Development Goals onto "
                "committee work programmes and report annually."
            ),
            implementation_progress=(
                "The Committee has added an annual SDG oversight session to its "
                "work programme."
            ),
        )
        self.advance(
            r_sdgs,
            "Assigned",
            actor=self.person("sonjica"),
            days_ago=112,
            comment="Assigned to the Portfolio Committee on IR and Cooperation.",
        )
        self.advance(
            r_sdgs,
            "In Progress",
            actor=self.person("cpaulse"),
            days_ago=104,
            comment="Committee work programme amended.",
        )
        self.advance(
            r_sdgs,
            "Implemented",
            actor=self.person("cpaulse"),
            days_ago=70,
            comment="Annual oversight session scheduled; reported to the section.",
        )
        self.record_event(
            r_sdgs.instance,
            "implementation-reported",
            actor=self.person("cpaulse"),
            days_ago=70,
            notes="Implementation confirmed by the Committee Secretariat.",
        )

        r_diplomacy = resolution(
            ipu,
            number="R-2026-IPU-02",
            title="Establish a parliamentary diplomacy caucus",
            responsible=self.groups["ir"],
            actor=self.person("cpaulse"),
            days_ago=116,
            resolution_text=(
                "The Assembly encourages the establishment of an all-party "
                "parliamentary diplomacy caucus."
            ),
            implementation_progress="Caucus constituted and rules adopted.",
        )
        self.advance(
            r_diplomacy,
            "Assigned",
            actor=self.person("sonjica"),
            days_ago=110,
            comment="Assigned to the IR section.",
        )
        self.advance(
            r_diplomacy,
            "In Progress",
            actor=self.person("cpaulse"),
            days_ago=100,
            comment="Terms of reference drafted.",
        )
        self.advance(
            r_diplomacy,
            "Implemented",
            actor=self.person("cpaulse"),
            days_ago=70,
            comment="Caucus constituted.",
        )
        self.advance(
            r_diplomacy,
            "Closed",
            actor=self.person("sonjica"),
            days_ago=55,
            comment="Implementation confirmed and the resolution closed.",
        )
        self.record_event(
            r_diplomacy.instance,
            "implementation-reported",
            actor=self.person("cpaulse"),
            days_ago=70,
            notes="Caucus constituted at the parliamentary diplomacy seminar.",
        )

        r_commonwealth = resolution(
            cpc,
            number="R-2026-CPC-01",
            title="Adopt a common Commonwealth position on climate adaptation",
            responsible=self.groups["forestry"],
            actor=self.person("dlomo"),
            days_ago=58,
            resolution_text=(
                "Members resolve to coordinate a common position ahead of the "
                "next Conference of the Parties."
            ),
        )
        self.grant_view(r_commonwealth.instance, "forestry")

    def _seed_international_agreements(self):
        agreement_type = self.types["International Agreement"]
        states = self.states_for(agreement_type)

        # 1. Tabled and referred to Committee (the type's initial state).
        services = self.create(
            InternationalAgreement,
            actor=self.person("gelie"),
            created_days_ago=20,
            owner=self.person("gelie"),
            assigned_to=self.person("paulsen"),
            title="SADC Protocol on Trade in Services",
            description=(
                "Protocol liberalising trade in services across the SADC region, "
                "tabled in terms of section 231(2) of the Constitution."
            ),
            agreement_type=InternationalAgreement.SECTION_231_2,
            submitting_department="Department of Trade, Industry and Competition",
            responsible_minister_name="Minister of Trade, Industry and Competition",
            atc_tabling_date=self.date(22),
            atc_reference="ATC, 18 August, p. 5",
            priority="medium",
            deadline=self.now + timedelta(days=30),
            agreement_document_url=(
                "https://parliament.sharepoint.com/sites/PWMS/Shared%20Documents/"
                "SADC%20Trade%20in%20Services.pdf"
            ),
            explanatory_memorandum_url=(
                "https://parliament.sharepoint.com/sites/PWMS/Shared%20Documents/"
                "SADC%20Trade%20in%20Services%20-%20EM.pdf"
            ),
            notes="Referred to the Trade and Economic Development committees.",
        )
        self.set_m2m(
            services.instance,
            "referral_committees",
            [self.groups["trade"], self.groups["trade_select"]],
        )

        # 2. Under active committee consideration.
        extradition = self.create(
            InternationalAgreement,
            actor=self.person("cpaulse"),
            created_days_ago=55,
            owner=self.person("cpaulse"),
            assigned_to=self.person("nkayi"),
            title="Extradition Treaty between South Africa and the United Arab Emirates",
            description=(
                "Bilateral extradition treaty tabled for Parliament's approval in "
                "terms of section 231(2)."
            ),
            agreement_type=InternationalAgreement.SECTION_231_2,
            submitting_department="Department of Justice and Constitutional Development",
            responsible_minister_name="Minister of Justice and Correctional Services",
            atc_tabling_date=self.date(57),
            atc_reference="ATC, 12 July, p. 22",
            priority="high",
            deadline=self.now + timedelta(days=12),
            notes="Committee has invited the Department and the NPA to brief it.",
        )
        self.advance(
            extradition,
            "Committee considering and processing",
            actor=self.person("cpaulse"),
            days_ago=48,
            comment="Committee commenced its consideration of the agreement.",
        )
        self.set_m2m(
            extradition.instance,
            "referral_committees",
            [self.groups["justice"], self.groups["security_justice"]],
        )

        # 3. Committee has reported and tabled its recommendation.
        air = self.create(
            InternationalAgreement,
            actor=self.person("nkayi"),
            created_days_ago=70,
            owner=self.person("nkayi"),
            assigned_to=self.person("cawe"),
            title="Bilateral Air Services Agreement with the Republic of Kenya",
            description=(
                "Technical air services agreement concluded under section 231(3), "
                "tabled for information."
            ),
            agreement_type=InternationalAgreement.SECTION_231_3,
            submitting_department="Department of Transport",
            responsible_minister_name="Minister of Transport",
            atc_tabling_date=self.date(72),
            atc_reference="ATC, 26 June, p. 9",
            priority="medium",
            deadline=self.now + timedelta(days=7),
            notes="Committee report tabled in the House; adoption pending.",
        )
        self.advance(
            air,
            "Committee considering and processing",
            actor=self.person("nkayi"),
            days_ago=63,
            comment="Committee considered the agreement and the explanatory memorandum.",
        )
        self.advance(
            air,
            "Committee submitted report for tabling",
            actor=self.person("cawe"),
            days_ago=58,
            comment="Committee report adopted and submitted for tabling.",
        )
        self.set_m2m(air.instance, "referral_committees", [self.groups["transport"]])

        # 4. Adopted by the House and referred to the Department for implementation.
        paris = self.create(
            InternationalAgreement,
            actor=self.person("gelie"),
            created_days_ago=130,
            owner=self.person("gelie"),
            assigned_to=self.person("cpaulse"),
            title="Paris Agreement - Updated Nationally Determined Contribution",
            description=(
                "Updated Nationally Determined Contribution tabled in terms of "
                "section 231(2)."
            ),
            agreement_type=InternationalAgreement.SECTION_231_2,
            submitting_department="Department of Forestry, Fisheries and Environment",
            responsible_minister_name=(
                "Minister of Forestry, Fisheries and the Environment"
            ),
            atc_tabling_date=self.date(132),
            atc_reference="ATC, 26 April, p. 17",
            priority="high",
            deadline=self.now + timedelta(days=3),
            notes="House adopted the agreement; Department to report on implementation.",
        )
        self.advance(
            paris,
            "Committee considering and processing",
            actor=self.person("gelie"),
            days_ago=124,
            comment="Committee commenced consideration.",
        )
        self.advance(
            paris,
            "Committee submitted report for tabling",
            actor=self.person("cpaulse"),
            days_ago=118,
            comment="Committee recommended that the House approve the agreement.",
        )
        self.advance(
            paris,
            "House adopted – referred to Department",
            actor=self.person("sonjica"),
            days_ago=110,
            comment="House adopted the agreement on 28 May.",
        )
        self.set_m2m(
            paris.instance,
            "referral_committees",
            [self.groups["forestry"], self.groups["minerals"]],
        )

        # 5. Closed - fully processed and approved by the House.
        cyber = self.create(
            InternationalAgreement,
            actor=self.person("sonjica"),
            created_days_ago=150,
            owner=self.person("sonjica"),
            assigned_to=self.person("nkayi"),
            title="United Nations Convention against Cybercrime",
            description=(
                "Multilateral convention on international cooperation against "
                "cybercrime, tabled in terms of section 231(2)."
            ),
            agreement_type=InternationalAgreement.SECTION_231_2,
            submitting_department="Department of Justice and Constitutional Development",
            responsible_minister_name="Minister of Justice and Correctional Services",
            atc_tabling_date=self.date(152),
            atc_reference="ATC, 6 April, p. 3",
            priority="medium",
            deadline=self.now - timedelta(days=40),
            notes="Approved by both Houses; instrument of ratification deposited.",
        )
        self.advance(
            cyber,
            "Committee considering and processing",
            actor=self.person("sonjica"),
            days_ago=144,
            comment="Committee commenced consideration.",
        )
        self.advance(
            cyber,
            "Committee submitted report for tabling",
            actor=self.person("nkayi"),
            days_ago=138,
            comment="Committee recommended approval.",
        )
        self.advance(
            cyber,
            "House adopted – referred to Department",
            actor=self.person("sonjica"),
            days_ago=130,
            comment="House adopted the agreement.",
        )
        self.advance(
            cyber,
            "Closed – House approved",
            actor=self.person("sonjica"),
            days_ago=100,
            comment="Ratification instrument deposited; agreement closed.",
        )
        self.set_m2m(cyber.instance, "referral_committees", [self.groups["justice"]])
        self.attach(
            cyber.instance,
            name="UN Convention against Cybercrime.pdf",
            kind="agreement",
            actor=self.person("nkayi"),
            days_ago=152,
            size=1_120_640,
        )
        self.attach(
            cyber.instance,
            name="Explanatory Memorandum - Cybercrime Convention.pdf",
            kind="memorandum",
            actor=self.person("nkayi"),
            days_ago=152,
            size=486_912,
        )

        # 6. Submitted by the Department but not yet tabled (pre-initial state).
        mlat = self.create(
            InternationalAgreement,
            actor=self.person("jardine"),
            created_days_ago=8,
            owner=self.person("jardine"),
            assigned_to=self.person("gelie"),
            state=states[normalise_state_name("Submitted for tabling")],
            title="SADC Protocol on Mutual Legal Assistance in Criminal Matters",
            description=(
                "Protocol submitted by the Department ahead of tabling in the ATC."
            ),
            agreement_type=InternationalAgreement.SECTION_231_2,
            submitting_department="Department of Justice and Constitutional Development",
            responsible_minister_name="Minister of Justice and Correctional Services",
            priority="low",
            deadline=self.now + timedelta(days=45),
            notes="Awaiting a tabling date in the ATC.",
        )
        self.grant_view(mlat.instance, "justice")

    def _seed_bills(self):
        def bill(
            *,
            number,
            title,
            short_title,
            section,
            house,
            sponsor,
            committee,
            owner,
            assigned,
            actor,
            created_days_ago,
            steps,
            introduced_days_ago,
            **fields,
        ):
            record = self.create(
                Bill,
                actor=actor,
                created_days_ago=created_days_ago,
                owner=owner,
                assigned_to=assigned,
                title=title,
                bill_number=number,
                short_title=short_title,
                bill_type=section,
                house_of_origin=house,
                sponsor_name=sponsor,
                responsible_committee=committee,
                introduced_date=self.date(introduced_days_ago),
                **fields,
            )
            for to_state, days_ago, step_actor, comment in steps:
                self.advance(
                    record,
                    to_state,
                    actor=step_actor,
                    days_ago=days_ago,
                    comment=comment,
                )
            return record

        # 1. Introduced - awaiting referral to a committee.
        education = bill(
            number="B 12—2026",
            title="Basic Education Laws Amendment Bill",
            short_title="Basic Education Laws",
            section=Bill.SECTION_76,
            house=Bill.NA,
            sponsor="Minister of Basic Education",
            committee=self.groups["basic_education"],
            owner=self.person("mbatha"),
            assigned=self.person("cupido"),
            actor=self.person("mbatha"),
            created_days_ago=12,
            introduced_days_ago=12,
            steps=[],
            priority="high",
            deadline=self.now + timedelta(days=60),
            atc_reference="ATC, 26 August, p. 7",
            notes="Introduced by the Minister; referral to the Committee pending.",
        )
        self._bill_version(
            education,
            actor=self.person("cupido"),
            label="B 12—2026",
            version_type=BillVersion.INTRODUCED,
            days_ago=12,
            current=True,
            notes="Bill as introduced.",
        )

        # 2. Referred to the responsible committee.
        electricity = bill(
            number="B 15—2026",
            title="Electricity Regulation Amendment Bill",
            short_title="Electricity Regulation",
            section=Bill.SECTION_75,
            house=Bill.NA,
            sponsor="Minister of Electricity and Energy",
            committee=self.groups["electricity"],
            owner=self.person("cupido"),
            assigned=self.person("cele"),
            actor=self.person("cupido"),
            created_days_ago=30,
            introduced_days_ago=30,
            steps=[
                (
                    "Referred to Committee",
                    24,
                    self.person("cupido"),
                    "Referred to the Portfolio Committee on Electricity and Energy.",
                ),
            ],
            priority="medium",
            deadline=self.now + timedelta(days=45),
            notes="Committee briefings scheduled for next month.",
        )
        self._bill_version(
            electricity,
            actor=self.person("cupido"),
            label="B 15—2026",
            version_type=BillVersion.INTRODUCED,
            days_ago=30,
            current=True,
            notes="Bill as introduced.",
        )

        # 3. Open for public participation.
        procurement = bill(
            number="B 18—2026",
            title="Public Procurement Amendment Bill",
            short_title="Public Procurement",
            section=Bill.SECTION_76,
            house=Bill.NA,
            sponsor="Minister of Finance",
            committee=self.groups["trade"],
            owner=self.person("mbatha"),
            assigned=self.person("mcgivern"),
            actor=self.person("mbatha"),
            created_days_ago=48,
            introduced_days_ago=48,
            steps=[
                (
                    "Referred to Committee",
                    44,
                    self.person("mbatha"),
                    "Referred to the Portfolio Committee on Trade, Industry and Competition.",
                ),
                (
                    "Public Participation",
                    38,
                    self.person("mcgivern"),
                    "Public comment invited; notices published in the Government Gazette.",
                ),
            ],
            priority="high",
            deadline=self.now + timedelta(days=20),
            atc_reference="ATC, 22 July, p. 11",
            order_paper_reference="Order Paper No. 34",
            notes="Written submissions closed; public hearings in the week of 15 September.",
        )
        self._bill_version(
            procurement,
            actor=self.person("mbatha"),
            label="B 18—2026",
            version_type=BillVersion.INTRODUCED,
            days_ago=48,
            notes="Bill as introduced.",
        )
        self._bill_version(
            procurement,
            actor=self.person("mcgivern"),
            label="B 18—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            days_ago=36,
            current=True,
            notes="Clause 12 amended after the first round of briefings.",
        )
        self.attach(
            procurement.instance,
            name="Public Procurement Amendment Bill - Submissions Summary.pdf",
            kind="submission",
            actor=self.person("mcgivern"),
            days_ago=34,
            size=742_400,
        )

        # 4. Committee deliberation.
        climate = bill(
            number="B 21—2026",
            title="Climate Change Amendment Bill",
            short_title="Climate Change",
            section=Bill.SECTION_76,
            house=Bill.NA,
            sponsor="Minister of Forestry, Fisheries and the Environment",
            committee=self.groups["forestry"],
            owner=self.person("loots"),
            assigned=self.person("ebrahim"),
            actor=self.person("loots"),
            created_days_ago=65,
            introduced_days_ago=65,
            steps=[
                (
                    "Referred to Committee",
                    61,
                    self.person("loots"),
                    "Referred to the committee.",
                ),
                (
                    "Public Participation",
                    55,
                    self.person("ebrahim"),
                    "Public comment period opened.",
                ),
                (
                    "Committee Deliberation",
                    49,
                    self.person("ebrahim"),
                    "Committee began clause-by-clause deliberation.",
                ),
            ],
            priority="high",
            deadline=self.now + timedelta(days=35),
            notes="Committee awaiting a legal opinion on clause 8.",
        )
        self._bill_version(
            climate,
            actor=self.person("loots"),
            label="B 21—2026",
            version_type=BillVersion.INTRODUCED,
            days_ago=65,
            notes="Bill as introduced.",
        )

        # 5. Committee report adopted.
        hate_speech = bill(
            number="B 24—2026",
            title="Hate Speech and Hate Crimes Bill",
            short_title="Hate Speech and Hate Crimes",
            section=Bill.SECTION_76,
            house=Bill.NA,
            sponsor="Minister of Justice and Correctional Services",
            committee=self.groups["justice"],
            owner=self.person("arnold"),
            assigned=self.person("kassan"),
            actor=self.person("arnold"),
            created_days_ago=85,
            introduced_days_ago=85,
            steps=[
                (
                    "Referred to Committee",
                    80,
                    self.person("arnold"),
                    "Referred to the committee.",
                ),
                (
                    "Public Participation",
                    74,
                    self.person("kassan"),
                    "Public comment invited.",
                ),
                (
                    "Committee Deliberation",
                    66,
                    self.person("kassan"),
                    "Deliberation on the Bill and the submissions received.",
                ),
                (
                    "Committee Report",
                    58,
                    self.person("kassan"),
                    "Committee adopted its report recommending that the House pass the Bill.",
                ),
            ],
            priority="medium",
            deadline=self.now + timedelta(days=15),
            atc_reference="ATC, 3 June, p. 19",
            notes="Committee report tabled; House debate to be scheduled.",
        )
        self._bill_version(
            hate_speech,
            actor=self.person("arnold"),
            label="B 24—2026",
            version_type=BillVersion.INTRODUCED,
            days_ago=85,
            notes="Bill as introduced.",
        )
        self._bill_version(
            hate_speech,
            actor=self.person("kassan"),
            label="B 24—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            days_ago=60,
            current=True,
            notes="Definition of hate speech narrowed following submissions.",
        )
        self._bill_version(
            hate_speech,
            actor=self.person("kassan"),
            label="B 24—2026 (1st amendment schedule)",
            version_type=BillVersion.AMENDMENT_SCHEDULE,
            days_ago=60,
            notes="Schedule of amendments adopted by the Committee.",
        )

        # 6. Before the House for debate and voting.
        land_court = bill(
            number="B 27—2026",
            title="Land Court Bill",
            short_title="Land Court",
            section=Bill.SECTION_76,
            house=Bill.NA,
            sponsor="Minister of Justice and Correctional Services",
            committee=self.groups["justice"],
            owner=self.person("isaac"),
            assigned=self.person("jenkins"),
            actor=self.person("isaac"),
            created_days_ago=110,
            introduced_days_ago=110,
            steps=[
                (
                    "Referred to Committee",
                    104,
                    self.person("isaac"),
                    "Referred to the committee.",
                ),
                (
                    "Public Participation",
                    96,
                    self.person("jenkins"),
                    "Public comment invited.",
                ),
                (
                    "Committee Deliberation",
                    90,
                    self.person("jenkins"),
                    "Clause-by-clause deliberation completed.",
                ),
                (
                    "Committee Report",
                    84,
                    self.person("jenkins"),
                    "Committee report adopted.",
                ),
                (
                    "House Debate and Voting",
                    80,
                    self.person("isaac"),
                    "Tabled for debate and voting in the National Assembly.",
                ),
            ],
            priority="medium",
            deadline=self.now + timedelta(days=8),
            order_paper_reference="Order Paper No. 41",
            notes="Debate scheduled; the Committee's report is the House's working document.",
        )
        self._bill_version(
            land_court,
            actor=self.person("isaac"),
            label="B 27—2026",
            version_type=BillVersion.INTRODUCED,
            days_ago=110,
            notes="Bill as introduced.",
        )
        self._bill_version(
            land_court,
            actor=self.person("jenkins"),
            label="B 27—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            days_ago=86,
            current=True,
            notes="Amendments agreed during deliberation.",
        )

        # 7. Before the National Council of Provinces.
        municipal = bill(
            number="B 30—2026",
            title="Municipal Fiscal Powers and Functions Amendment Bill",
            short_title="Municipal Fiscal Powers",
            section=Bill.SECTION_76,
            house=Bill.NA,
            sponsor="Minister of Cooperative Governance and Traditional Affairs",
            committee=self.groups["cogta"],
            owner=self.person("gangen"),
            assigned=self.person("starkey"),
            actor=self.person("gangen"),
            created_days_ago=140,
            introduced_days_ago=140,
            steps=[
                (
                    "Referred to Committee",
                    134,
                    self.person("gangen"),
                    "Referred to the committee.",
                ),
                (
                    "Public Participation",
                    126,
                    self.person("starkey"),
                    "Public comment invited.",
                ),
                (
                    "Committee Deliberation",
                    120,
                    self.person("starkey"),
                    "Deliberation completed.",
                ),
                (
                    "Committee Report",
                    112,
                    self.person("starkey"),
                    "Committee report adopted.",
                ),
                (
                    "House Debate and Voting",
                    106,
                    self.person("gangen"),
                    "House passed the Bill and referred it to the NCOP.",
                ),
                (
                    "NCOP Consideration",
                    100,
                    self.person("gangen"),
                    "Referred to the National Council of Provinces for concurrence.",
                ),
            ],
            priority="high",
            deadline=self.now + timedelta(days=18),
            atc_reference="ATC, 8 May, p. 6",
            notes="NCOP select committee briefings under way.",
        )
        self._bill_version(
            municipal,
            actor=self.person("gangen"),
            label="B 30—2026",
            version_type=BillVersion.INTRODUCED,
            days_ago=140,
            notes="Bill as introduced.",
        )
        self._bill_version(
            municipal,
            actor=self.person("starkey"),
            label="B 30—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            days_ago=118,
            current=True,
            notes="Amendments agreed by the Committee.",
        )

        # 8. Signed into law - the full lifecycle, with preserved versions.
        leadership = bill(
            number="B 33—2026",
            title="Traditional and Khoi-San Leadership Amendment Bill",
            short_title="Traditional and Khoi-San Leadership",
            section=Bill.SECTION_76,
            house=Bill.NCOP,
            sponsor="Minister of Cooperative Governance and Traditional Affairs",
            committee=self.groups["cogta"],
            owner=self.person("mbatha"),
            assigned=self.person("cupido"),
            actor=self.person("mbatha"),
            created_days_ago=200,
            introduced_days_ago=200,
            steps=[
                (
                    "Referred to Committee",
                    194,
                    self.person("mbatha"),
                    "Referred to the committee.",
                ),
                (
                    "Public Participation",
                    186,
                    self.person("cupido"),
                    "Public comment invited.",
                ),
                (
                    "Committee Deliberation",
                    178,
                    self.person("cupido"),
                    "Deliberation completed.",
                ),
                (
                    "Committee Report",
                    170,
                    self.person("cupido"),
                    "Committee report adopted.",
                ),
                (
                    "House Debate and Voting",
                    162,
                    self.person("mbatha"),
                    "House passed the Bill.",
                ),
                (
                    "NCOP Consideration",
                    150,
                    self.person("mbatha"),
                    "NCOP concurred with the Bill as passed.",
                ),
                (
                    "Awaiting Presidential Assent",
                    120,
                    self.person("mbatha"),
                    "Referred to the President for assent.",
                ),
                (
                    "Signed into Law",
                    95,
                    self.person("mbatha"),
                    "Signed by the President; commencement date to be proclaimed.",
                ),
            ],
            priority="medium",
            deadline=self.now - timedelta(days=90),
            atc_reference="ATC, 20 February, p. 2",
            bill_document_url=(
                "https://parliament.sharepoint.com/sites/PWMS/Shared%20Documents/"
                "Traditional%20and%20Khoi-San%20Leadership%20-%20as%20signed.pdf"
            ),
            notes="Signed into law; the Department will publish the commencement date.",
        )
        self._bill_version(
            leadership,
            actor=self.person("cupido"),
            label="B 33—2026",
            version_type=BillVersion.INTRODUCED,
            days_ago=200,
            notes="Bill as introduced.",
        )
        self._bill_version(
            leadership,
            actor=self.person("cupido"),
            label="B 33—2026 (1st amendment schedule)",
            version_type=BillVersion.AMENDMENT_SCHEDULE,
            days_ago=176,
            notes="Schedule of amendments proposed by the Committee.",
        )
        self._bill_version(
            leadership,
            actor=self.person("cupido"),
            label="B 33—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            days_ago=174,
            current=True,
            notes="Amended Bill as adopted by the House.",
        )
        self._bill_version(
            leadership,
            actor=self.person("mbatha"),
            label="B 33—2026 (2nd amendment)",
            version_type=BillVersion.AMENDED,
            days_ago=152,
            notes="NCOP amendments incorporated.",
        )
        self.attach(
            leadership.instance,
            name="Traditional and Khoi-San Leadership - as signed.pdf",
            kind="bill",
            actor=self.person("mbatha"),
            days_ago=95,
            size=3_145_728,
            versions=[
                {"days_ago": 200, "size": 2_910_112},
                {"days_ago": 174, "size": 3_012_664},
                {"days_ago": 95},
            ],
        )

        # 9. Withdrawn before the House voted on it.
        nhi = bill(
            number="B 36—2026",
            title="National Health Insurance Amendment Bill",
            short_title="National Health Insurance",
            section=Bill.SECTION_76,
            house=Bill.NA,
            sponsor="Minister of Health",
            committee=self.groups["health"],
            owner=self.person("loots"),
            assigned=self.person("ebrahim"),
            actor=self.person("loots"),
            created_days_ago=90,
            introduced_days_ago=90,
            steps=[
                (
                    "Referred to Committee",
                    84,
                    self.person("loots"),
                    "Referred to the committee.",
                ),
                (
                    "Withdrawn",
                    70,
                    self.person("loots"),
                    "Withdrawn by the Minister to allow further consultation.",
                ),
            ],
            priority="low",
            deadline=self.now - timedelta(days=30),
            notes="Withdrawn on 15 July pending further consultation with stakeholders.",
        )
        self._bill_version(
            nhi,
            actor=self.person("loots"),
            label="B 36—2026",
            version_type=BillVersion.INTRODUCED,
            days_ago=90,
            current=True,
            notes="Bill as introduced.",
        )

    def _bill_version(
        self, record, *, actor, label, version_type, days_ago, notes="", current=False
    ):
        version = BillVersion.objects.create(
            bill=record.instance,
            version_label=label,
            version_type=version_type,
            version_date=self.date(days_ago),
            is_current=current,
            notes=notes,
            recorded_by=actor,
        )
        when = self.moment(days_ago)
        BillVersion.objects.filter(pk=version.pk).update(
            created_at=when, updated_at=when
        )
        return version

    def record_update(
        self,
        report,
        *,
        actor,
        days_ago,
        resulting_state,
        atc_reference="",
        atc_publication_date=None,
        atc_page_number="",
        atc_document_url="",
        notes="",
    ):
        """Append a BR03 update, then move its (and its event's) clock back."""
        update = report.record_update(
            update_date=self.date(days_ago),
            resulting_state=resulting_state,
            atc_reference=atc_reference,
            atc_publication_date=atc_publication_date,
            atc_page_number=atc_page_number,
            atc_document_url=atc_document_url,
            notes=notes,
            recorded_by=actor,
        )
        when = self.moment(days_ago)
        DelegationReportUpdate.objects.filter(pk=update.pk).update(
            created_at=when, updated_at=when
        )
        # Creating the update emitted the ATC event the close transition needs.
        WorkflowEvent.objects.filter(
            content_type=ContentType.objects.get_for_model(report),
            object_id=report.pk,
            event_type=self.events.get("atc-update-published"),
        ).order_by("-pk").update(occurred_at=when)
        return update

    # -- geography lookups --------------------------------------------------
    def _country(self, name):

        return Country.objects.filter(name__iexact=name).first()

    def _city(self, name, country_name):
        """The largest city matching ``name`` within a country (city names repeat)."""
        return (
            City.objects.filter(country__name=country_name, name__icontains=name)
            .order_by("-population")
            .first()
        )

    # -- alerts -------------------------------------------------------------
    def _seed_notifications(self):
        """Write the in-app alerts the bell and the Alerts page read.

        The automatic fan-out is silenced while seeding, so a curated set is
        written here: one "moved state" alert per instance that has moved, one
        "created" alert for the rest, plus a handful addressed to the two
        accounts a walkthrough is most likely to sign in with.
        """
        transition_kind = "workflow-transition"
        created_kind = "workflow-created"

        for record in self.seeded:
            instance = record.instance
            actor = instance.owner
            if record.steps:
                when = record.steps[-1]
                subject = f"{instance.title}: moved to {instance.current_state.name}"
                message = (
                    f"'{instance.title}' moved to '{instance.current_state.name}'.\n\n"
                    f"Reference: {instance.identifier or '-'}"
                )
                kind = transition_kind
            else:
                when = record.created_at
                subject = f"New {instance.workflow_type.name}: {instance.title}"
                message = (
                    f"A new {instance.workflow_type.name} has been created and is "
                    f"waiting in the '{instance.current_state.name}' state.\n\n"
                    f"Reference: {instance.identifier or '-'}"
                )
                kind = created_kind
            self.notify(
                instance.owner, instance, kind, subject, message, when, actor=actor
            )

        # Alerts for the accounts a demo is most likely to use.
        demo_followers = [self.person("gelie"), self.person("admin")]
        for index, record in enumerate(
            [r for r in self.seeded if r.model is not Bill][:6]
        ):
            for follower in demo_followers:
                instance = record.instance
                if instance.owner_id == follower.pk:
                    continue
                when = record.steps[-1] if record.steps else record.created_at
                self.notify(
                    follower,
                    instance,
                    transition_kind if record.steps else created_kind,
                    f"{instance.workflow_type.name}: {instance.title}",
                    (
                        f"'{instance.title}' is in '{instance.current_state.name}' "
                        f"and needs attention.\n\nReference: {instance.identifier or '-'}"
                    ),
                    when,
                    actor=instance.owner,
                    read=index % 2 == 1,
                )

    def notify(
        self,
        recipient,
        instance,
        kind,
        subject,
        message,
        when,
        *,
        actor=None,
        read=False,
    ):
        if recipient is None:
            return None
        row = Notification.objects.create(
            recipient=recipient,
            channel="in_app",
            kind=kind,
            subject=subject[:255],
            body=message,
            url=instance.get_absolute_url(),
            actor=actor if (actor and actor.pk != recipient.pk) else None,
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
            context={
                "state": instance.current_state.name,
                "reference": instance.identifier,
            },
            status="sent",
            sent_at=when,
        )
        Notification.objects.filter(pk=row.pk).update(
            created_at=when,
            updated_at=when,
            sent_at=when,
            read_at=when if read else None,
        )
        return row

    # -- timeline repair ----------------------------------------------------
    def _backdate(self):
        """Move every "now" timestamp onto the story's intended dates.

        The models stamp transitions, audit entries and referral events with the
        wall clock, so the whole demo would otherwise read as one batch. Per
        instance, the audit entries are aligned to the create time and the
        transitions (in order); ``TransitionLog`` rows are aligned the same way.
        """
        for record in self.seeded:
            content_type = ContentType.objects.get_for_model(record.model)
            last_moment = record.steps[-1] if record.steps else record.created_at

            record.model.objects.filter(pk=record.instance.pk).update(
                created_at=record.created_at, updated_at=last_moment
            )

            logs = list(
                TransitionLog.objects.filter(
                    content_type=content_type, object_id=record.instance.pk
                ).order_by("id")
            )
            for log, when in zip(logs, record.steps):
                TransitionLog.objects.filter(pk=log.pk).update(timestamp=when)

            entries = list(
                LogEntry.objects.filter(
                    content_type=content_type, object_id=record.instance.pk
                ).order_by("id")
            )
            step_times = iter(record.steps)
            for entry in entries:
                when = (
                    record.created_at
                    if entry.action == 0
                    else next(step_times, last_moment)
                )
                LogEntry.objects.filter(pk=entry.pk).update(timestamp=when)

    # -- summary ------------------------------------------------------------
    def _report(self):
        counts = {
            "Delegation reports": DelegationReport.objects.count(),
            "International resolutions": InternationalResolution.objects.count(),
            "International agreements": InternationalAgreement.objects.count(),
            "Bills": Bill.objects.count(),
            "Transitions": TransitionLog.objects.count(),
            "Events": WorkflowEvent.objects.count(),
            "Referrals": WorkflowReferral.objects.count(),
            "Alerts": Notification.objects.count(),
            "Documents": Attachment.objects.count(),
        }
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo data seeded."))
        for label, count in counts.items():
            self.stdout.write(f"   {label}: {count}")
