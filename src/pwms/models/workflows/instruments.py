"""Concrete legislative instruments built on the workflow base."""

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ..base import BaseModel
from .base import AbstractLegislativeWorkflow
from .events import EventType


class DelegationReport(AbstractLegislativeWorkflow):
    """
    A delegation's report on an international engagement/forum and the
    resolutions concluded there (BRS BR02/BR03).

    System behaviour:

    * a unique ``reference_number`` is generated on creation (BR02.6);
    * newly created reports start in the seeded *Awaiting PGIR approval*
      state, the BRS's automatic initial status (BR02.7);
    * each report contains many :class:`InternationalResolution` rows through
      :class:`WorkflowRelationship` (see ``add_sub_workflow()``);
    * delegation members and support officials are captured as
      :class:`DelegationParticipant` rows (BR02.3.7/8);
    * the implementation due date (BR02.3.12) is the inherited ``deadline``.

    Documents (BR02.3.14, BR03.5.5, BR11) are held in SharePoint; this model
    keeps the report document link, while BR03 updates (ATC reference, date,
    page, document) are stored per update in :class:`DelegationReportUpdate` and
    exposed as read-only ``atc_*`` properties holding the latest values.
    """

    reference_number = models.CharField(
        max_length=30,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        help_text="System-generated unique reference number.",
    )
    detail_url_name = "delegation_report_detail"
    engagement_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Name of the international engagement / forum.",
    )
    engagement_start_date = models.DateField(
        null=True,
        blank=True,
        help_text="Engagement start date; may not be a future date.",
    )
    engagement_end_date = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Engagement end date; may not be before the start date or in the future."
        ),
    )
    location_city = models.ForeignKey(
        "City",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegation_reports",
        help_text="Engagement location – city.",
    )
    location_country = models.ForeignKey(
        "Country",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegation_reports",
        help_text="Engagement location – country.",
    )
    notes = models.TextField(
        blank=True,
        help_text=(
            "Additional notes / follow-up action by Presiding Officer(s) or "
            "Parliamentarian(s)."
        ),
    )

    # BR02.3.14 / BR03.5.5 / BR11: documents live in SharePoint.
    report_document_url = models.URLField(
        max_length=2048,
        blank=True,
        help_text="SharePoint link to the delegation report document.",
    )

    class Meta:
        verbose_name = "Delegation Report"
        verbose_name_plural = "Delegation Reports"

    def __str__(self):
        label = self.reference_number or "unsaved"
        return f"{label} – {self.title}" if self.title else str(label)

    # -- BRS validation -----------------------------------------------------
    def clean(self):
        super().clean()
        today = timezone.localdate()
        errors = {}
        if self.engagement_start_date and self.engagement_start_date > today:
            errors["engagement_start_date"] = (
                "The engagement start date may not be a future date."
            )
        if self.engagement_end_date:
            if self.engagement_end_date > today:
                errors["engagement_end_date"] = (
                    "The engagement end date may not be a future date."
                )
            elif (
                self.engagement_start_date
                and self.engagement_end_date < self.engagement_start_date
            ):
                errors["engagement_end_date"] = (
                    "The engagement end date may not be before the start date."
                )
        if errors:
            raise ValidationError(errors)

    # -- system-generated reference number (BR02.6) -------------------------
    def save(self, *args, **kwargs):
        if self._state.adding and not self.reference_number:
            self.reference_number = self._next_reference_number()
        super().save(*args, **kwargs)

    @classmethod
    def _next_reference_number(cls):
        """
        ``DR-<year>-<sequence>``, sequential per calendar year.

        The unique constraint backstops the small race window between the
        lookup and the insert.
        """
        year = timezone.now().year
        prefix = f"DR-{year}-"
        last = (
            cls.objects.filter(reference_number__startswith=prefix)
            .order_by("-reference_number")
            .values_list("reference_number", flat=True)
            .first()
        )
        try:
            sequence = int(last.rsplit("-", 1)[1]) + 1
        except AttributeError, IndexError, ValueError:
            sequence = 1
        return f"{prefix}{sequence:04d}"

    # -- BR03 update history ------------------------------------------------
    def record_update(
        self,
        *,
        update_date=None,
        resulting_state=None,
        atc_reference="",
        atc_publication_date=None,
        atc_page_number="",
        atc_document_url="",
        notes="",
        recorded_by=None,
    ):
        """
        Append a :class:`DelegationReportUpdate` (BR03) and return it.

        ATC publication details make the update emit an
        ``atc-update-published`` event, unlocking the seeded close guard.
        """
        return DelegationReportUpdate.objects.create(
            delegation_report=self,
            update_date=update_date or timezone.localdate(),
            resulting_state=resulting_state,
            atc_reference=atc_reference,
            atc_publication_date=atc_publication_date,
            atc_page_number=atc_page_number,
            atc_document_url=atc_document_url,
            notes=notes,
            recorded_by=recorded_by,
        )

    @property
    def latest_update(self):
        """Most recent BR03 update, or ``None``."""
        return self.updates.order_by("-update_date", "-id").first()

    # BR03 ATC detail is the read-only "latest" view of the update history;
    # the full history lives in DelegationReportUpdate rows.
    @property
    def atc_reference(self):
        update = self.latest_update
        return update.atc_reference if update else ""

    @property
    def atc_publication_date(self):
        update = self.latest_update
        return update.atc_publication_date if update else None

    @property
    def atc_page_number(self):
        update = self.latest_update
        return update.atc_page_number if update else ""

    @property
    def atc_document_url(self):
        update = self.latest_update
        return update.atc_document_url if update else ""


