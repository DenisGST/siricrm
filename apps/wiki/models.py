"""Модель руководства пользователя — дерево статей.

Одна статья = один узел дерева. Иерархия — самоссылкой parent, порядок внутри
уровня — полем order. Нумерация («1», «1.1», «1.2») НЕ хранится: она
вычисляется из позиции узла среди опубликованных соседей (services.build_tree),
иначе при вставке раздела в середину пришлось бы перенумеровывать всё дерево.

Тело — markdown, рендер в HTML — markdown_render.render_markdown.
"""
import re

from django.db import models
from django.urls import reverse
from django.utils.text import slugify

# Кириллица → латиница для человекочитаемых slug'ов в URL.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit_slug(text: str) -> str:
    """«Вход и регистрация» → «vhod-i-registraciya»."""
    low = (text or "").lower()
    latin = "".join(TRANSLIT.get(ch, ch) for ch in low)
    s = slugify(latin)
    return s or "article"


class WikiArticle(models.Model):
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
        verbose_name="Родительский раздел",
    )
    slug = models.SlugField(
        max_length=140,
        unique=True,
        verbose_name="Адрес (slug)",
        help_text="Часть URL: /wiki/a/<slug>/. Заполнится сама из заголовка.",
    )
    title = models.CharField(max_length=200, verbose_name="Заголовок")
    order = models.PositiveIntegerField(
        default=0,
        verbose_name="Порядок",
        help_text="Внутри своего уровня, по возрастанию.",
    )
    body = models.TextField(blank=True, verbose_name="Текст (markdown)")
    is_published = models.BooleanField(
        default=True,
        verbose_name="Опубликована",
        help_text="Снятая с публикации статья видна только редакторам руководства.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "core.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Кто правил последним",
    )

    class Meta:
        verbose_name = "Статья руководства"
        verbose_name_plural = "Статьи руководства"
        ordering = ["order", "title"]
        indexes = [models.Index(fields=["parent", "order"])]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug(translit_slug(self.title))
        super().save(*args, **kwargs)

    def _unique_slug(self, base: str) -> str:
        base = base[:130]
        slug, i = base, 2
        qs = WikiArticle.objects.exclude(pk=self.pk) if self.pk else WikiArticle.objects.all()
        while qs.filter(slug=slug).exists():
            slug = f"{base}-{i}"
            i += 1
        return slug

    def get_absolute_url(self) -> str:
        return reverse("wiki:article", kwargs={"slug": self.slug})

    @property
    def ancestors(self):
        """Цепочка родителей от корня до self (для «хлебных крошек»)."""
        chain, node, guard = [], self.parent, 0
        while node is not None and guard < 20:  # guard — страховка от цикла в данных
            chain.append(node)
            node, guard = node.parent, guard + 1
        return list(reversed(chain))

    @property
    def depth(self) -> int:
        return len(self.ancestors)
