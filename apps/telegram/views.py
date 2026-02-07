import json
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from telegram import Update

from .bot import application
import logging



logger = logging.getLogger(__name__)

@csrf_exempt
@require_http_methods(["POST"])
async def telegram_webhook(request):

    
    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

     # безопасно вызвать initialize() каждый раз — если уже инициализировано, он просто сразу вернётся
    
    await application.initialize()

    # Потом создаём Update с привязанным bot
    update = Update.de_json(data, application.bot)

    # И только потом обрабатываем
    await application.process_update(update)

    return JsonResponse({"ok": True})