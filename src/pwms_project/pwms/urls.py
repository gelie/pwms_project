from django.urls import path

from . import views

app_name = "pwms"

urlpatterns = [
    path("", views.index),
]
