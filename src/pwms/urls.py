from django.urls import path

from . import views

app_name = "pwms"

urlpatterns = [
    path("", views.index, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("workflows/", views.workflows, name="workflows"),
    path("groups/", views.groups, name="groups"),
    path("reports/", views.reports, name="reports"),
    path("about/", views.about, name="about"),
    path("contact/", views.contact, name="contact"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
]