class DelegationParticipant(BaseModel):
    """
    A parliamentary delegation member or support official (BR02.3.7/8).

    Captures name, surname, title and delegation role per person, with an
    optional link to a :class:`User` when the person has a system account.
    """

    MEMBER = "member"
    OFFICIAL = "official"
    PARTICIPANT_TYPE_CHOICES = [
        (MEMBER, _("Parliamentary Delegation Member")),
        (OFFICIAL, _("Support Official")),
    ]

    delegation_report = models.ForeignKey(
        DelegationReport,
        on_delete=models.CASCADE,
        related_name="participants",
        help_text="Delegation report this person is attached to.",
    )
    participant_type = models.CharField(
        max_length=20,
        choices=PARTICIPANT_TYPE_CHOICES,
        default=MEMBER,
        help_text="Parliamentary delegation member or support official.",
    )
    title = models.CharField(
        max_length=50,
        blank=True,
        help_text="Honorific / job title, e.g. Hon., Dr, Prof.",
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    delegation_role = models.CharField(
        max_length=100,
        blank=True,
        help_text="Role in the delegation, e.g. Leader of the Delegation, MP, Secretary.",
    )
    user = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegation_participations",
        help_text="Optional link to the person's system account.",
    )
    order = models.IntegerField(
        default=0, help_text="Display order within the delegation."
    )

    # Taking someone off the delegation keeps the row: the report then still shows
    # who was on it, who took them off and when. A removed row is history, so the
    # unique constraint below only covers a person's current stint.
    removed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the person was taken off the delegation.",
    )
    removed_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegation_participants_removed",
        help_text="Who took them off the delegation.",
    )

    class Meta:
        ordering = ["participant_type", "order", "last_name", "first_name"]
        indexes = [
            models.Index(fields=["delegation_report", "participant_type"]),
        ]
        constraints = [
            # One person appears at most once on a delegation *at a time*: a row
            # taken off the delegation is kept as history and does not stop them
            # being added again. ``user`` is optional — an official may have no
            # account — so rows without one are never compared at all.
            models.UniqueConstraint(
                fields=["delegation_report", "user"],
                condition=models.Q(user__isnull=False, removed_at__isnull=True),
                name="workflows_participant_report_user_uniq",
            ),
        ]

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return f"{name} ({self.get_participant_type_display()})"

    @property
    def full_name(self):
        return " ".join(
            part for part in (self.title, self.first_name, self.last_name) if part
        )

    @property
    def is_removed(self):
        """Whether the person has been taken off the delegation."""
        return self.removed_at is not None

    def remove(self, *, by=None):
        """
        Take the person off the delegation, keeping the row for the record.

        Soft delete: every list of the delegation's people filters on
        ``removed_at``, so the row survives as the record of who was on the
        delegation, who took them off and when.
        """
        if self.is_removed:
            return self
        self.removed_at = timezone.now()
        self.removed_by = by
        self.save(update_fields=["removed_at", "removed_by", "updated_at"])
        return self


