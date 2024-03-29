from django.shortcuts import render


def login(request):
    context = {
        'title': 'SiriCRM - Авторизация'
    }
    return render(request, 'users/login.html', context)


def registration(request):
    context = {
        'title': 'SiriCRM - Регистрация'
    }
    return render(request, 'users/registration.html', context)


def profile(request):
    context = {
        'title': 'SiriCRM - Кабинет'
    }
    return render(request, 'users/profile.html', context)


def logout(request):
    ...
