from django import template

register = template.Library()


@register.filter
def children(folder):
    """Возвращает _children (предзагруженные дочерние папки)."""
    return getattr(folder, "_children", [])


@register.filter
def has_children(folder):
    return bool(getattr(folder, "_children", []))


_ICONS = {
    "pdf":  "📄", "doc": "📝", "docx": "📝", "xls": "📊", "xlsx": "📊",
    "ppt":  "📊", "pptx": "📊", "txt": "📃", "csv": "📃",
    "jpg":  "🖼", "jpeg": "🖼", "png": "🖼", "gif": "🖼", "webp": "🖼",
    "mp4":  "🎬", "avi": "🎬", "mov": "🎬",
    "mp3":  "🎵", "wav": "🎵",
    "zip":  "🗜", "rar": "🗜", "7z": "🗜",
    "sig":  "🔐",
}

@register.filter
def file_icon(filename):
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return _ICONS.get(ext, "📎")
