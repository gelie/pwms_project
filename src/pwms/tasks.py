"""Background tasks.

``django-background-tasks`` is installed and configured (``background_task`` in
``INSTALLED_APPS``, ``MAX_ATTEMPTS`` / ``BACKGROUND_TASK_RUN_ASYNC`` set), and
its worker — ``manage.py process_tasks`` — discovers ``tasks.py`` inside each
installed app. This module is therefore what the worker sees, and it is the only
place that knows the queue exists: the work itself lives in the modules that own
it, so the same code runs from a request, a management command, a test or the
worker.

Two jobs are queued here:

* **alert email** — one task per ``Notification`` email row, queued by
  ``pwms.notifications.dispatch`` inside the transaction that wrote the alert
  (see :func:`queue_notification_email`);
* **scheduled report shares** — one repeating drain queued once with
  ``manage.py send_scheduled_report_shares --queue`` (see
  :func:`queue_scheduled_report_shares`).
"""

from background_task import background
from background_task.models import Task

from .notifications.dispatch import deliver_email
from .reporting import send_scheduled_shares

#: How often a queued scheduled-share drain re-runs.
DRAIN_INTERVAL = Task.HOURLY

#: ``Task.verbose_name`` is a ``CharField(max_length=255)``.
VERBOSE_NAME_LIMIT = 255


# -- alert email -------------------------------------------------------------
@background(schedule=0)
def deliver_notification_email(notification_id):
    """
    Deliver one queued alert email.

    ``deliver_email`` raises a retryable failure, which is how the queue learns
    to run this task again (its ``MAX_ATTEMPTS`` and backoff do the timing). Once
    the attempts are spent the row is already marked ``failed``, so the last run
    returns quietly.
    """
    return deliver_email(notification_id)


def queue_notification_email(notification_id, *, verbose_name=""):
    """
    Queue one alert email for the worker and return the queued :class:`Task`.

    Called from inside the transaction that wrote the alert, so the task commits
    with the row it delivers — and rolls back with it.
    """
    return deliver_notification_email(
        notification_id,
        schedule=0,
        verbose_name=(verbose_name or "Alert email")[:VERBOSE_NAME_LIMIT],
    )


# -- scheduled report shares -------------------------------------------------
@background(schedule=0)
def deliver_scheduled_report_shares():
    """Email every due scheduled report share. Returns the delivery summary."""
    return send_scheduled_shares()


def queue_scheduled_report_shares(*, repeat=DRAIN_INTERVAL):
    """
    Queue the scheduled-share drain and return the queued :class:`Task`.

    The task repeats every ``repeat`` seconds until the queue is cleared, so once
    queued a worker keeps scheduled shares going without further help. Queue it
    with ``manage.py send_scheduled_report_shares --queue``.
    """
    return deliver_scheduled_report_shares(
        schedule=0,
        repeat=repeat,
        verbose_name="Deliver scheduled report shares",
    )
