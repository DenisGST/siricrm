"""Сигналы login/logout + outgoing message: пишут в EmployeeLog."""
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.models import Employee


def _ip(request):
    if not request:
        return None
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _ua(request):
    return request.META.get("HTTP_USER_AGENT", "")[:1000] if request else ""


def _log(emp, action, description, request=None):
    from apps.core.models import EmployeeLog
    EmployeeLog.objects.create(
        employee=emp, action=action, description=description,
        ip_address=_ip(request), user_agent=_ua(request),
    )


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    emp = Employee.objects.filter(user=user).first()
    if not emp:
        return
    if not emp.is_online:
        emp.is_online = True
        emp.save(update_fields=["is_online"])
    _log(emp, "login", "Вход в систему", request=request)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is None:
        return
    emp = Employee.objects.filter(user=user).first()
    if not emp:
        return
    if emp.is_online:
        emp.is_online = False
        emp.save(update_fields=["is_online"])
    # idle-причину поставит middleware через session['logout_reason'] — иначе
    # это обычный явный logout (кнопка «Выйти»).
    reason = ""
    if request is not None:
        reason = (request.session.pop("logout_reason", "") or "")
    desc = "Выход из системы" + (f" ({reason})" if reason else "")
    _log(emp, "logout", desc, request=request)


_CHANNEL_LABEL = {
    "whatsapp": "WhatsApp",
    "wa": "WhatsApp",
    "telegram": "Telegram",
    "tg": "Telegram",
    "max": "MAX",
}


@receiver(post_save, sender="crm.Message")
def on_message_sent(sender, instance, created, **kwargs):
    """Логируем каждое исходящее сообщение, отправленное сотрудником."""
    if not created:
        return
    if instance.direction != "outgoing":
        return
    if instance.employee_id is None:
        return  # системные/импортные сообщения без автора
    from apps.core.models import EmployeeLog
    channel = _CHANNEL_LABEL.get((instance.channel or "").lower(), instance.channel or "?")
    client = instance.client
    fio = ""
    if client:
        fio = f"{client.last_name} {client.first_name}".strip() or "(без ФИО)"
    desc = f"Отправлено сообщение клиенту в {channel}" + (f": {fio}" if fio else "")
    EmployeeLog.objects.create(
        employee_id=instance.employee_id, action="message_sent",
        description=desc, client=client, message=instance,
    )
