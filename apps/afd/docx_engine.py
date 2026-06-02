"""Движок заполнения .docx-шаблонов плейсхолдерами вида {key}.

Особенность Word: один логический текст («{дата рождения}») часто разбит на
несколько run'ов (<w:r>) из-за правок/проверки орфографии. Поэтому простой
поиск-замена по run'у не сработает. Решение: для каждого абзаца склеиваем
текст всех run'ов, выполняем замену, и записываем результат в первый run,
очищая остальные (формат первого run'а сохраняется).
"""
import io
import re

from docx import Document

_PLACEHOLDER_RE = re.compile(r"\{[^{}\n]+\}")


def _iter_paragraphs(container):
    """Рекурсивно обходит все абзацы документа, включая абзацы в таблицах."""
    for para in getattr(container, "paragraphs", []):
        yield para
    for table in getattr(container, "tables", []):
        for row in table.rows:
            for cell in row.cells:
                yield from _iter_paragraphs(cell)


def _replace_in_paragraph(paragraph, context):
    runs = paragraph.runs
    if not runs:
        return
    full = "".join(r.text for r in runs)
    if "{" not in full:
        return
    if not _PLACEHOLDER_RE.search(full):
        return

    def _sub(m):
        key = m.group(0)[1:-1]  # без фигурных скобок
        if key in context:
            val = context[key]
            return "" if val is None else str(val)
        return m.group(0)  # неизвестный плейсхолдер оставляем как есть

    new_text = _PLACEHOLDER_RE.sub(_sub, full)
    if new_text == full:
        return
    # Записываем всё в первый run, остальные очищаем.
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ""


def render_docx(template_bytes: bytes, context: dict) -> bytes:
    """Возвращает bytes .docx с подставленными значениями.

    context: {"placeholder_key": "value", ...} — ключи БЕЗ фигурных скобок.
    Значение None трактуется как пустая строка.
    """
    doc = Document(io.BytesIO(template_bytes))
    for para in _iter_paragraphs(doc):
        _replace_in_paragraph(para, context)
    # Заголовки/колонтитулы тоже могут содержать плейсхолдеры.
    for section in doc.sections:
        for hf in (section.header, section.footer,
                   section.first_page_header, section.first_page_footer):
            for para in getattr(hf, "paragraphs", []):
                _replace_in_paragraph(para, context)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def list_placeholders(template_bytes: bytes) -> list[str]:
    """Возвращает уникальные плейсхолдеры (без скобок) из шаблона — для UI."""
    doc = Document(io.BytesIO(template_bytes))
    found = []
    seen = set()
    for para in _iter_paragraphs(doc):
        for m in _PLACEHOLDER_RE.finditer(para.text):
            key = m.group(0)[1:-1]
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found
