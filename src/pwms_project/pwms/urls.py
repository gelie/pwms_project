from django.urls import path

from . import views

app_name = "pwms"

urlpatterns = [
    path("", views.index, name="home"),
    path("about/", views.index, name="about"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
]