class DelegationReportUpdate(BaseModel):
    """
    A recorded update on a delegation report (BRS BR03).

    The BRS "delegation report update type" is the report status the update
    moved the report to (``resulting_state``); the ATC publication details are
    the supporting evidence (BR03.5.3/5).

    Creating a row that carries ATC publication details emits an
    ``atc-update-published`` :class:`WorkflowEvent` — the evidence the seeded
    *Close – House approved* transition guard requires. Rows form the report's
    BR03 update history (the previous single-set ``atc_*`` fields are exposed
    as read-only "latest" properties on :class:`DelegationReport`).
    """

    delegation_report = models.ForeignKey(
        DelegationReport,
        on_delete=models.CASCADE,
        related_name="updates",
        help_text="Report this update belongs to.",
    )
    update_date = models.DateField(
        default=timezone.localdate, help_text="Date of the update."
    )
    resulting_state = models.ForeignKey(
        "State",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="delegation_report_updates",
        help_text="Report status (update type) after this update.",
    )
    atc_reference = models.CharField(
        max_length=255, blank=True, help_text="ATC reference."
    )
    atc_publication_date = models.DateField(
        null=True, blank=True, help_text="ATC publication date."
    )
    atc_page_number = models.CharField(
        max_length=50, blank=True, help_text="ATC page number."
    )
    atc_document_url = models.URLField(
        max_length=2048,
        blank=True,
        help_text="SharePoint link to the ATC / update document.",
    )
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegation_report_updates",
    )

    class Meta:
        ordering = ["-update_date", "-id"]
        indexes = [
            models.Index(fields=["delegation_report", "-update_date"]),
        ]

    def __str__(self):
        return f"{self.delegation_report} — update {self.update_date}"

    @property
    def has_atc_details(self):
        """True when the update carries ATC publication evidence (BR03.5.3)."""
        return bool(
            self.atc_reference
            or self.atc_publication_date
            or self.atc_page_number
            or self.atc_document_url
        )

    def save(self, *args, **kwargs):
        created = self._state.adding
        super().save(*args, **kwargs)
        if not created or not self.has_atc_details:
            return
        event_type = EventType.objects.filter(slug="atc-update-published").first()
        if event_type is None:  # registry entry removed — skip silently
            return
        self.delegation_report.record_event(
            event_type,
            actor=self.recorded_by,
            payload={
                "atc_reference": self.atc_reference,
                "atc_page_number": self.atc_page_number,
                "update_date": str(self.update_date),
            },
            document_url=self.atc_document_url,
            notes=self.notes,
        )


class InternationalResolution(AbstractLegislativeWorkflow):
    """
    A resolution adopted at an international engagement (BRS BR02.3.9).

    Resolutions are captured from the recommendations of a delegation report and
    attached to it through :class:`WorkflowRelationship`. Their lifecycle is
    seeded as *Captured → Assigned → In Progress → Implemented → Closed* and
    implementation progress is recorded in ``implementation_progress``.
    """

    detail_url_name = "international_resolution_detail"

    resolution_number = models.CharField(max_length=50, unique=True)
    resolution_text = models.TextField(
        blank=True,
        help_text=(
            "Resolution as concluded, captured from the delegation report "
            "recommendations."
        ),
    )
    adoption_date = models.DateField(null=True, blank=True)
    responsible_group = models.ForeignKey(
        "Group",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolutions_responsible",
        help_text="Committee / group responsible for implementation.",
    )
    implementation_progress = models.TextField(
        blank=True,
        help_text="Latest narrative on Parliament's implementation of the resolution.",
    )

    class Meta:
        verbose_name = "International Resolution"
        verbose_name_plural = "International Resolutions"

    def __str__(self):
        return f"Resolution {self.resolution_number} - {self.title}"


