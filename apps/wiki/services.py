"""Дерево оглавления и поиск по руководству."""
from django.db.models import Q

from .markdown_render import plain_text
from .models import WikiArticle

SNIPPET_LEN = 160


def _visible_qs(can_edit: bool):
    qs = WikiArticle.objects.all()
    if not can_edit:
        qs = qs.filter(is_published=True)
    return qs


def build_tree(can_edit: bool = False):
    """Дерево оглавления со сквозной нумерацией.

    Возвращает список узлов-словарей: {article, number, children}. Читаем ВСЁ
    дерево одним запросом и собираем в памяти — статей руководства десятки, а
    рекурсивный обход по .children дал бы N+1.
    """
    articles = list(_visible_qs(can_edit).order_by("order", "title"))
    by_parent: dict = {}
    for a in articles:
        by_parent.setdefault(a.parent_id, []).append(a)

    def walk(parent_id, prefix):
        out = []
        for i, art in enumerate(by_parent.get(parent_id, []), start=1):
            number = f"{prefix}{i}"
            out.append({
                "article": art,
                "number": number,
                "children": walk(art.pk, f"{number}."),
            })
        return out

    return walk(None, "")


def flatten(tree):
    """Дерево → плоский список узлов в порядке чтения (для «След./Пред.»)."""
    out = []
    for node in tree:
        out.append(node)
        out.extend(flatten(node["children"]))
    return out


def number_for(article, can_edit: bool = False) -> str:
    """Номер конкретной статьи («1.2»). Пустая строка, если не нашли."""
    for node in flatten(build_tree(can_edit)):
        if node["article"].pk == article.pk:
            return node["number"]
    return ""


def siblings_neighbours(article, can_edit: bool = False):
    """Предыдущая и следующая статьи в порядке чтения."""
    seq = flatten(build_tree(can_edit))
    for i, node in enumerate(seq):
        if node["article"].pk == article.pk:
            prev = seq[i - 1]["article"] if i > 0 else None
            nxt = seq[i + 1]["article"] if i + 1 < len(seq) else None
            return prev, nxt
    return None, None


def search(query: str, can_edit: bool = False, limit: int = 30):
    """Поиск по заголовку и тексту. Совпадение в заголовке — выше.

    icontains, без Postgres FTS: руководство — это десятки статей, полнотекстовый
    индекс тут ничего не ускорит, а стемминг русского потребовал бы отдельной
    конфигурации словаря.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []

    qs = _visible_qs(can_edit).filter(Q(title__icontains=q) | Q(body__icontains=q))
    numbers = {n["article"].pk: n["number"] for n in flatten(build_tree(can_edit))}

    results = []
    for art in qs[:limit]:
        in_title = q.lower() in art.title.lower()
        results.append({
            "article": art,
            "number": numbers.get(art.pk, ""),
            "snippet": _snippet(art.body, q),
            "in_title": in_title,
        })
    results.sort(key=lambda r: (not r["in_title"], r["number"]))
    return results


def _snippet(body: str, q: str) -> str:
    """Кусок текста вокруг первого совпадения."""
    text = plain_text(body)
    if not text:
        return ""
    pos = text.lower().find(q.lower())
    if pos < 0:
        return text[:SNIPPET_LEN] + ("…" if len(text) > SNIPPET_LEN else "")
    start = max(0, pos - SNIPPET_LEN // 3)
    end = min(len(text), pos + SNIPPET_LEN)
    out = text[start:end]
    return ("…" if start > 0 else "") + out + ("…" if end < len(text) else "")
