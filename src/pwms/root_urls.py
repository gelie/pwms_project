"""
Root URL configuration for the pwms project (single top-level ``pwms`` package).

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView

from pwms.api.api import api as ninja_api

# Django >= 2 rejects the 3-tuple Ninja's ``api.urls`` returns, so unpack it.
_api_patterns, _api_app_name, _api_namespace = ninja_api.urls

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="pwms/", permanent=False)),
    path("pwms/", include("pwms.urls")),
    # Partner-facing Django-Ninja API (users, memberships, bills). The old DRF
    # API that lived under /api/ has been retired.
    path(
        settings.EXTERNAL_API_PREFIX.lstrip("/"),
        include((_api_patterns, _api_app_name), namespace=_api_namespace),
    ),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT) + static(
    settings.STATIC_URL, document_root=settings.STATIC_ROOT
)