class InternationalAgreement(AbstractLegislativeWorkflow):
    """
    A government international agreement tabled in Parliament and tracked
    through referral, committee consideration and House adoption (BRS
    *International Agreements Tracking and Monitoring*, BR02/BR03).

    System behaviour:

    * a unique ``reference_number`` is generated on creation (BR02);
    * newly created agreements start in the seeded *Agreement Tabled –
      referred to Committee* state, the BRS's automatic initial status (BR02);
    * the agreement type is Section 231(2) or Section 231(3) (BR02);
    * referral committees are captured as a many-to-many link to
      :class:`Group` (BR02);
    * the implementation due date (BR02) is the inherited ``deadline``, and the
      BR12 overdue identifier is exposed by ``is_overdue`` /
      ``overdue_identifier``.

    Documents (BR02, BR04, BR11) are held in SharePoint; this model keeps the
    agreement and explanatory-memorandum links.
    """

    detail_url_name = "international_agreement_detail"

    SECTION_231_2 = "section-231-2"
    SECTION_231_3 = "section-231-3"
    AGREEMENT_TYPE_CHOICES = [
        (SECTION_231_2, _("Section 231(2) agreement")),
        (SECTION_231_3, _("Section 231(3) agreement")),
    ]

    reference_number = models.CharField(
        max_length=30,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        help_text="System-generated unique reference number.",
    )
    agreement_type = models.CharField(
        max_length=20,
        choices=AGREEMENT_TYPE_CHOICES,
        blank=True,
        help_text="Constitutional basis of the agreement.",
    )
    submitting_department = models.CharField(
        max_length=255,
        blank=True,
        help_text="Government department that submitted the agreement.",
    )
    responsible_minister = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="responsible_agreements",
        help_text=(
            "Responsible Member of the Executive (Minister) submitting the agreement."
        ),
    )
    responsible_minister_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=(
            "Minister as recorded on the tabled document - for a former "
            "office holder or one without a PWMS account."
        ),
    )
    atc_tabling_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date the agreement was tabled in the ATC.",
    )
    atc_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text=("Reference details of the ATC and any other relevant documents."),
    )
    referral_committees = models.ManyToManyField(
        "Group",
        blank=True,
        related_name="international_agreements_referred",
        help_text="Committee(s) to which the agreement is referred.",
    )
    notes = models.TextField(
        blank=True,
        help_text=(
            "Additional notes / follow-up action by Presiding Officer(s) or "
            "Parliamentarian(s)."
        ),
    )
    agreement_document_url = models.URLField(
        max_length=2048,
        blank=True,
        help_text="SharePoint link to the uploaded agreement document.",
    )
    explanatory_memorandum_url = models.URLField(
        max_length=2048,
        blank=True,
        help_text="SharePoint link to the explanatory memorandum.",
    )

    class Meta:
        verbose_name = "International Agreement"
        verbose_name_plural = "International Agreements"

    def __str__(self):
        label = self.reference_number or "unsaved"
        return f"{label} – {self.title}" if self.title else str(label)

    # -- responsible minister (BR02) ----------------------------------------
    @property
    def responsible_minister_display(self):
        """The linked minister, or the name recorded on the tabled document."""
        return self.responsible_minister or self.responsible_minister_name

    def clean(self):
        """
        Only an executive office holder may be linked as responsible minister.

        Checked on ``full_clean()``, so every form-driven entry point (web form,
        admin) and any explicit validation is covered. An office holder who has
        since left the post stays valid: the agreement already on file named the
        minister who served at the time, and a reshuffle must not invalidate it.
        """
        super().clean()
        if self.responsible_minister_id is None:
            return
        if self.responsible_minister.executive_memberships().exists():
            return
        raise ValidationError(
            {
                "responsible_minister": (
                    f"{self.responsible_minister} holds no executive appointment: "
                    "link a Minister or Deputy Minister, or record the name in "
                    "the responsible-minister name field instead."
                )
            }
        )

    # -- system-generated reference number (BR02) ---------------------------
    def save(self, *args, **kwargs):
        if self._state.adding and not self.reference_number:
            self.reference_number = self._next_reference_number()
        super().save(*args, **kwargs)

    @classmethod
    def _next_reference_number(cls):
        """
        ``IA-<year>-<sequence>``, sequential per calendar year.

        The unique constraint backstops the small race window between the
        lookup and the insert.
        """
        year = timezone.now().year
        prefix = f"IA-{year}-"
        last = (
            cls.objects.filter(reference_number__startswith=prefix)
            .order_by("-reference_number")
            .values_list("reference_number", flat=True)
            .first()
        )
        try:
            sequence = int(last.rsplit("-", 1)[1]) + 1
        except AttributeError, IndexError, ValueError:
            sequence = 1
        return f"{prefix}{sequence:04d}"


