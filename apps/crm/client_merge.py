"""Объединение карточек-дублей клиентов.

Используется кнопкой «Объединить» в карточке клиента (право
``apps.core.permissions.can_merge_clients``).

Поток:
  compare_clients(c1, c2)  → данные для таблицы сравнения (поля + коллекции);
  merge_clients(survivor, other, scalar_take_other, collection_actions)
                           → выполняет слияние в транзакции и удаляет ``other``.

Логика переноса FK повторяет ручную процедуру (см. память
client-merge-procedure): телефоны добавляются как additional с учётом
глобального unique (phone, purpose); файлы переносятся в одноимённую по slug
папку survivor'а; ClientEmployee — unique (client, employee); всё, что не
выбрано явно, безопасно переносится на survivor (ничего не теряем).
"""
import re

from django.db import transaction

from apps.crm.models import (
    Client, ClientPhone, ClientEmployee, ClientLogEntry, Message, Service,
    Address, ClientNameHistory,
)
from apps.finance.models import Payment, Charge
from apps.files.models import ClientFolder, ClientFile


# ── Одиночные поля (выбор Клиент1 / Клиент2) ──────────────────────────────
SCALAR_FIELDS = [
    ("last_name", "Фамилия"),
    ("first_name", "Имя"),
    ("patronymic", "Отчество"),
    ("birth_date", "Дата рождения"),
    ("birth_place", "Место рождения"),
    ("passport_series", "Серия паспорта"),
    ("passport_number", "Номер паспорта"),
    ("passport_issued_by", "Паспорт: кем выдан"),
    ("passport_issued_date", "Паспорт: дата выдачи"),
    ("passport_division_code", "Код подразделения"),
    ("inn", "ИНН"),
    ("snils", "СНИЛС"),
    ("email", "Email"),
    ("username", "Username"),
    ("telegram_id", "Telegram ID"),
    ("max_chat_id", "MAX chat id"),
    ("status", "Статус"),
    ("referral_source", "Источник"),
    ("notes", "Заметки"),
]

# ── Коллекции (выбор Клиент1 / Клиент2 / Объединить) ──────────────────────
# key → (заголовок, callable(client) -> queryset для подсчёта)
COLLECTIONS = [
    ("phones",    "Телефоны",                 lambda c: ClientPhone.objects.filter(client=c)),
    ("services",  "Услуги",                   lambda c: Service.objects.filter(client=c)),
    ("finance",   "Платежи и начисления",     lambda c: Payment.objects.filter(client=c)),
    ("messages",  "Сообщения (чат)",          lambda c: Message.objects.filter(client=c)),
    ("files",     "Файлы",                    lambda c: ClientFile.objects.filter(folder__client=c)),
    ("addresses", "Адреса",                   lambda c: Address.objects.filter(client=c)),
    ("events",    "События и действия (лог)", lambda c: ClientLogEntry.objects.filter(client=c)),
]


def _fmt(value):
    if value is None or value == "":
        return ""
    return str(value)


def _norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else d


def compare_clients(c1, c2):
    """Готовит данные для таблицы сравнения.

    Возвращает dict:
      scalars: [{name, label, v1, v2, differ, default}]  default = 'c1'|'c2'
      collections: [{key, label, n1, n2}]
    """
    scalars = []
    for name, label in SCALAR_FIELDS:
        v1, v2 = getattr(c1, name, None), getattr(c2, name, None)
        s1, s2 = _fmt(v1), _fmt(v2)
        # дефолт: непустое; если оба непустые и различаются — c1 (survivor по умолчанию)
        default = "c1"
        if not s1 and s2:
            default = "c2"
        scalars.append({
            "name": name, "label": label,
            "v1": s1, "v2": s2,
            "differ": s1 != s2,
            "default": default,
        })

    collections = []
    for key, label, getter in COLLECTIONS:
        if key == "finance":
            n1 = Payment.objects.filter(client=c1).count() + Charge.objects.filter(client=c1).count()
            n2 = Payment.objects.filter(client=c2).count() + Charge.objects.filter(client=c2).count()
        else:
            n1 = getter(c1).count()
            n2 = getter(c2).count()
        collections.append({"key": key, "label": label, "n1": n1, "n2": n2})

    # одинаковые услуги (для предупреждения)
    names1 = set(Service.objects.filter(client=c1).values_list("name__short_name", flat=True))
    names2 = set(Service.objects.filter(client=c2).values_list("name__short_name", flat=True))
    dup_services = sorted(names1 & names2)

    return {"scalars": scalars, "collections": collections, "dup_services": dup_services}


# ── Перенос коллекций ─────────────────────────────────────────────────────
def _move_phones(source, survivor):
    """Добавить номера source как additional (unique phone,purpose глобальный)."""
    surv_nums = {_norm_phone(p) for p in
                 ClientPhone.objects.filter(client=survivor).values_list("phone", flat=True)}
    for num in set(ClientPhone.objects.filter(client=source).values_list("phone", flat=True)):
        if _norm_phone(num) not in surv_nums:
            if not ClientPhone.objects.filter(phone=num, purpose="additional").exists():
                ClientPhone.objects.create(client=survivor, phone=num, purpose="additional")
                surv_nums.add(_norm_phone(num))
    ClientPhone.objects.filter(client=source).delete()


def _merge_folder_subtree(canonical, dup):
    """Рекурсивно слить dup-папку в canonical: дети с одинаковым именем сливаются,
    уникальные — перевешиваются, файлы dup переезжают на canonical, dup удаляется."""
    for child in list(dup.children.all()):
        match = ClientFolder.objects.filter(parent=canonical, name=child.name).first()
        if match:
            _merge_folder_subtree(match, child)
        else:
            child.parent = canonical
            child.save(update_fields=["parent"])
    ClientFile.objects.filter(folder=dup).update(folder=canonical)
    dup.delete()


