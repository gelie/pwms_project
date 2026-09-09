# PWMS Technical Documentation

This folder is the project's self-documentation. It is written to be read in
order, or used as reference while developing.

## Reading order

1. [Technical Stack](./Technical%20Stack.md) — what the project is built on and how to run it.
2. [System Design](./System%20Design.md) — the architecture: layers, the workflow engine, RBAC and auditing.
3. [Data Model](./Data%20Model.md) — every model, its fields and relationships.
4. [Functional Design](./Functional%20Design.md) — what the system does for its users.
5. [API Reference](./API%20Reference.md) — the endpoints and OpenAPI docs.
6. [Management Commands](./Management%20Commands.md) — the `manage.py` commands (sync, jobs, diagrams).
7. [Roadmap & Planned Integrations](./Roadmap%20&%20Planned%20Integrations.md) — email, notifications, SharePoint and other plans.

## Conventions used in these documents

- Diagrams use Mermaid and render on GitHub, VS Code and most Markdown viewers.
- `pwms.` prefixes a Django model (e.g. `pwms.User`); un-prefixed names are
  Django or third-party (e.g. `auditlog.LogEntry`).
- "Workflow instance" = a row of a concrete workflow model
  (currently `InternationalResolution`).
- "Workflow type" / "machine" = a `WorkflowType` (the reusable state/transition definition).

## Keeping docs in sync

When you change models, URLs, commands or architecture, update the matching doc
in the same change. The **Data Model**, **API Reference** and
**Management Commands** pages are the most likely to drift.
