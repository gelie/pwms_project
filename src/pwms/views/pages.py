import logging

from django.contrib.auth.decorators import login_not_required
from django.shortcuts import render

logger = logging.getLogger(__name__)


@login_not_required
def about(request):
    """About page (public, no sign-in required)."""
    return render(request, "pwms/about.html")


def contact(request):
    """Contact page (sign-in required)."""
    return render(request, "pwms/contact.html")
