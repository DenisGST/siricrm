"""
Template tags для иконок:
- {% icon "pencil" size=20 %}          — inline Lucide SVG (line)
- {% icon "pencil" size=20 stroke="#6b7280" %}  — с цветом обводки
- {% icon_3d "telegram" size=32 %}     — Fluent Emoji 3D (colored raster PNG)

Ищет SVG в static/icons/line/ и PNG в static/icons/3d/.
"""
from pathlib import Path
from django import template
from django.conf import settings
from django.templatetags.static import static
from django.utils.safestring import mark_safe
from functools import lru_cache

register = template.Library()


def _line_dir() -> Path:
    return Path(settings.BASE_DIR) / "static" / "icons" / "line"


@lru_cache(maxsize=256)
def _read_svg(name: str):
    p = _line_dir() / f"{name}.svg"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


@register.simple_tag
def icon(name, size=16, stroke=None, css_class=""):
    """Inline SVG-иконка Lucide. Цвет наследуется через currentColor."""
    svg = _read_svg(name)
    if svg is None:
        return mark_safe(f"<!-- icon '{name}' not found -->")
    # Удаляем лицензионный комментарий
    svg = "\n".join(l for l in svg.splitlines() if not l.strip().startswith("<!--"))
    # Меняем размер
    svg = svg.replace('width="24"', f'width="{size}"')
    svg = svg.replace('height="24"', f'height="{size}"')
    if css_class:
        svg = svg.replace("<svg ", f'<svg class="{css_class}" ', 1)
    if stroke:
        svg = svg.replace('stroke="currentColor"', f'stroke="{stroke}"')
    return mark_safe(svg)


@register.simple_tag
def icon_3d(name, size=32, css_class=""):
    """Цветная 3D-иконка (Fluent Emoji 3D, PNG)."""
    url = static(f"icons/3d/{name}.png")
    return mark_safe(
        f'<img src="{url}" alt="{name}" width="{size}" height="{size}" '
        f'class="{css_class}" loading="lazy" '
        f'style="display:inline-block;vertical-align:middle;object-fit:contain">'
    )


def _brand_dir() -> Path:
    return Path(settings.BASE_DIR) / "static" / "icons" / "brand"


@lru_cache(maxsize=64)
def _read_brand_svg(name: str):
    p = _brand_dir() / f"{name}.svg"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


BRAND_COLORS = {
    "telegram": "#26A5E4",
    "whatsapp": "#25D366",
    "max": "#FF6B00",
}


@register.simple_tag
def icon_brand(name, size=24, color=None):
    """Брендовый логотип (Telegram, WhatsApp и т.д.). Можно переопределить цвет."""
    svg = _read_brand_svg(name)
    if svg is None:
        return mark_safe(f"<!-- brand icon '{name}' not found -->")
    final_color = color or BRAND_COLORS.get(name, "currentColor")
    svg = svg.replace("<svg ", f'<svg width="{size}" height="{size}" fill="{final_color}" ', 1)
    return mark_safe(svg)


@register.simple_tag
def sidebar_icon(icon_value, size=22):
    """
    Умный рендер иконки для sidebar MenuItem.
    - Если icon_value — имя из static/icons/line/ → inline Lucide SVG
    - Если icon_value — имя из static/icons/brand/ с префиксом "brand:" → брендовая SVG
    - Иначе отдаём как есть (эмодзи / текст)
    """
    if not icon_value:
        return ""
    val = icon_value.strip()
    if val.startswith("brand:"):
        return icon_brand(val[6:], size=size)
    # Проверяем наличие SVG в line/
    if _read_svg(val) is not None:
        return icon(val, size=size)
    # Fallback — эмодзи / текст как есть
    return mark_safe(val)
