# apps/crm/telegram_bridge.py
import concurrent.futures

from asgiref.sync import async_to_sync
from telegram.ext import Application

from apps.crm.models import Client
from apps.core.models import Employee

from apps.telegram_bot.handlers import send_text_from_crm, application  # путь подправь под свой проект


def send_text_from_crm_sync(
    *,
    client: Client,
    text: str,
    employee: Employee | None = None,
) -> None:
    """
    Синхронная обёртка для вызова async send_text_from_crm из Django-view.
    """
    app: Application | None = application
    if app is None or app.bot is None or app.bot.loop is None:
        # бот ещё не поднялся — можно просто сохранить сообщение без отправки
        return

    loop = app.bot.loop

    future = concurrent.futures.Future()

    async def _run():
        try:
            await send_text_from_crm(client=client, text=text, employee=employee)
            future.set_result(True)
        except Exception as e:
            future.set_exception(e)

    loop.call_soon_threadsafe(lambda: loop.create_task(_run()))
    # ждём результат, чтобы при ошибке не молчать
    future.result(timeout=5)