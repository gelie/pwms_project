from django.urls import path

from . import views

app_name = "pwms_api"

urlpatterns = [
    path("", views.api_root, name="api-root"),
    path(
        "resolutions/<uuid:public_id>/audit/",
        views.ResolutionAuditHistoryView.as_view(),
        name="resolution-audit-history",
    ),
    path(
        "agreements/<uuid:public_id>/audit/",
        views.AgreementAuditHistoryView.as_view(),
        name="agreement-audit-history",
    ),
]
