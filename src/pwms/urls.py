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
    # International agreements
    path(
        "workflows/international-agreements/",
        views.international_agreements,
        name="international_agreements",
    ),
    path(
        "workflows/international-agreements/new/",
        views.international_agreement_create,
        name="international_agreement_create",
    ),
    path(
        "workflows/international-agreements/<uuid:public_id>/",
        views.international_agreement_detail,
        name="international_agreement_detail",
    ),
    path(
        "workflows/international-agreements/<uuid:public_id>/edit/",
        views.international_agreement_update,
        name="international_agreement_update",
    ),
    path(
        "workflows/international-agreements/<uuid:public_id>/delete/",
        views.international_agreement_delete,
        name="international_agreement_delete",
    ),
    # Bills
    path("workflows/bills/", views.bills, name="bills"),
    path("workflows/bills/new/", views.bill_create, name="bill_create"),
    path(
        "workflows/bills/<uuid:public_id>/",
        views.bill_detail,
        name="bill_detail",
    ),
    path(
        "workflows/bills/<uuid:public_id>/edit/",
        views.bill_update,
        name="bill_update",
    ),
    path(
        "workflows/bills/<uuid:public_id>/versions/new/",
        views.bill_version_create,
        name="bill_version_create",
    ),
    path(
        "workflows/bills/<uuid:public_id>/versions/<uuid:version_public_id>/edit/",
        views.bill_version_update,
        name="bill_version_update",
    ),
    path(
        "workflows/bills/<uuid:public_id>/delete/",
        views.bill_delete,
        name="bill_delete",
    ),
    # User search
    path("user-search/", views.user_search, name="user_search"),
    # Group search for dynamic lookup
    path("group-search/", views.group_search, name="group_search"),
    # Country / city lookup for the engagement location pickers
    path("country-search/", views.country_search, name="country_search"),
    path("city-search/", views.city_search, name="city_search"),
    path("groups/mine/", views.my_groups, name="my_groups"),
    path("groups/all/", views.all_groups, name="all_groups"),
    path("groups/<int:pk>/", views.group_detail, name="group_detail"),
    path("reports/", views.reports, name="reports"),
    path("about/", views.about, name="about"),
    path("contact/", views.contact, name="contact"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
]
