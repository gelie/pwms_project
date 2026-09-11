from django.urls import path

from . import views

app_name = "pwms"

urlpatterns = [
    path("", views.index, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("workflows/", views.workflows, name="workflows"),
    # Delegation reports
    path(
        "workflows/delegation-reports/",
        views.delegation_reports,
        name="delegation_reports",
    ),
    path(
        "workflows/delegation-reports/new/",
        views.delegation_report_create,
        name="delegation_report_create",
    ),
    path(
        "workflows/delegation-reports/<uuid:public_id>/",
        views.delegation_report_detail,
        name="delegation_report_detail",
    ),
    path(
        "workflows/delegation-reports/<uuid:public_id>/edit/",
        views.delegation_report_update,
        name="delegation_report_update",
    ),
    path(
        "workflows/delegation-reports/<uuid:public_id>/delete/",
        views.delegation_report_delete,
        name="delegation_report_delete",
    ),
    # International resolutions
    path(
        "workflows/international-resolutions/",
        views.international_resolutions,
        name="international_resolutions",
    ),
    path(
        "workflows/international-resolutions/new/",
        views.international_resolution_create,
        name="international_resolution_create",
    ),
    path(
        "workflows/international-resolutions/<uuid:public_id>/",
        views.international_resolution_detail,
        name="international_resolution_detail",
    ),
    path(
        "workflows/international-resolutions/<uuid:public_id>/edit/",
        views.international_resolution_update,
        name="international_resolution_update",
    ),
    path(
        "workflows/international-resolutions/<uuid:public_id>/delete/",
        views.international_resolution_delete,
        name="international_resolution_delete",
    ),
    path("groups/mine/", views.my_groups, name="my_groups"),
    path("groups/all/", views.all_groups, name="all_groups"),
    path("groups/<int:pk>/", views.group_detail, name="group_detail"),
    path("reports/", views.reports, name="reports"),
    path("about/", views.about, name="about"),
    path("contact/", views.contact, name="contact"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
]
