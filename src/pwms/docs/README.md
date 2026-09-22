# PWMS Technical Documentation

This folder is the project's self-documentation. It is written to be read in
order, or used as reference while developing.

## Reading order

1. [Technical Stack](./Technical%20Stack.md) — what the project is built on and how to run it.
2. [System Design](./System%20Design.md) — the architecture: layers, the workflow engine, RBAC and auditing.
3. [Data Model](./Data%20Model.md) — every model, its fields and relationships.
4. [Functional Design](./Functional%20Design.md) — what the system does for its users.
5. [API Reference](./API%20Reference.md) — the endpoints and OpenAPI docs.
6. [Search Lookups](./Search%20Lookups.md) — the HTMX search pickers (users, groups, countries, cities) and the country → city cascade.
7. [Management Commands](./Management%20Commands.md) — the `manage.py` commands (sync, jobs, diagrams).
8. [SharePoint Sync](./SharePoint%20Sync.md) — the `populate_sites` command, the local `Sharepoint*` tables, and document attachments/versioning.
9. [Roadmap & Planned Integrations](./Roadmap%20&%20Planned%20Integrations.md) — email, notifications, SharePoint and other plans.

## User-facing documents

These are written for end users rather than developers, and are the only ones
meant to be read by people who will not be reading the code.

| Document | Contents |
| --- | --- |
| [User Manual Introduction](./User%20Manual%20Introduction.md) | *Systems Overview* and *The Purpose* — PWMS in plain language, for the front of a user manual |
| [Introductory Lesson Plan](./Introductory%20Lesson%20Plan.md) | The 90-minute induction for the IRPD section: objectives and outcomes, a timed running order with demo scripts, hands-on tasks, an exit ticket and the trainer's preparation checklist |
| [Training Manual](./Training%20Manual.md) | The complete IRPD training course: learning objectives and outcomes, fourteen modules, practical exercises, an assessment and the administrator prerequisites |
| [Quick Reference Card](./Quick%20Reference%20Card.html) ([PDF](./Quick%20Reference%20Card.pdf)) | A printable one-page A4 wall card for the section: the three lifecycles, creating records, transitions, referrals, documents, reports, alerts and who to escalate to |
| [UAT Form](./UAT%20Form.html) ([PDF](./UAT%20Form.pdf), [workbook](./UAT%20Form.xlsx)) | The user acceptance testing form: 38 IRPD scenarios with acceptance criteria (10 of them critical), a defect log, the expected-behaviour list, the acceptance decision and sign-off — plus the results workbook the section fills in, which rolls the results up |

### Regenerating the generated files

The quick-reference card and the UAT form are generated from their HTML source,
and the UAT workbook is generated from the form; the PDFs and the workbook are
**generated, not hand-edited**. After changing either HTML file, rebuild what
depends on it with WeasyPrint and `build_uat_workbook` (both are project
dependencies) and check the result before committing — the card must stay a single
A4 page, the form's tables must still repeat their header rows across page breaks,
and the workbook must build from the form without error:

```bash
.venv/bin/python -c "from weasyprint import HTML; d = HTML(filename='src/pwms/docs/Quick Reference Card.html').render(); print('pages:', len(d.pages)); d.write_pdf('src/pwms/docs/Quick Reference Card.pdf')"
.venv/bin/python -c "from weasyprint import HTML; d = HTML(filename='src/pwms/docs/UAT Form.html').render(); print('pages:', len(d.pages)); d.write_pdf('src/pwms/docs/UAT Form.pdf')"
.venv/bin/python manage.py build_uat_workbook
```

## Conventions used in these documents

- Diagrams use Mermaid and render on GitHub, VS Code and most Markdown viewers.
- `pwms.` prefixes a Django model (e.g. `pwms.User`); un-prefixed names are
  Django or third-party (e.g. `auditlog.LogEntry`).
- "Workflow instance" = a row of a concrete workflow model (today
  `DelegationReport`, `InternationalResolution`, `InternationalAgreement` or
  `Bill`).
- "Workflow type" / "machine" = a `WorkflowType` (the reusable state/transition definition).

## Keeping docs in sync

When you change models, URLs, commands or architecture, update the matching doc
in the same change. The **Data Model**, **API Reference** and
**Management Commands** pages are the most likely to drift.
