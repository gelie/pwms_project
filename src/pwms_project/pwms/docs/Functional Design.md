# Functional Design

This document describes **what PWMS does** for its users, feature by feature,
and the typical flows. It complements the structural documents
([System Design](./System%20Design.md), [Data Model](./Data%20Model.md)).

---

## 1. Personas

| Persona | Needs |
| --- | --- |
| **Member of Parliament (MP)** | table/own instruments, follow progress, receive alerts |
| **Parliamentary / Committee staff** | manage instruments through committee stages, draft documents, assign work |
| **Committee (Chairperson / Members / Secretary / Researcher)** | view instruments referred to the committee, move them through states, add comments |
| **Administrator / Clerk** | manage users, groups, roles, memberships, workflow definitions |
| **System operator** | run sync/jobs, monitor audit history |

---

## 2. Identity & organisation management

- **Users** are created from the admin and/or synchronised from the legacy Oracle
  source (`sync_users_oracle*` commands). Users carry parliamentary profile data
  (title, party, constituency, membership dates, encrypted ID number).
- **Groups** form an MPTT hierarchy: Parliament → Houses → Portfolio / Select /
  Ad-hoc / Joint Committees → sub-units (offices, divisions, parties, …).
- **Roles** (`Role`) carry coarse workflow capability flags.
- **GroupMembership** ties user + group + role over a time window (start/end
  dates, active flag) so historical memberships are preserved.
- Lifecycle: members leave/join; memberships are deactivated rather than deleted.

**Functional rules**

- A user is “in a role in a group” only while an **active** membership exists.
- Role capability flags gate coarse actions (e.g. `can_manage_permissions`).

---

## 3. Workflow management

### 3.1 Definitions (the machine)

An administrator defines a **WorkflowType** and its **States** and **Transitions**
(e.g. *International Resolution*: `Drafting → Committee → Gazetted → Adopted`),
including:

- which states are initial/terminal;
- which transitions are allowed (from → to);
- which roles may perform each transition (`allowed_roles`);
- which roles should be notified when it happens (`notify_roles`);
- whether a comment is mandatory.

Definitions are reusable: they are exported/imported as JSON
(`export_workflow_type` / `import_workflow_type`) and rendered as diagrams
(`generate_*diagrams`).

### 3.2 Instances

A workflow instance (today: **InternationalResolution**) is created with:

- a workflow **type** and its **initial state**;
- an **owner** and optionally an **assignee**;
- a **deadline** and **priority**;
- initial **referrals** to one or more groups (committees/houses).

The instance always knows its `workflow_type` and `current_state`, and offers
only the **available transitions** for that state.

### 3.3 Transitioning

To move an instance forward, a user performs an available transition:

1. The system validates the transition belongs to the type and is valid from the
   current state; a comment is required if configured.
2. `current_state` advances to the transition’s `to_state`.
3. A `TransitionLog` entry is written (who, from → to, comment, IP).
4. The field change is also captured by auditlog.
5. (Planned) role-based notification emails are dispatched.

### 3.4 Referrals

Instruments can be dynamically **referred to groups** (`referred_to_groups`, M2M,
audited). This is how a draft passes to a committee for input.

---

## 4. Access control

Access decisions use the three RBAC layers (see
[System Design §4](./System%20Design.md#4-contenttype-based-rbac)):

| Question answered | Table |
| --- | --- |
| Does this committee/house have access to this instance at all? | `WorkflowGroupAccess` |
| Within that access, what may *this role* do (override)? | `WorkflowRolePermission` |
| Within that access, what may be done **in this state**? | `WorkflowStatePermission` |

**Example scenario — committee member editing in "Drafting" but not "Gazetted"**

1. `WorkflowGroupAccess(group=Committee X, content_object=resolution, can_view=T, can_edit=F)` —
   the committee may *view* but not *edit* by default.
2. `WorkflowStatePermission(group_access=…, state=Drafting, can_edit=T)` —
   while the resolution is in `Drafting`, the committee may edit.
3. No `WorkflowStatePermission` for `Gazetted` → falls back to the group default
   (view only).
4. `WorkflowRolePermission(group_access=…, role=Committee Chairperson,
   can_transition=T, allowed_states=[Drafting, Committee])` — only chairs can move
   it on, and only out of those states.

`instance.can(user, "transition")` encodes exactly this resolution chain.

---

## 5. Auditing & history

Every workflow instance has a **complete history**, viewable through the API:

| Event | Source | Example |
| --- | --- | --- |
| created | auditlog | CREATE entry with initial field values |
| edited (any field, incl. current_state) | auditlog | UPDATE entry with before/after diff |
| deleted | auditlog | DELETE entry |
| state transition | TransitionLog | `STATE_TRANSITION: Drafting → Gazetted` by alice@… |

Consumers:
- Web/Admin views (planned) to render history timelines.
- REST API `GET …/audit/` returning both audit sources (currently auditlog).

---

## 6. Web UI & API

- **Server-rendered pages** (Bootstrap 5 + HTMX, lucide icons, flatpickr dates):
  home/about, login/logout. (Template surface is expanding.)
- **Django Admin** for administration (users, groups, roles, memberships,
  workflow definitions).
- **REST API (DRF)** exposing audit history; browsable, plus Swagger/ReDoc docs.
- **django-ninja spike** under `/ninja/` compares an alternative implementation —
  see [API Reference](./API%20Reference.md).

---

## 7. Automation & data operations

`manage.py` commands provide (see [Management Commands](./Management%20Commands.md)):

- **Synchronisation** from the legacy Oracle system (users, groups, roles,
  memberships) — basis for keeping identity data current.
- **Committee scraping** (Parliament website) to import committee members.
- **Notification / deadline jobs** — delegate expirations, referral deadlines,
  overdue alerts (email delivery planned).
- **Workflow tooling** — state import, workflow-type import/export, diagrams.
- **Maintenance** — validate memberships, fix missing group access, update event
  statuses, show hierarchy.

---

## 8. Security considerations

- Admin and API both require authentication; the API defaults to
  `IsAuthenticated` (session/basic) and data endpoints deny anonymous access.
- Sensitive personal data (ID numbers) is **encrypted at rest** and keyed by an
  HMAC for lookups (`cryptography`, `IDNO_HMAC_KEY`).
- Full audit trails enable accountability for every state change and edit.
- (Planned) notification content must respect access rules so MPs/staff only see
  what they may.

---

## 9. Typical end-to-end flow

```mermaid
flowchart LR
    A["Resolution drafted<br/>(owner, State: Drafting)"] --> B["Referred to Committee X"]
    B --> C["Committee edits in Drafting<br/>(state permission)"]
    C --> D["Chairperson transitions<br/>to Committee"]
    D --> E["Committee outcome → Gazetted<br/>(transition + log)"]
    E --> F["Adopted (terminal state)"]
```

Each arrow is a **Transition**; each step is audited; each participant’s right to
act was resolved through the **RBAC layers** for that instance and state.
