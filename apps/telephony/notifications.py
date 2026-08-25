"""Карточки «вам звонили» — создание, обновление и пуш сотруднику.

Идём тем же путём, что остальные оповещения проекта: в личную группу
``user_notifications_{user_id}`` уходит HTML-фрагмент, а JS в ``dashboard.html``
на него реагирует (как с ``data-toast``, ``data-chat-message``).

🛑 Карточка хранится в базе (``IncomingCallAlert``), а не только в разметке:
смысл её в том, что человек отошёл и не взял трубку, а любое обновление
страницы стирало бы напоминание именно тогда, когда оно нужнее всего.
"""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.template.loader import render_to_string

from .models import IncomingCallAlert

logger = logging.getLogger(__name__)


def _send(employee, html: str):
    if employee is None or not getattr(employee, "user_id", None):
        return
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            f"user_notifications_{employee.user_id}",
            {"type": "notify", "html": html},
        )
    except Exception:
        # Оповещение не должно ронять слушателя событий АТС.
        logger.debug("не удалось отправить карточку звонка", exc_info=True)


def register_incoming_call(employee, *, channel_key: str, phone: str, client=None,
                           missed: bool = False):
    """Записать входящий и показать карточку. → IncomingCallAlert.

    Идемпотентно по паре «сотрудник + нога звонка»: повторный DialBegin
    (перезвон в том же канале) не плодит карточки.

    missed=True — карточка сразу рисуется как пропущенный. Так её ставит
    реестр пропущенных (``missed._push_cards``): звонок на группу мог вообще
    не дойти до конкретной трубки (оборвался в голосовом меню), и события
    DialBegin по нему не было — а показать обращение всё равно надо.
    """
    alert, created = IncomingCallAlert.objects.get_or_create(
        employee=employee, channel_key=channel_key,
        defaults={"phone": phone or "", "client": client,
                  "finished": missed, "answered": False},
    )
    if not created:
        return alert
    _send(employee, render_to_string(
        "telephony/partials/_call_alert_push.html", {"alert": alert}))
    return alert


def finish_incoming_call(employee, *, channel_key: str, answered: bool):
    """Звонок завершился: отметить итог и перерисовать карточку.

    🛑 Карточку НЕ убираем — она висит, пока сотрудник сам не нажмёт «Убрать».
    В этом весь смысл: человек мог отойти, и вернувшись должен увидеть, что
    ему звонили. Меняется только вид: пропущенный подсвечивается красным.
    """
    alert = (IncomingCallAlert.objects
             .filter(employee=employee, channel_key=channel_key, dismissed_at__isnull=True)
             .first())
    if alert is None:
        return None
    if alert.finished and alert.answered == answered:
        return alert
    alert.finished = True
    alert.answered = answered
    alert.save(update_fields=["finished", "answered"])
    _send(employee, render_to_string(
        "telephony/partials/_call_alert_push.html", {"alert": alert}))
    return alert
