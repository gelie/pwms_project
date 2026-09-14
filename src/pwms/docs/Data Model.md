# Data Model

All `pwms` models are defined under `pwms/models/` and registered through
`pwms/models/__init__.py`. Database table names are prefixed `pwms_`
(e.g. `pwms_internationalresolution`).

---

## ERD (overview)

```mermaid
erDiagram
    Group ||--o{ Group : "parent / children (MPTT)"
    User ||--o{ GroupMembership : memberships
    Group ||--o{ GroupMembership : members
    Role ||--o{ GroupMembership : "role held"
    User ||--o{ Role : "has via memberships"

    WorkflowType ||--o{ State : states
    WorkflowType ||--o{ Transition : transitions
    WorkflowType ||--o{ WorkflowType : "parent_type / child_types"
    WorkflowType }o--|| Group : "group (RBAC scope)"
    WorkflowType }o--o{ Role : create_roles
    State ||--o{ Transition : "from_state"
    State ||--o{ Transition : "to_state"
    Transition }o--o{ Role : allowed_roles
    Transition }o--o{ Role : notify_roles
    Transition }o--o{ EventType : required_event_types
    Transition ||--o{ TransitionCondition : conditions

    AbstractLegislativeWorkflow ||--|| WorkflowType : workflow_type
    AbstractLegislativeWorkflow ||--|| State : current_state

    WorkflowReferral }o--|| AbstractLegislativeWorkflow : "GFK content"
    WorkflowReferral }o--|| Group : referred_to
    DelegationReportUpdate }o--|| DelegationReport : updates
    WorkflowEvent }o--|| AbstractLegislativeWorkflow : "GFK content"
    WorkflowEvent }o--|| EventType : event_type

    WorkflowRelationship }o--|| AbstractLegislativeWorkflow : "parent (GFK)"
    WorkflowRelationship }o--|| AbstractLegislativeWorkflow : "child (GFK)"

    InternationalResolution ||--o{ WorkflowGroupAccess : "GFK content"
    WorkflowGroupAccess ||--o| Group : group
    WorkflowGroupAccess ||--o{ WorkflowRolePermission : role_permissions
    WorkflowGroupAccess ||--o{ WorkflowStatePermission : state_permissions
    WorkflowRolePermission }o--|| Role : role
    WorkflowRolePermission }o--o{ State : allowed_states
    WorkflowStatePermission }o--|| State : state

    InternationalResolution ||--o{ TransitionLog : "GFK content"
    InternationalResolution ||--o{ auditlog.LogEntry : "GFK content (registered)"
```

> The abstract `AbstractLegislativeWorkflow` is shown for clarity; it has no
> table. Its fields are copied onto each concrete subclass
> (`InternationalResolution`, `DelegationReport`, `InternationalAgreement` and
> `Bill` today).

---

## 1. `BaseModel` (abstract) — `pwms/models/base.py`

Shared by every pwms model.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | BigAutoField | internal integer PK (used by GenericForeignKeys) |
| `public_id` | UUID | **uuid7**, unique, indexed — external/URL-safe identifier |
| `created_at` | DateTime | auto |
| `updated_at` | DateTime | auto (often excluded from audit diffs) |

---

## 2. Identity & organisation

### `User(AbstractUser, BaseModel)` — `pwms.User`

Django user with parliamentary profile fields:

| Field | Notes |
| --- | --- |
| `title`, `middle_name`, `gender`, `phone`, `bio`, `avatar` | profile |
| `employee_type` | staff / member / graduate |
| `identity_source` | `erp` (owned by `sync_users_oracle`) or `local` (owned by PWMS — e.g. Ministers appointed from outside the ERP) |
| `positiondesc`, `supervisor` (self FK) | reporting |
| `department` (FK `Group`) | home department |
| `date_of_birth`, `date_joined_parliament`, `termination_date` | lifecycle |
| `idno_encrypted`, `idno_hmac` | ID number stored encrypted; HMAC (key from `IDNO_HMAC_KEY`) enables unique lookup |
| `is_mp`, `is_staff_member`, `is_active` | flags |
| `constituency`, `party_affiliation` | political attributes |

Methods include membership/role helpers (`User.ministers()`, `current_portfolio`) and `can_transition_workflow(...)`.

