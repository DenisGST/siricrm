"""Views руководства пользователя.

Руководство открывается в ОТДЕЛЬНОЙ вкладке (иконка «?» в шапке), поэтому это
самостоятельные страницы со своим шаблоном-шеллом, а не HTMX-своп в
#content-area, как остальные разделы. Навигация по дереву — обычными ссылками:
у каждой статьи свой URL (можно дать коллеге ссылку на конкретный раздел,
работает «назад» и Ctrl+F по странице). HTMX используется точечно — живой
поиск и предпросмотр в редакторе.

Читают все авторизованные (@login_required), правят — can_edit_wiki.
"""
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.permissions import get_employee

from . import services
from .markdown_render import render_markdown
from .models import WikiArticle
from .permissions import can_edit_wiki, require_wiki_edit


def _base_ctx(request, current=None):
    """Общий контекст левой колонки: дерево + подсветка/раскрытие текущей ветки."""
    can_edit = can_edit_wiki(request.user)
    return {
        "can_edit": can_edit,
        "tree": services.build_tree(can_edit),
        "current_id": current.pk if current else None,
        # Ветка раскрыта, если внутри неё текущая статья → её предки + она сама.
        "open_ids": (
            [a.pk for a in current.ancestors] + [current.pk] if current else []
        ),
    }


@login_required
def index(request):
    """Корень руководства — открываем первую статью, если она есть."""
    ctx = _base_ctx(request)
    if ctx["tree"]:
        return redirect(ctx["tree"][0]["article"].get_absolute_url())
    return render(request, "wiki/panel.html", {**ctx, "article": None})


@login_required
def article(request, slug):
    can_edit = can_edit_wiki(request.user)
    qs = WikiArticle.objects.all() if can_edit else WikiArticle.objects.filter(is_published=True)
    art = get_object_or_404(qs, slug=slug)
    prev_art, next_art = services.siblings_neighbours(art, can_edit)
    return render(request, "wiki/panel.html", {
        **_base_ctx(request, current=art),
        "article": art,
        "article_html": render_markdown(art.body),
        "number": services.number_for(art, can_edit),
        "prev_article": prev_art,
        "next_article": next_art,
    })


@login_required
def search_results(request):
    """HTMX-партиал левой колонки: результаты поиска, а на пустой запрос — дерево.

    `cur` — slug открытой статьи: без него после очистки поиска дерево
    вернулось бы без подсветки и со схлопнутой веткой.
    """
    q = (request.GET.get("q") or "").strip()
    current = WikiArticle.objects.filter(slug=request.GET.get("cur") or "").first()
    return render(request, "wiki/partials/_search_results.html", {
        **_base_ctx(request, current=current),
        "q": q,
        "results": services.search(q, can_edit_wiki(request.user)),
    })


# ---------------------------------------------------------------- редактор


@login_required
@require_wiki_edit
def article_new(request):
    if request.method == "POST":
        return _save(request, WikiArticle())
    parent_id = request.GET.get("parent") or None
    initial = WikiArticle(parent_id=parent_id) if parent_id else WikiArticle()
    return render(request, "wiki/editor.html", {
        **_base_ctx(request),
        "article": initial,
        "is_new": True,
        "parents": _parent_choices(None),
    })


@login_required
@require_wiki_edit
def article_edit(request, slug):
    art = get_object_or_404(WikiArticle, slug=slug)
    if request.method == "POST":
        return _save(request, art)
    return render(request, "wiki/editor.html", {
        **_base_ctx(request, current=art),
        "article": art,
        "is_new": False,
        "number": services.number_for(art, True),
        "parents": _parent_choices(art),
    })


@login_required
@require_wiki_edit
def preview(request):
    """HTMX-партиал: живой предпросмотр markdown в редакторе."""
    html = render_markdown(request.POST.get("body") or "")
    return render(request, "wiki/partials/_preview.html", {"article_html": html})


def _parent_choices(article):
    """Возможные родители: все статьи, кроме самой себя и своих потомков.

    Иначе можно было бы сделать статью потомком собственного ребёнка и
    получить оторванный от корня цикл в дереве.
    """
    nodes = services.flatten(services.build_tree(True))
    if article is None or not article.pk:
        return nodes
    banned = {article.pk}
    for node in nodes:  # nodes идут в порядке чтения → родитель всегда раньше ребёнка
        if node["article"].parent_id in banned:
            banned.add(node["article"].pk)
    return [n for n in nodes if n["article"].pk not in banned]


def _save(request, art):
    title = (request.POST.get("title") or "").strip()
    if not title:
        return HttpResponseBadRequest("Заголовок обязателен")

    parent_id = request.POST.get("parent") or None
    if parent_id:
        allowed = {n["article"].pk for n in _parent_choices(art)}
        if int(parent_id) not in allowed:
            return HttpResponseBadRequest("Нельзя сделать статью потомком самой себя")

    is_new = art.pk is None
    art.title = title
    art.parent_id = parent_id
    art.body = request.POST.get("body") or ""
    art.is_published = request.POST.get("is_published") == "on"

    slug = (request.POST.get("slug") or "").strip()
    if slug:
        art.slug = slug

    if is_new:
        # В конец своего уровня.
        siblings = WikiArticle.objects.filter(parent_id=parent_id)
        art.order = (max((s.order for s in siblings), default=0)) + 10

    art.updated_by = get_employee(request.user)
    art.save()
    return redirect(art.get_absolute_url())


@login_required
@require_wiki_edit
@require_POST
def article_delete(request, slug):
    art = get_object_or_404(WikiArticle, slug=slug)
    parent = art.parent
    art.delete()  # CASCADE унесёт и подразделы — в шаблоне об этом предупреждаем
    return redirect(parent.get_absolute_url() if parent else "wiki:index")


@login_required
@require_wiki_edit
@require_POST
def article_move(request, slug):
    """Сдвинуть статью на уровне вверх/вниз — меняемся order с соседом."""
    art = get_object_or_404(WikiArticle, slug=slug)
    direction = request.POST.get("dir")
    if direction not in ("up", "down"):
        return HttpResponseBadRequest("dir must be up/down")

    siblings = list(
        WikiArticle.objects.filter(parent_id=art.parent_id).order_by("order", "title")
    )
    idx = next((i for i, s in enumerate(siblings) if s.pk == art.pk), None)
    if idx is None:
        return redirect(art.get_absolute_url())

    swap_idx = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap_idx < len(siblings):
        # order у соседей мог совпадать (порядок добирался по title) — обменяться
        # значениями тогда недостаточно, перенумеровываем уровень целиком.
        siblings[idx], siblings[swap_idx] = siblings[swap_idx], siblings[idx]
        for i, s in enumerate(siblings, start=1):
            new_order = i * 10
            if s.order != new_order:
                s.order = new_order
                s.save(update_fields=["order"])

    return redirect(art.get_absolute_url())
