from django.shortcuts import render


def index(request):
    return render(request, "pwms/index.html")


def login_view(request):
    return render(request, "pwms/login.html")


def logout_view(request):
    return render(request, "pwms/logout.html")