### `Group(MPTTModel, BaseModel)` — `pwms.Group`

Hierarchical organisational unit:

| Field | Notes |
| --- | --- |
| `name`, `short_name`, `slug` (unique) | identity |
| `group_type` | legislature, house, portfolio/select/special/ad-hoc/joint/internal committee, administration, office, division, section, business_unit, party, executive, presidency, ministry, department, province, premier, delegation |
| `parent` (Tree FK) | MPTT hierarchy |
| `description`, `is_active`, `contact_email`, `contact_phone`, `location`, `start_date`, `end_date` | metadata |

Helpers: `get_full_path()`, `get_active_members()`.

Migration `0023_seed_executive_groups_and_roles` seeds the executive branch:
`Government of RSA` (type `executive`) with one child per Cabinet ministry (type
`ministry`, except *The Presidency*, which is `presidency`), plus the `Minister`
and `Deputy Minister` roles. A Minister is a `GroupMembership` linking their
`User` to a ministry group with that role — office is held over time, so no
separate "executive user" model is needed.

### `Role(BaseModel)` — `pwms.Role`

| Field | Notes |
| --- | --- |
| `name`, `slug` (unique), `description` | identity |
| `can_transition_workflows`, `can_create_workflows`, `can_assign_workflows`, `can_manage_permissions` | workflow capability flags |

### `GroupMembership(BaseModel)`

Users hold roles **within** groups, over a period:

| Field | Notes |
| --- | --- |
| `user` (FK `User`, related `memberships`) | who |
| `group` (FK `Group`, related `members`) | where |
| `role` (FK `Role`, PROTECT) | what role |
| `start_date`, `end_date`, `is_active`, `notes` | membership period |
| `updated_by` (FK `User`) | who last edited |

Unique on `(user, group, role, start_date)`; `clean()` validates dates.

---

## 3. Workflow engine & instances — `pwms/models/workflows.py`

### `WorkflowType`

| Field | Notes |
| --- | --- |
| `name` (unique), `slug`, `description`, `enabled` | registry entry |
| `group` (FK, related `workflow_types`) | RBAC scope: the group whose members/roles govern this type |
| `create_roles` (M2M to `Role`, related `creatable_workflow_types`) | roles **from that group** allowed to create instances; empty means superusers only |
| `viewer_groups` (M2M to `Group`, related `viewer_workflow_types`) | read-only stakeholder groups; every new instance is shared with them (a read-only `WorkflowGroupAccess` row) at creation |
| `parent_type` (self-FK, related `child_types`) | optional type hierarchy; declaring child types opts the parent into type-restricted instance links |

Reverse relations: `states`, `transitions`, `child_types`, `%(class)s_instances`.

Type-level hierarchy helpers: `is_root_type`, `get_ancestor_types()`,
`get_descendant_types()`, `allowed_child_types()` (empty means "unrestricted").

Group-scoped creation helpers: `group_roles()` (roles held by members of
`group`, i.e. the valid `create_roles` choices), `can_create(user)` (superuser
bypass; otherwise requires one of `create_roles` held in that exact group) and
the classmethod `creatable_by(user)` returning the enabled types a user may
create in. `group` is nullable in the database because the seeded types exist
before their group does, but it is **required in the admin**; the role/group
pairing is enforced by `WorkflowTypeAdminForm`.

Type-level **viewer groups** (`viewer_groups`) are read-only stakeholders —
groups with an interest in every instance of the type but no active role in
producing it. On creation each instance materialises its `WorkflowGroupAccess`
rows from the type via `AbstractLegislativeWorkflow.materialize_group_access()`:

* the type's owning `group`, flagged `is_primary`;
* one row per `viewer_groups` entry.

Every materialised row starts read-only (`can_view` on, all other capabilities
off), so "who can see this instance" stays answerable from the instance's own
access table with a `granted_at` timestamp per grant. Materialisation runs only
at creation; run the `sync_type_group_access` command to backfill instances that
predate a policy change.

### `State`

| Field | Notes |
| --- | --- |
| `workflow_type` (FK, related `states`) | owning machine |
| `name`, `slug`, `description` | identity (slug unique per type) |
| `is_initial`, `is_terminal`, `allows_referrals` | behaviour flags |
| `order`, `color` | display |

