"""
URL configuration for config project.

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

from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from pwms.api.ninja import api as ninja_api
from rest_framework.permissions import AllowAny

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="pwms/", permanent=False)),
    path("pwms/", include("pwms.urls")),
    # Django REST Framework API (audit history for workflow instances)
    path("api/", include("pwms.api.urls")),
    # DRF browsable-API login/logout endpoints
    path("api/auth/", include("rest_framework.urls")),
    # django-ninja spike (same audit endpoint, FastAPI-style) - compare/remove
    path("ninja/", ninja_api.urls),
    # OpenAPI schema + interactive docs (schema/UI are public; data stays auth'd)
    path(
        "api/schema/",
        SpectacularAPIView.as_view(permission_classes=[AllowAny]),
        name="api-schema",
    ),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(
            url_name="api-schema", permission_classes=[AllowAny]
        ),
        name="api-docs",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(
            url_name="api-schema", permission_classes=[AllowAny]
        ),
        name="api-redoc",
    ),
]
