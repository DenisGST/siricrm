"""Рендер markdown-тела статьи в безопасный HTML.

Статьи пишут доверенные сотрудники (is_references_access), но санитайзим
всё равно: скомпрометированный админ-аккаунт иначе получил бы stored XSS на
каждого сотрудника, открывшего руководство. python-markdown пропускает сырой
HTML насквозь, поэтому bleach обязателен, а не «на всякий случай».
"""
import bleach
import markdown
from django.utils.safestring import mark_safe

# Теги, разрешённые в статье. Осознанно НЕТ: script, style, iframe, form,
# input, object, embed.
ALLOWED_TAGS = [
    "p", "br", "hr", "div", "span",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "u", "del", "s", "mark", "small", "sup", "sub",
    "ul", "ol", "li",
    "blockquote", "pre", "code", "kbd",
    "a", "img",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "details", "summary",
]

ALLOWED_ATTRS = {
    "*": ["class", "id", "title"],
    "a": ["href", "target", "rel"],
    "img": ["src", "alt", "width", "height", "loading"],
    "th": ["colspan", "rowspan", "align"],
    "td": ["colspan", "rowspan", "align"],
    "ol": ["start"],
}

ALLOWED_PROTOCOLS = ["http", "https", "mailto", "tel"]

MD_EXTENSIONS = [
    "extra",        # таблицы, fenced code, сноски, списки определений
    "sane_lists",
    "nl2br",        # перевод строки = <br>, как ждёт обычный пользователь
    "admonition",   # блоки-врезки !!! note "Заголовок"
    "toc",          # якоря у заголовков — нужны для ссылок вида /wiki/a/slug/#zagolovok
]


def render_markdown(text: str) -> str:
    """Markdown → санитизированный HTML. Возвращает safe-строку для шаблона."""
    if not text:
        return ""
    html = markdown.markdown(
        text,
        extensions=MD_EXTENSIONS,
        extension_configs={"toc": {"permalink": False}},
        output_format="html",
    )
    clean = bleach.clean(
        html,
        tags=set(ALLOWED_TAGS),
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    # Внешние ссылки — в новую вкладку и без utm-утечки referrer'а.
    clean = bleach.linkifier.Linker(
        callbacks=[_external_link_attrs],
        skip_tags=["pre", "code"],
        parse_email=False,
    ).linkify(clean)
    return mark_safe(clean)


def _external_link_attrs(attrs, new=False):
    href = attrs.get((None, "href"), "")
    if href.startswith("http://") or href.startswith("https://"):
        attrs[(None, "target")] = "_blank"
        attrs[(None, "rel")] = "noopener noreferrer"
    return attrs


def plain_text(text: str, limit: int = 0) -> str:
    """Markdown → голый текст. Для сниппетов в результатах поиска."""
    if not text:
        return ""
    html = markdown.markdown(text, extensions=["extra"], output_format="html")
    txt = bleach.clean(html, tags=set(), attributes={}, strip=True)
    txt = " ".join(txt.split())
    if limit and len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0] + "…"
    return txt