def _move_files(source, survivor):
    """Перенести все папки/файлы source → survivor с рекурсивным слиянием
    одноимённых поддеревьев. Гарантирует наличие корневой папки у survivor
    (иначе ClientFile.folder=None → IntegrityError на NOT NULL).
    """
    from apps.files.folder_utils import get_or_create_root
    # Гарантируем корневую папку у survivor (если её нет — создастся)
    get_or_create_root(survivor)

    # Переключаем все папки source на survivor — теперь у survivor могут быть
    # дубли корней (по 2 root-папки с одинаковым name).
    ClientFolder.objects.filter(client=source).update(client=survivor)

    # Сливаем дубли корней (parent IS NULL) у survivor.
    roots = list(ClientFolder.objects.filter(client=survivor, parent__isnull=True))
    if len(roots) > 1:
        # Canonical — с большим числом детей, иначе самый старый.
        roots.sort(key=lambda r: (-ClientFolder.objects.filter(parent=r).count(), r.created_at))
        canonical = roots[0]
        # Нормализуем имя корня по ФИО survivor (если переименование клиента состоялось).
        target_name = f"{survivor.last_name} {survivor.first_name}".strip()
        if target_name and canonical.name != target_name:
            canonical.name = target_name
            canonical.save(update_fields=["name"])
        for dup in roots[1:]:
            _merge_folder_subtree(canonical, dup)


# модель+поле для «простых» коллекций (reassign / delete)
_SIMPLE = {
    "services":  [(Service, "client")],
    "finance":   [(Payment, "client"), (Charge, "client")],
    "messages":  [(Message, "client")],
    "addresses": [(Address, "client")],
    "events":    [(ClientLogEntry, "client")],
}
_HANDLED_MODELS = {ClientPhone, ClientFolder, ClientEmployee,
                   Service, Payment, Charge, Message, Address, ClientLogEntry}


@transaction.atomic
def merge_clients(survivor, other, *, scalar_take_other=None, collection_actions=None):
    """Слить ``other`` в ``survivor`` и удалить ``other``.

    scalar_take_other: множество имён полей, для которых берём значение OTHER
                       (для остальных остаётся значение survivor).
    collection_actions: {key: 'both'|'survivor'|'other'} — что делать с коллекцией:
        both     — данные обеих карточек (перенести other → survivor);
        survivor — оставить только survivor (данные other по этой коллекции удалить);
        other    — оставить только other  (данные survivor удалить, other перенести).
    """
    scalar_take_other = scalar_take_other or set()
    collection_actions = collection_actions or {}

    # 1) Одиночные поля
    changed = []
    for name, _ in SCALAR_FIELDS:
        if name in scalar_take_other:
            setattr(survivor, name, getattr(other, name))
            changed.append(name)
    if changed:
        survivor.save(update_fields=changed)

    # 2) Коллекции
    def _delete_collection(client, key):
        if key == "phones":
            ClientPhone.objects.filter(client=client).delete()
        elif key == "files":
            ClientFolder.objects.filter(client=client).delete()  # cascade files
        else:
            for model, field in _SIMPLE.get(key, []):
                model.objects.filter(**{field: client}).delete()

    def _move_collection(source, survivor, key):
        if key == "phones":
            _move_phones(source, survivor)
        elif key == "files":
            _move_files(source, survivor)
        else:
            for model, field in _SIMPLE.get(key, []):
                model.objects.filter(**{field: source}).update(**{field: survivor})

    for key, _, _ in COLLECTIONS:
        action = collection_actions.get(key, "both")
        if action == "survivor":
            _delete_collection(other, key)          # выбросить данные other
        elif action == "other":
            _delete_collection(survivor, key)       # выбросить данные survivor
            _move_collection(other, survivor, key)
        else:  # both
            _move_collection(other, survivor, key)

    # 3) Ответственные (unique client+employee)
    for ce in ClientEmployee.objects.filter(client=other):
        if ClientEmployee.objects.filter(client=survivor, employee_id=ce.employee_id).exists():
            ce.delete()
        else:
            ce.client = survivor
            ce.save(update_fields=["client"])

    # 4) spouse-ссылки
    Client.objects.filter(spouse=other).update(spouse=survivor)

    # 5) Всё прочее (не выбранное явно) — безопасно переносим на survivor,
    #    чтобы ничего не потерять (ClientNameHistory, Consultation, Kreditor,
    #    GeneratedDocument, EmployeeLog, IncomingScan, ...).
    for rel in Client._meta.related_objects:
        model, field = rel.related_model, rel.field.name
        if model in _HANDLED_MODELS:
            continue
        if model is Client and field == "spouse":
            continue
        model.objects.filter(**{field: other}).update(**{field: survivor})

    # 6) Защита: на other не должно остаться CASCADE-связей (кроме намеренно
    #    удалённых ClientPhone/ClientFolder).
    leftovers = []
    for rel in Client._meta.related_objects:
        if getattr(getattr(rel, "on_delete", None), "__name__", "") != "CASCADE":
            continue
        if rel.related_model in (ClientPhone, ClientFolder):
            continue
        n = rel.related_model.objects.filter(**{rel.field.name: other}).count()
        if n:
            leftovers.append(f"{rel.related_model.__name__}={n}")
    if leftovers:
        raise RuntimeError(f"Не перенесены связи: {leftovers}")

    other_id = str(other.id)
    other.delete()
    return other_id