Uniqueness: `(workflow_type, name)` and `(workflow_type, slug)`.

### `Transition`

| Field | Notes |
| --- | --- |
| `workflow_type` (FK, related `transitions`) | owning machine |
| `name`, `slug` | identity (slug unique per type) |
| `from_state`, `to_state` (FK `State`) | the edge |
| `allowed_roles` (M2M `Role`) | who may take this transition |
| `notify_roles` (M2M `Role`) | who gets alerted on it |
| `required_event_types` (M2M `EventType`) | event guards — blocked until each type has been recorded on the instance |
| `requires_comment`, `order` | behaviour |

Uniqueness: `(workflow_type, from_state, to_state)` and `(workflow_type, slug)`.

### `TransitionCondition(BaseModel)`

Extra business rule guarding a transition, beyond the state machine and the
event guards. Evaluated by `unmet_transition_conditions()` together with
`required_event_types`.

| Field | Notes |
| --- | --- |
| `transition` (FK, related `conditions`) | guarded transition |
| `condition_type` | `no_open_referrals` / `all_children_closed` / `field_set` |
| `field_name` | parameter for `field_set`, e.g. `adoption_date` |
| `enabled`, `order` | switch + evaluation order |

Handlers live in the `TRANSITION_CONDITION_HANDLERS` registry (extend with the
`@transition_condition("name")` decorator). Unique
`(transition, condition_type, field_name)`.

### `AbstractLegislativeWorkflow` (abstract)

| Field | Notes |
| --- | --- |
| `workflow_type` (FK) | machine the instance follows |
| `title`, `description` | content |
| `current_state` (FK `State`) | where it is now (validated to belong to `workflow_type`) |
| `owner`, `assigned_to` (FK `User`) | responsible parties |
| `deadline`, `priority` | due date + low/medium/high/urgent |

Referrals are **not** an M2M: they are typed `WorkflowReferral` rows, so they
carry who referred, when, to whom, by when, and the response (see below). The
base class exposes `refer(group, ...)`, `referrals()` and `open_referrals()`;
creation is gated by `State.allows_referrals`.

**Parent / child hierarchy.** Concrete instances of different subtypes can be
nested (a `DelegationReport` contains many `InternationalResolution` rows).
Because a self-referential FK is impossible on an abstract model, the link
lives in `WorkflowRelationship` and the base class exposes a tree API:

| Helper | Purpose |
| --- | --- |
| `parent_relationship`, `parent_workflow`, `relationship_type` | link to / data about the parent (`None`/`''` when root) |
| `sub_workflows`, `has_sub_workflows` | direct children |
| `is_root_workflow`, `hierarchy_level` | position in the tree |
| `get_ancestors()`, `get_workflow_hierarchy_path()`, `get_all_descendants()` | tree traversal (cycle-safe) |
| `set_parent_workflow(parent, relationship_type=...)`, `clear_parent_workflow()` | attach / detach / re-parent |
| `add_sub_workflow(child, ...)`, `remove_sub_workflow(child)` | manage children |

### `DelegationReport(AbstractLegislativeWorkflow)`

A delegation's report on an international engagement/forum (BRS BR02/BR03). It
contains many `InternationalResolution` rows through `WorkflowRelationship`
and parents many `DelegationParticipant` rows.

| Field | Notes |
| --- | --- |
| `reference_number` (unique) | system-generated `DR-<year>-<sequence>` (BR02.6) |
| `engagement_name` | international engagement / forum (BR02.3.3) |
| `engagement_start_date`, `engagement_end_date` | validated: not future, end ≥ start (BR02.3.4/5) |
| `location_city`, `location_country` | engagement location (BR02.3.6); FKs to `City` / `Country` |
| `notes` | additional notes / follow-up action (BR02.3.13) |
| `report_document_url` | SharePoint link to the delegation report (BR02.3.14) |
| read-only `atc_reference`, `atc_publication_date`, `atc_page_number`, `atc_document_url` | latest values from the `updates` history (BR03.5.3/5) |
| inherited `deadline` | due date for implementation (BR02.3.12) |
| inherited `assigned_to` + `assigned_to_email` | assigned official + email (BR02.3.10/11) |

