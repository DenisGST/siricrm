"""Почтовый аккаунт АУ для подачи заявок в «Коммерсантъ».

Сотрудник-АУ вводит в профиле только e-mail и пароль. SMTP/IMAP-хосты и порты
выводим по домену адреса — так пользователю не нужно знать про «smtp.yandex.ru:465».

Заявку подаёт сам управляющий со своего ящика (счёт ИД приходит ответом ему же),
поэтому одна учётка обслуживает и отправку (SMTP), и приём (IMAP).

🛑 Пароль хранится на Employee в зашифрованном виде (Fernet). MailAccount держит
   его уже расшифрованным — создавать аккаунт только там, где он тут же уходит в
   SMTP/IMAP (Celery-таска), и не логировать.
"""
from __future__ import annotations

from dataclasses import dataclass

# Известные провайдеры: домен → (smtp_host, imap_host, порт, ssl).
# Порты одинаковые у всех (465/993 + SSL), поэтому в таблице только хосты.
_PROVIDERS = {
    # Яндекс
    "yandex.ru": ("smtp.yandex.ru", "imap.yandex.ru"),
    "ya.ru": ("smtp.yandex.ru", "imap.yandex.ru"),
    "yandex.com": ("smtp.yandex.ru", "imap.yandex.ru"),
    # Mail.ru Group (общие серверы smtp.mail.ru / imap.mail.ru)
    "mail.ru": ("smtp.mail.ru", "imap.mail.ru"),
    "bk.ru": ("smtp.mail.ru", "imap.mail.ru"),
    "inbox.ru": ("smtp.mail.ru", "imap.mail.ru"),
    "list.ru": ("smtp.mail.ru", "imap.mail.ru"),
    "internet.ru": ("smtp.mail.ru", "imap.mail.ru"),
    # Rambler
    "rambler.ru": ("smtp.rambler.ru", "imap.rambler.ru"),
    "lenta.ru": ("smtp.rambler.ru", "imap.rambler.ru"),
    "autorambler.ru": ("smtp.rambler.ru", "imap.rambler.ru"),
    # Gmail (на всякий случай — АУ бывают и там)
    "gmail.com": ("smtp.gmail.com", "imap.gmail.com"),
    "googlemail.com": ("smtp.gmail.com", "imap.gmail.com"),
}


@dataclass
class MailAccount:
    user: str          # логин (= e-mail)
    password: str      # уже расшифрованный
    from_addr: str     # адрес в поле From
    smtp_host: str
    smtp_port: int
    imap_host: str
    imap_port: int
    use_ssl: bool
    label: str         # для сообщений об ошибках (ФИО АУ)

    @property
    def has_imap(self) -> bool:
        return bool(self.imap_host)


def hosts_for_email(email: str) -> tuple[str, str]:
    """(smtp_host, imap_host) по домену адреса.

    Известный провайдер — из таблицы. Неизвестный (корпоративный домен) — частый
    паттерн smtp.<домен>/imap.<домен>; если он не подойдёт, отправка вернёт понятную
    ошибку соединения, и почту нужно будет заводить через известного провайдера.
    """
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    if domain in _PROVIDERS:
        return _PROVIDERS[domain]
    if not domain:
        return "", ""
    return f"smtp.{domain}", f"imap.{domain}"


def account_for_employee(emp) -> MailAccount | None:
    """Собрать почтовый аккаунт из кредов сотрудника-АУ. None — если не настроен."""
    if emp is None or not emp.kommersant_mail_configured:
        return None
    password = emp.get_kommersant_password()
    if not password:
        return None
    email = emp.kommersant_email.strip()
    smtp_host, imap_host = hosts_for_email(email)
    label = (emp.user.get_full_name() or emp.user.username) if emp.user_id else "АУ"
    return MailAccount(
        user=email, password=password, from_addr=email,
        smtp_host=smtp_host, smtp_port=465,
        imap_host=imap_host, imap_port=993,
        use_ssl=True, label=label,
    )


def account_for_manager(am) -> MailAccount | None:
    """Почтовый аккаунт по арбитражному управляющему (через привязанного сотрудника).

    Внешний АУ без учётной записи сотрудника почту в CRM не заводит — заявку от его
    имени через систему не подать, вернём None (UI подскажет привязать сотрудника).
    """
    if am is None or not am.employee_id:
        return None
    return account_for_employee(am.employee)
