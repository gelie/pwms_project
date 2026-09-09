from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from workflows.models import User, WorkflowReferral
from workflows.views import _create_notification


class Command(BaseCommand):
    help = "Check referral deadlines and send notifications or auto-recall expired referrals"

    def handle(self, *args, **options):
        now = timezone.now()

        # Check for deadlines approaching (24 hours before)
        deadline_24h = now + timedelta(hours=24)
        approaching_24h = WorkflowReferral.objects.filter(
            deadline__isnull=False,
            deadline__lte=deadline_24h,
            deadline__gt=now,
            recalled_at__isnull=True,
            deadline_notified=False,
        )

        # Check for deadlines approaching (1 hour before)
        deadline_1h = now + timedelta(hours=1)
        approaching_1h = WorkflowReferral.objects.filter(
            deadline__isnull=False,
            deadline__lte=deadline_1h,
            deadline__gt=now,
            recalled_at__isnull=True,
            deadline_notified=True,  # Already notified for 24h
        )

        # Check for expired deadlines (auto-recall)
        expired = WorkflowReferral.objects.filter(
            deadline__isnull=False, deadline__lte=now, recalled_at__isnull=True
        )

        self.stdout.write(
            f"Found {approaching_24h.count()} referrals approaching 24h deadline"
        )
        self.stdout.write(
            f"Found {approaching_1h.count()} referrals approaching 1h deadline"
        )
        self.stdout.write(f"Found {expired.count()} expired referrals")

        # Process 24-hour warnings
        for referral in approaching_24h:
            self.send_deadline_warning(referral, "24 hours")
            referral.deadline_notified = True
            referral.save()

        # Process 1-hour warnings
        for referral in approaching_1h:
            self.send_deadline_warning(referral, "1 hour")

        # Process expired referrals (auto-recall)
        for referral in expired:
            self.auto_recall_referral(referral)

        self.stdout.write(self.style.SUCCESS("Deadline check completed successfully"))

    def send_deadline_warning(self, referral, time_remaining):
        """Send deadline warning notifications"""
        # In-app notification to referred group members
        group_members = User.objects.filter(
            memberships__group=referral.referred_to, memberships__is_active=True
        ).distinct()
        for member in group_members:
            _create_notification(
                user=member,
                verb="deadline_warning",
                title=f"Referral Deadline Warning: {referral.workflow.title}",
                message=f"The referral for '{referral.workflow.title}' expires in {time_remaining}. Please respond before {referral.deadline.strftime('%Y-%m-%d %H:%M')}.",
                workflow=referral.workflow,
            )

        # Email notification to referred group members
        recipient_emails = [member.email for member in group_members if member.email]
        if recipient_emails:
            send_mail(
                subject=f"Referral Deadline Warning - {referral.workflow.title}",
                message=f"""
Dear {referral.referred_to.name} Member,

This is a reminder that the referral for the workflow '{referral.workflow.title}'
expires in {time_remaining}.

Deadline: {referral.deadline.strftime("%Y-%m-%d %H:%M")}
Workflow: {referral.workflow.title}
Referred by: {referral.referred_by.get_full_name() or referral.referred_by.username}

Please take appropriate action before the deadline expires.

Best regards,
PWMS System
                """.strip(),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@pwms.com"),
                recipient_list=recipient_emails,
                fail_silently=False,
            )

        # Notify workflow owner
        _create_notification(
            user=referral.workflow.owner,
            verb="deadline_warning",
            title=f"Referral Deadline Warning: {referral.workflow.title}",
            message=f"The referral to {referral.referred_to.name} expires in {time_remaining}.",
            workflow=referral.workflow,
        )

    def auto_recall_referral(self, referral):
        """Auto-recall expired referral"""
        # Mark as recalled
        referral.recalled_at = timezone.now()
        referral.recalled_by = None  # System recall
        referral.recall_reason = f"Auto-recalled: referral deadline expired on {referral.deadline.strftime('%Y-%m-%d %H:%M')}"
        referral.save()

        # Notify referred group members
        group_members = User.objects.filter(
            memberships__group=referral.referred_to, memberships__is_active=True
        ).distinct()
        for member in group_members:
            _create_notification(
                user=member,
                verb="auto_recall",
                title=f"Referral Auto-Recalled: {referral.workflow.title}",
                message=f"The referral for '{referral.workflow.title}' has been automatically recalled due to deadline expiration.",
                workflow=referral.workflow,
            )

        # Notify workflow owner
        _create_notification(
            user=referral.workflow.owner,
            verb="auto_recall",
            title=f"Referral Auto-Recalled: {referral.workflow.title}",
            message=f"The referral to {referral.referred_to.name} has been automatically recalled due to deadline expiration.",
            workflow=referral.workflow,
        )

        # Email notifications
        recipient_emails = [member.email for member in group_members if member.email]
        if referral.workflow.owner.email:
            recipient_emails.append(referral.workflow.owner.email)

        if recipient_emails:
            send_mail(
                subject=f"Referral Auto-Recalled - {referral.workflow.title}",
                message=f"""
Dear User,

This is to inform you that the referral for the workflow '{referral.workflow.title}'
has been automatically recalled due to deadline expiration.

Referral Details:
- Workflow: {referral.workflow.title}
- Referred to: {referral.referred_to.name}
- Deadline was: {referral.deadline.strftime("%Y-%m-%d %H:%M")}
- Auto-recalled: {referral.recalled_at.strftime("%Y-%m-%d %H:%M")}

The workflow is no longer accessible to the referred group.

Best regards,
PWMS System
                """.strip(),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@pwms.com"),
                recipient_list=recipient_emails,
                fail_silently=False,
            )
