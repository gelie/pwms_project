from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

app_name = "pwms_api"

# SimpleRouter (not DefaultRouter) on purpose: the project keeps its own hand-written
# ``api_root`` at ``/api/`` — including the ``api-root`` URL name the existing tests
# reverse — so the router must not install a competing root view.
router = SimpleRouter()
router.register("users", views.UserViewSet, basename="user")
router.register("groups", views.GroupViewSet, basename="group")
router.register("roles", views.RoleViewSet, basename="role")
router.register("memberships", views.MembershipViewSet, basename="membership")
router.register("workflow-types", views.WorkflowTypeViewSet, basename="workflow-type")
router.register("states", views.StateViewSet, basename="state")
router.register("transitions", views.TransitionViewSet, basename="transition")
router.register(
    "delegation-reports", views.DelegationReportViewSet, basename="delegation-report"
)
router.register(
    "resolutions",
    views.InternationalResolutionViewSet,
    basename="resolution",
)
router.register("agreements", views.InternationalAgreementViewSet, basename="agreement")
router.register("bills", views.BillViewSet, basename="bill")
router.register("meetings", views.MeetingViewSet, basename="meeting")

urlpatterns = [
    path("", views.api_root, name="api-root"),
    # Token lifecycle. Login/logout for the browsable API stay under /api/auth/
    # too (rest_framework.urls, included from pwms.root_urls).
    path("auth/token/", views.ObtainTokenView.as_view(), name="obtain-token"),
    path("auth/token/refresh/", views.RefreshTokenView.as_view(), name="refresh-token"),
    path("auth/token/revoke/", views.RevokeTokenView.as_view(), name="revoke-token"),
    # Audit-history routes kept as standalone views (keyed by public_id), retained
    # for the original API contract; each instance viewset also exposes
    # ``/{public_id}/audit/`` as an action.
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
    path(
        "bills/<uuid:public_id>/audit/",
        views.BillAuditHistoryView.as_view(),
        name="bill-audit-history",
    ),
    *router.urls,
]
