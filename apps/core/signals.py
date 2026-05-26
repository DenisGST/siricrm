"""Сигналы login/logout: пишут в EmployeeLog и проставляют Employee.is_online."""
from django.contrib.auth.signals import user_logged_in, user_logged_out
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
