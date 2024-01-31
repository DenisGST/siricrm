from django.http import HttpResponse
from django.shortcuts import render

from goods.models import Categories

def index(request):

    categories = Categories.objects.all()

    context: dict[str, str] = {
        'title': 'SiriCRM - Главная',
        'content': 'Система управления SiriCRM',
        'categories': categories
        
    }

    return render(request,'main/index.html', context)


def about(request):
    context: dict[str, str] = {
        'title': 'SiriCRM - Про SiriCRM',
        'content': "Про SiriCRM",
        'text_on_page': "SiriCRM - Система управления делами юридической компании."
        
    }

    return render(request,'main/about.html', context)

