"""Helpers for creating default client folder structures."""
from .models import ClientFolder


def _mk(client, parent, name, slug, order=0):
    # 🛑 НЕ используем get_or_create: у части клиентов задвоены папки (напр. две
    # slug="root" после объединения карточек) → get_or_create падает с
    # MultipleObjectsReturned и ломает любое прикрепление файла (договор/иск/скан).
    # Устойчиво: берём самую старую подходящую папку, иначе создаём.
    qs = ClientFolder.objects.filter(client=client, slug=slug)
    if not slug:
        # Пустой slug — уточняем по имени/родителю, чтобы не схлопнуть разные
        # именованные папки клиента.
        qs = qs.filter(name=name, parent=parent)
    existing = qs.order_by("created_at").first()
    if existing is not None:
        return existing
    return ClientFolder.objects.create(
        client=client, parent=parent, name=name, slug=slug, order=order)


def get_or_create_root(client):
    return _mk(client, None, f"{client.last_name} {client.first_name}", "root", 0)


def create_default_folders(client):
    """Корневая папка + Чат + Личные."""
    root     = get_or_create_root(client)
    chat     = _mk(client, root,  "Чат",           "chat",          0)
    _mk(client, chat,  "Отправленные",  "chat_sent",     0)
    _mk(client, chat,  "Полученные",    "chat_received", 1)
    _mk(client, root,  "Личные",        "personal",      1)
    return root


def create_bfl_folders(client):
    """Папка БФЛ со всей вложенной структурой."""
    root = get_or_create_root(client)
    bfl  = _mk(client, root, "БФЛ", "bfl", 2)
    _mk(client, bfl, "Ввод", "bfl_vvod", 0)

    restr = _mk(client, bfl, "Реструктуризация", "bfl_restr", 1)
    _mk(client, restr, "Исходящие",         "bfl_restr_out",  0)
    _mk(client, restr, "Входящие",          "bfl_restr_in",   1)
    _mk(client, restr, "СК",                "bfl_restr_sk",   2)
    _mk(client, restr, "Квартальные отчёты","bfl_restr_qrep", 3)

    real = _mk(client, bfl, "Реализация", "bfl_real", 2)
    _mk(client, real, "Исходящие",         "bfl_real_out",  0)
    _mk(client, real, "Входящие",          "bfl_real_in",   1)
    _mk(client, real, "СК",                "bfl_real_sk",   2)
    _mk(client, real, "Торги",             "bfl_real_torgi",3)
    _mk(client, real, "Завершение",        "bfl_real_end",  4)
    _mk(client, real, "Квартальные отчёты","bfl_real_qrep", 5)

    return bfl


def build_tree(client):
    """Возвращает список корневых папок с вложенными _children (без N+1).

    Каждой папке проставляется files_count — число файлов во всём её
    поддереве (включая вложенные папки), чтобы счётчик был виден и у
    свёрнутых родительских папок.
    """
    from django.db.models import Count
    folders = list(
        ClientFolder.objects.filter(client=client)
        .annotate(_own_files=Count("files"))
        .order_by("order", "name")
    )
    by_id = {f.pk: f for f in folders}
    for f in folders:
        f._children = []
    roots = []
    for f in folders:
        if f.parent_id:
            p = by_id.get(f.parent_id)
            if p:
                p._children.append(f)
        else:
            roots.append(f)

    # Рекурсивно суммируем файлы по поддереву.
    def _count(folder):
        total = folder._own_files
        for child in folder._children:
            total += _count(child)
        folder.files_count = total
        return total

    for r in roots:
        _count(r)
    return roots


def get_folder_path(folder):
    """Возвращает список папок от корня до текущей (breadcrumb)."""
    path = []
    cur = folder
    while cur:
        path.insert(0, cur)
        cur = cur.parent
    return path


def get_chat_folder(client, direction):
    """direction: 'sent' | 'received'"""
    slug = "chat_sent" if direction == "sent" else "chat_received"
    try:
        return ClientFolder.objects.get(client=client, slug=slug)
    except ClientFolder.DoesNotExist:
        create_default_folders(client)
        return ClientFolder.objects.get(client=client, slug=slug)