class Bill(AbstractLegislativeWorkflow):
    """
    A Parliamentary bill tracked through its legislative lifecycle (BRS
    *Online Bill Tracking*).

    The BRS separates detailed internal procedural tracking from a simplified
    public view: every internal state maps to one of the high-level public
    statuses through ``State.public_status`` (BRS §12), so the same record can
    be published at ``public_status`` without exposing internal granularity.

    System behaviour:

    * ``bill_number`` is Parliament's B-number (unique), including the version
      suffix where one applies (BRS §13.1);
    * the bill profile carries the long and short title, bill type (s74–s77),
      House of introduction, sponsor or originating authority, responsible
      committee, latest version and associated documents (BRS §7A);
    * the ATC and order-paper references are recorded per bill (BRS §7B);
    * ``notes`` captures sub-events under a main stage, so exceptional or
      explanatory steps need not complicate the public lifecycle (BRS §15A).

    Documents live in SharePoint; this model keeps the bill document link.
    """

    detail_url_name = "bill_detail"

    SECTION_74 = "section-74"
    SECTION_75 = "section-75"
    SECTION_76 = "section-76"
    SECTION_77 = "section-77"
    BILL_TYPE_CHOICES = [
        (SECTION_74, _("Section 74 bill (constitutional amendment)")),
        (SECTION_75, _("Section 75 bill (ordinary)")),
        (SECTION_76, _("Section 76 bill (affecting provinces)")),
        (SECTION_77, _("Section 77 bill (money)")),
    ]

    NA = "na"
    NCOP = "ncop"
    HOUSE_CHOICES = [
        (NA, _("National Assembly")),
        (NCOP, _("National Council of Provinces")),
    ]

    bill_number = models.CharField(
        max_length=50,
        unique=True,
        help_text=("B-number, including any version suffix"),
    )
    short_title = models.CharField(
        max_length=255,
        blank=True,
        help_text="Short title of the bill",
    )
    bill_type = models.CharField(
        max_length=20,
        choices=BILL_TYPE_CHOICES,
        blank=True,
        help_text=("Constitutional classification: Section 74, 75, 76 or 77"),
    )
    house_of_origin = models.CharField(
        max_length=10,
        choices=HOUSE_CHOICES,
        blank=True,
        help_text="",
    )
    sponsor = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sponsored_bills",
        help_text="Sponsor or originating authority with a PWMS account",
    )
    sponsor_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Sponsor or originating authority as recorded on the bill - e.g. external body",
    )
    introduced_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date the bill was introduced",
    )
    responsible_committee = models.ForeignKey(
        "Group",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bills_responsible",
        help_text="Committee responsible for the bill",
    )
    atc_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text="ATC reference for the bill's movements",
    )
    order_paper_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text="Order paper reference, where available",
    )
    bill_document_url = models.URLField(
        max_length=2048,
        blank=True,
        help_text="SharePoint link to the bill document",
    )
    sharepoint_folder = models.ForeignKey(
        "SharepointFolder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bills",
        help_text=(
            "This bill's folder under the Bill Base Folder, filed automatically "
            "when the bill is saved; attachments default to it."
        ),
    )
    notes = models.TextField(
        blank=True,
        help_text="Sub-events or explanatory notes under the current stage",
    )

    class Meta:
        verbose_name = "Bill"
        verbose_name_plural = "Bills"

    def __str__(self):
        return (
            f"{self.bill_number} – {self.title}"
            if self.title
            else str(self.bill_number)
        )

    @property
    def sponsor_display(self):
        """The linked sponsor, or the sponsor/authority named on the bill."""
        return self.sponsor or self.sponsor_name

    @property
    def public_status(self):
        """
        The simplified public status for the current state (BRS §12).

        Empty string when the bill has no state yet.
        """
        if self.current_state_id is None:
            return ""
        return self.current_state.public_status.name

    @property
    def current_version(self):
        """
        Label of the bill's current version (BRS §15A).

        Read-only: the :class:`BillVersion` history is the record, so the label
        is derived rather than typed. Prefers the row flagged ``is_current`` and
        falls back to the most recent version; empty string when the bill has no
        versions recorded yet. Reads the related set through the prefetch cache
        when it is loaded (the API bill queryset does), so list responses do not
        query per bill.
        """
        rows = self._version_rows()
        for row in rows:  # Meta.ordering: newest first
            if row.is_current:
                return row.version_label
        return rows[0].version_label if rows else ""

    @property
    def introduced_version(self):
        """
        Label of the bill's original (introduced) version (BRS §15A).

        The earliest ``introduced``-typed version, falling back to the earliest
        version of any type; a bill recorded without versions publishes as its
        B-number — the bill as introduced.
        """
        row = self._introduced_version_row()
        return row.version_label if row is not None else self.bill_number

    @property
    def introduced_version_date(self):
        """
        Date the bill's original (introduced) version was tabled (BRS §15A).

        Tracks :attr:`introduced_version`: the earliest introduced-typed
        version's date, else the earliest version's date, else the bill's
        introduction date.
        """
        row = self._introduced_version_row()
        if row is not None:
            return row.version_date
        return self.introduced_date

    @property
    def current_version_date(self):
        """
        Date the bill's current version was tabled / published (BRS §15A).

        Tracks :attr:`current_version`: the flagged row's date, else the most
        recent version's date; ``None`` when the bill has no versions recorded.
        """
        rows = self._version_rows()
        for row in rows:  # Meta.ordering: newest first
            if row.is_current:
                return row.version_date
        return rows[0].version_date if rows else None

    def _version_rows(self):
        """The bill's versions (prefetch-cache aware)."""
        return list(self.versions.all())

    def _introduced_version_row(self):
        """The original version row: earliest introduced, else earliest of all."""
        rows = self._version_rows()
        introduced = [row for row in rows if row.version_type == BillVersion.INTRODUCED]
        pool = introduced or rows
        if not pool:
            return None
        return min(pool, key=lambda row: (row.version_date, row.pk))