Helpers: `is_overdue` / `overdue_identifier` implement BR12 ("Due date expired
– pending follow up" when the deadline is past and the state is not terminal);
`record_update(...)` appends to the BR03 history (see
`DelegationReportUpdate`).

### `InternationalResolution(AbstractLegislativeWorkflow)`

A resolution adopted at an engagement, captured from the delegation report's
recommendations (BR02.3.9).

| Field | Notes |
| --- | --- |
| `resolution_number` (unique) | resolution reference |
| `resolution_text` | the resolution as concluded |
| `adoption_date` | date of adoption |
| `responsible_group` (FK `Group`) | committee/group responsible for implementation |
| `implementation_progress` | latest implementation narrative |

### `InternationalAgreement(AbstractLegislativeWorkflow)`

A government international agreement tabled in Parliament and tracked through
referral, committee consideration and House adoption (BRS *International
Agreements Tracking and Monitoring*, BR02/BR03).

| Field | Notes |
| --- | --- |
| `reference_number` (unique) | system-generated `IA-<year>-<sequence>` (BR02) |
| `agreement_type` | `section-231-2` / `section-231-3` (BR02) |
| `submitting_department` | department that submitted the agreement (BR02) |
| `responsible_minister` (FK `User`, related `responsible_agreements`) | responsible Member of the Executive (BR02); `clean()` accepts only a current or former office holder, and `responsible_minister_name` records the minister as tabled (former minister, or one without an account) |
| `atc_tabling_date` | date the agreement was tabled in the ATC (BR02) |
| `atc_reference` | reference details of the ATC / relevant documents (BR02) |
| `referral_committees` (M2M `Group`, related `international_agreements_referred`) | committees the agreement is referred to (BR02) |
| `notes` | additional notes / follow-up action (BR02) |
| `agreement_document_url`, `explanatory_memorandum_url` | SharePoint links to the agreement and its explanatory memorandum (BR02/BR04/BR11) |
| inherited `deadline` | due date for implementation (BR02) |
| inherited `assigned_to` + `assigned_to_email` | assigned official + email (BR02) |

Helpers: `is_overdue` / `overdue_identifier` implement BR12 ("Due date expired
– pending follow up" when the deadline is past and the state is not terminal).

### `Bill(AbstractLegislativeWorkflow)`

A Parliamentary bill tracked through its legislative lifecycle (BRS *Online Bill
Tracking*). Internal procedural states map to the seven high-level public
statuses through `State.public_name`, exposed on the model as `public_status`.

| Field | Notes |
| --- | --- |
| `bill_number` (unique) | Parliament's B-number, including any version suffix (BRS §13.1) |
| `short_title` | short title (BRS §7A) |
| `bill_type` | `section-74` / `section-75` / `section-76` / `section-77` (BRS §13.1) |
| `house_of_origin` | `na` / `ncop` — House of introduction (BRS §7A) |
| `sponsor` (FK `User`, related `sponsored_bills`) | sponsor (BRS §7A); `sponsor_name` keeps an originating authority or a historical name |
| `introduced_date` | date introduced (BRS §13.1) |
| `responsible_committee` (FK `Group`, related `bills_responsible`) | committee responsible (BRS §13.2) |
| `atc_reference`, `order_paper_reference` | authoritative-source references (BRS §7B) |
| `bill_document_url` | SharePoint link to the bill document (BRS §7B) |
| `notes` | sub-events / explanatory notes under the current stage (BRS §15A) |

Properties: `public_status` returns the current state's `public_name` display
value (empty string when the bill has no state); `current_version` is **derived**
from the version history (the row flagged `is_current`, else the most recent
version, else `""`) rather than typed — see `BillVersion` below. `is_overdue` /
`overdue_identifier` behave as on the other concrete workflows.

### `BillVersion(BaseModel)`

A preserved version of a bill (BRS §15A, *version integrity*). Rows are
append-only history — historical versions may not be overwritten — and the
version type distinguishes the introduced bill, amendment schedules and the
resulting amended bill so an amendment and its schedule stay recorded together.

| Field | Notes |
| --- | --- |
| `bill` (FK, related `versions`) | owning bill |
| `version_label` | label including the B-number suffix where it changed (BRS §15A) |
| `version_type` | `introduced` / `amended` / `amendment_schedule` (BRS §15A) |
| `version_date` | date this version was tabled / published |
| `document_url` | SharePoint link to this version's document (BRS §7B) |
| `notes` | what changed in this version |
| `recorded_by` (FK `User`, optional) | contributor accountability (BRS §7A) |
| `is_current` | the version before Parliament; one per bill (partial unique constraint) |

Ordered newest first and indexed on `(bill, -version_date)`. `save()` demotes any
other current row for the bill inside a transaction, so the partial unique
constraint on `(bill) WHERE is_current` holds and `Bill.current_version` stays
unambiguous. Versions are recorded from the bill's page (`bill_version_create`)
and corrected in place (`bill_version_update`) via `BillVersionForm` — the bill
and `recorded_by` come from the request, and an edit keeps the row's identity
and original recorder while `auditlog` captures the change. They are also
editable through the Django admin (an inline on the bill page plus a standalone
`BillVersionAdmin`) and bulk-loaded with `import_bill_versions`.

### `DelegationParticipant(BaseModel)`

Delegation members and support officials (BR02.3.7/8).

| Field | Notes |
| --- | --- |
| `delegation_report` (FK, related `participants`) | owning report |
| `participant_type` | `member` / `official` |
| `title`, `first_name`, `last_name`, `delegation_role` | person details |
| `user` (FK `User`, optional) | link to a system account |
| `order` | display order |

Property `full_name`. Indexed on `(delegation_report, participant_type)`.

### Seeded workflow definitions

Migration `0005_seed_workflow_definitions` creates (idempotently, matched by
natural key) the seeded BRS machines; migration
`0016_seed_international_agreement_workflow` adds the third and
`0019_seed_bill_workflow` the Bill lifecycle:

| Type | States |
| --- | --- |
| **Delegation Report** | Awaiting PGIR approval *(initial)* → Submitted for tabling → Tabled and referred to Committee → Closed – House approved *(terminal)* |
| **International Resolution** | Captured *(initial)* → Assigned → In Progress → Implemented → Closed *(terminal)*; `parent_type` = Delegation Report |
| **International Agreement** | Submitted for tabling → Agreement Tabled – referred to Committee *(initial)* → Committee considering and processing → Committee submitted report for tabling → House adopted – referred to Department → Closed – House approved *(terminal)* |
| **Bill** | Introduced *(initial)* → Referred to Committee → Public Participation → Committee Deliberation → Committee Report → House Debate and Voting → NCOP Consideration → Awaiting Presidential Assent → Signed into Law *(terminal)*, with mediation, presidential referral-back and withdrawal branches; each state carries a BRS §12 public status in `public_name` |

Each step has a single onward transition; roles are **not** attached to these
transitions (assign `allowed_roles` / `notify_roles` per environment). The Bill
machine is the exception to "a single onward transition" at its branching
states (NCOP consideration and presidential assent) and its withdrawal edges,
which require a comment.

Migration `0012_assign_workflow_type_rbac_group` points the delegation-report
and international-resolution types at group 98 — *IRP: MR: Man And Gen*, the
Multilateral Relations unit that handles international engagements — and
`0016_seed_international_agreement_workflow` owns the international-agreement
type with the same group. Those migrations are no-ops where the group does not
yet exist (a fresh or test database), because the types are seeded before the
group, which only arrives with the Oracle organisational sync. `create_roles`
is intentionally left empty: assign the creating roles per environment in the
WorkflowType admin (only roles held by members of the type's group are offered).

Migration `0021_assign_bill_workflow_type_group` gives the **Bill** type its
owning group — *LSO: Legal Services: Man And Gen*, the Legal Services Office's
Management & General section. Like `0012`/`0016` it is a no-op where the group
does not exist, and it never overwrites an existing assignment. `create_roles`
stays empty until an administrator assigns the creating roles.

Migration `0007_seed_event_types` adds the standard event types
(`report-document-attached`, `atc-update-published`, `implementation-reported`,
`referral-created`, `referral-responded`) and wires the first guard: *Delegation
Report → Close – House approved* requires an `atc-update-published` event.
Migration `0009` adds `referral-recalled` / `referral-expired`.

### `WorkflowReferral(BaseModel)`

A referral of a workflow instance to a committee/group. Replaces the old
`referred_to_groups` M2M, which could not carry who/when/by-when/response.
Created through `instance.refer(group, ...)`, gated by `State.allows_referrals`.

| Field | Notes |
| --- | --- |
| `content_type` + `object_id` → `content_object` | GFK to the workflow instance |
| `referred_to` (FK `Group`) | committee / group asked to consider |
| `referred_by` (FK `User`), `referred_at` | who / when |
| `due_date` | response deadline; `is_overdue` flag |
| `status` | `open` / `responded` / `recalled` / `expired` / `cancelled` |
| `responded_at`, `responded_by`, `response_document_url`, `response_notes` | response |
| `recalled_at`, `recalled_by`, `recall_reason` | withdrawal |
| `deadline_notified_at` | reminder job marker |
| `notes` | free text |

Lifecycle helpers `respond()`, `recall()`, `mark_expired()` and creation emit
`referral-created` / `referral-responded` / `referral-recalled` /
`referral-expired` events automatically (`save()` detects create/status
changes), so referrals always appear on the instance timeline.

### `WorkflowRelationship(BaseModel)`

Generic parent/child link between two concrete workflow instances (one table
serves every subclass, like the RBAC tables).

| Field | Notes |
| --- | --- |
| `parent_content_type`, `parent_object_id`, `parent` (GFK) | the containing workflow |
| `child_content_type`, `child_object_id`, `child` (GFK) | the contained workflow |
| `relationship_type` (`contains`/`follows_up`/`supersedes`/`relates_to`) | nature of the link |
| `order`, `notes` | display order + annotation |

Uniqueness: `(child_content_type, child_object_id)` — a child has a single
parent, so instances form a tree. `clean()` / `save()` reject self-parenting and
cycles, and enforce the parent type's declared child types when any exist.

### `DelegationReportUpdate(BaseModel)`

The BR03 update history for a delegation report: one row per update, replacing
the previous single-set ATC fields (now read-only "latest" properties on
`DelegationReport`). Append via `report.record_update(...)`.

| Field | Notes |
| --- | --- |
| `delegation_report` (FK, related `updates`) | owning report |
| `update_date` | BR03.5.2 (defaults to today) |
| `resulting_state` (FK `State`) | BRS "update type" — status after the update (BR03.5.1) |
| `atc_reference`, `atc_publication_date`, `atc_page_number` | ATC details (BR03.5.3) |
| `atc_document_url` | SharePoint link to the ATC document (BR03.5.5) |
| `notes`, `recorded_by` | free text + actor |

Creating a row **that carries ATC details** emits an `atc-update-published`
event (create-only — editing a row does not re-emit), which is the evidence the
seeded close guard requires.

---

## 4. RBAC layer

### `WorkflowGroupAccess`

| Field | Notes |
| --- | --- |
| `group` (FK `Group`, related `workflow_accesses`) | House/Committee granted access |
| `content_type` + `object_id` → `content_object` | **GFK** to the workflow instance |
| `can_view`, `can_edit`, `can_delete`, `can_share`, `can_comment`, `can_manage`, `can_transition` | group-level defaults |
| `is_primary`, `granted_by` (FK `User`), `granted_at` | grant metadata |

Indexed on `(content_type, object_id)`.

### `WorkflowRolePermission`

Per-role override inside a group access — **authoritative for that role** when a
row exists.

| Field | Notes |
| --- | --- |
| `group_access` (FK, related `role_permissions`) | parent |
| `role` (FK `Role`) | which role |
| `can_view/edit/delete/share/comment/manage/transition` | overrides |
| `allowed_states` (M2M `State`) | if set, override applies only in these states |

Unique `(group_access, role)`.

### `WorkflowStatePermission`

What the granted group may do **in a particular state**.

| Field | Notes |
| --- | --- |
| `group_access` (FK, related `state_permissions`) | parent |
| `state` (FK `State`) | the state |
| `can_view/edit/delete/share/comment/manage/transition` | abilities |

Unique `(group_access, state)`.

---

## 5. Auditing

### `TransitionLog` (plain `models.Model`, not `BaseModel`)

Domain event log for workflow progression — one table serves every subclass via
a GFK.

| Field | Notes |
| --- | --- |
| `content_type` + `object_id` → `content_object` | GFK to the workflow instance |
| `action` | e.g. `STATE_TRANSITION`, future `REFERRAL` |
| `from_state`, `to_state` (FK `State`) | the move |
| `actor` (FK `User`) | who performed it |
| `ip_address`, `timestamp`, `notes` | context |

Indexed on `(content_type, object_id, -timestamp)`.

### `EventType` (registry) and `WorkflowEvent` (append-only log)

`WorkflowEvent` records **business-meaningful lifecycle facts** ("Report document
attached", "ATC update published", "referral answered") as opposed to
`TransitionLog`, which records *state changes*. Events are evidence, not state:
rows are immutable (``save()`` refuses updates; corrections are compensating
events), so the timeline stays trustworthy (BR08).

| `EventType` field | Notes |
| --- | --- |
| `name` (unique), `slug`, `description` | registry entry |
| `is_system` | seeded standard type; treated as read-only |

| `WorkflowEvent` field | Notes |
| --- | --- |
| `content_type` + `object_id` → `content_object` | GFK to any concrete workflow instance |
| `event_type` (FK `EventType`) | what happened |
| `occurred_at` | when (defaults to now) |
| `actor` (FK `User`, nullable) | who caused it (null = system) |
| `origin` | `user` / `system` / `integration` |
| `payload` (JSON) | event-specific structured detail |
| `document_url` | optional SharePoint / document reference |
| `notes`, `transition_log` (FK, optional) | context + originating state change |

Indexed on `(content_type, object_id, -occurred_at)` and
`(event_type, -occurred_at)`. The base class exposes `record_event(...)` and
`events()`.

**Event guards on transitions.** `Transition.required_event_types` (M2M
`EventType`) blocks a transition until at least one event of each listed type
exists on the instance. `perform_transition()` evaluates the guards and raises
with *all* unmet requirements (via `unmet_transition_conditions()`).
Seeded example (migration `0007`): *Delegation Report → Close – House approved*
requires an `atc-update-published` event (BR03.5.3).

### `auditlog.LogEntry` (third-party)

Automatic CRUD history for registered models. For `InternationalResolution` and
`DelegationReport` it is written on create/update/delete with `actor`,
`timestamp`, `remote_addr` and a field-level `changes` diff (including M2M
changes for `referred_to_groups`). Reached via `auditlog.models.LogEntry`
(see `pwms/utils/audit_helpers.py`).

---

## 6. Conventions worth remembering

- **GFK over FK-to-abstract:** cross-cutting tables that reference *any* workflow
  subclass use `GenericForeignKey` because Django forbids FKs to abstract models.
- **Integer PK is the GFK target** (`object_id` stores `pwms_internationalresolution.id`).
- **New concrete workflow model checklist:** subclass the abstract base, run
  `makemigrations`, register with `auditlog` in `apps.py`, add an audit API view.

---

## 7. Reference data — countries & cities

`pwms/models/geography.py`. The two tables are reference data: nothing in the
app creates them, and they are filled by `manage.py load_places` from the bundled
GeoNames extract (see [the data README](../data/README.md) and
[Management Commands](./Management%20Commands.md)).

### `Country(BaseModel)`

| Field | Notes |
| --- | --- |
| `code` (unique) | ISO 3166-1 alpha-2, e.g. `ZA`; also matched when searching |
| `iso3` | ISO 3166-1 alpha-3 |
| `name` (indexed) | display name |
| `continent` | GeoNames continent code (`AF`, `AS`, `EU`, `NA`, `OC`, `SA`, `AN`) |

### `City(BaseModel)`

| Field | Notes |
| --- | --- |
| `country` (FK `Country`, cascade) | `related_name="cities"` |
| `name` | populated place, as GeoNames spells it |
| `ascii_name` | diacritic-free spelling; searched alongside `name` |
| `latitude`, `longitude` | decimal degrees (9,6) |
| `population` | used to rank search results; `0` when the source has no figure |

Indexed on `(country, name)` and `(country, ascii_name)`, which is what the
`pwms:city_search` fragment filters on. 47,360 rows: every `cities15000` place
plus all 13,529 South African populated places.

> Contains data from [GeoNames](https://www.geonames.org/), licensed
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Keep that
> attribution wherever this data is published.
