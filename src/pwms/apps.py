from django.apps import AppConfig


class PwmsConfig(AppConfig):
    name = "pwms"
    verbose_name = "Parliamentary Workflow Management System"

    def ready(self):
        """
        Wire up automatic CRUD auditing (django-auditlog) for the concrete
        workflow models.

        auditlog cannot register abstract models, so each concrete subclass of
        ``AbstractLegislativeWorkflow`` must be registered here explicitly.
        ``ready()`` runs once per process after the app registry is populated,
        which makes it the correct hook (imports are done lazily to avoid
        circular imports).

        Every create/update/delete then produces an ``auditlog.LogEntry`` row
        (with actor + before/after field diffs), complementing the domain
        ``TransitionLog`` events written by ``perform_transition()``.
        """
        from django.apps import apps

        if not apps.is_installed("auditlog"):
            return

        from auditlog.registry import auditlog

        from .models import DelegationReport, InternationalResolution

        # Referrals are typed WorkflowReferral rows whose lifecycle is recorded
        # as domain events, so there are no M2M fields left to track here.
        auditlog.register(
            InternationalResolution,
            exclude_fields=["updated_at"],  # auto timestamps add no audit value
        )
        auditlog.register(
            DelegationReport,
            exclude_fields=["updated_at"],
        )

        # When new concrete workflow models are added (Bill, Motion, Question...),
        # register them here as well, e.g.:
        # auditlog.register(Bill, exclude_fields=["updated_at"])