class BillVersion(BaseModel):
    """
    A preserved version of a bill (BRS *Online Bill Tracking* §15A).

    Version integrity: rows are append-only history — historical versions may
    not be overwritten — and ``version_type`` distinguishes the introduced
    bill, amendment schedules and the resulting amended bill from one another,
    so an amendment and its schedule stay recorded together. The B-number
    suffix carried by ``version_label`` records where the number changed.

    Exactly one row per bill may be flagged ``is_current`` (a partial unique
    constraint, normalised in :meth:`save`), which is the version
    ``Bill.current_version`` reports; the rest are the previous versions.
    """

    INTRODUCED = "introduced"
    AMENDED = "amended"
    AMENDMENT_SCHEDULE = "amendment_schedule"
    VERSION_TYPE_CHOICES = [
        (INTRODUCED, _("Introduced version")),
        (AMENDED, _("Amended bill")),
        (AMENDMENT_SCHEDULE, _("Amendment schedule")),
    ]

    bill = models.ForeignKey(
        Bill,
        on_delete=models.CASCADE,
        related_name="versions",
        help_text="Bill this version belongs to.",
    )
    version_label = models.CharField(
        max_length=100,
        help_text=(
            "Version label, including the B-number suffix where it changed "
            "(e.g. 'B 12—2026 (1st amendment)')."
        ),
    )
    version_type = models.CharField(
        max_length=20,
        choices=VERSION_TYPE_CHOICES,
        default=INTRODUCED,
        help_text=("Introduced bill, amended bill or amendment schedule."),
    )
    version_date = models.DateField(
        default=timezone.localdate,
        help_text="Date this version was tabled / published.",
    )
    document_url = models.URLField(
        max_length=2048,
        blank=True,
        help_text="SharePoint link to this version's document.",
    )
    notes = models.TextField(
        blank=True,
        help_text="What changed in this version.",
    )
    recorded_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bill_versions",
        help_text="Who recorded this version.",
    )
    is_current = models.BooleanField(
        default=False,
        help_text="Mark the version currently before Parliament.",
    )

    class Meta:
        ordering = ["-version_date", "-id"]
        indexes = [
            models.Index(fields=["bill", "-version_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["bill"],
                condition=models.Q(is_current=True),
                name="workflows_billversion_one_current_per_bill_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.bill} — {self.version_label}"

    def save(self, *args, **kwargs):
        if not self.is_current or not self.bill_id:
            return super().save(*args, **kwargs)
        with transaction.atomic():
            # Only one version per bill may be current, so demote the others
            # before writing this row and the partial unique constraint holds.
            BillVersion.objects.filter(bill_id=self.bill_id, is_current=True).exclude(
                pk=self.pk
            ).update(is_current=False)
            return super().save(*args, **kwargs)
