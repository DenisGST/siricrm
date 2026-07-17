# -*- coding: utf-8 -*-
"""Заливка стартовых статей руководства пользователя.

Идемпотентно: статьи ищутся по slug. По умолчанию НЕ трогает уже
существующие (их мог отредактировать администратор через UI). С флагом
--force-text перезаписывает заголовок и тело из кода, но сохраняет позицию
в дереве и флаг публикации.

    python manage.py wiki_seed
    python manage.py wiki_seed --force-text

🛑 Контент хранится в БД и НЕ синхронизируется деплоем — команду надо
прогнать на КАЖДОМ сервере (dev и prod), как procedure_seed / efrsb_seed.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.wiki.models import WikiArticle
from apps.wiki.seed_content import CONTENT


class Command(BaseCommand):
    help = "Заливает стартовые статьи руководства пользователя (идемпотентно)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force-text",
            action="store_true",
            help="Перезаписать заголовок и текст существующих статей из кода.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        self.force = opts["force_text"]
        self.created = self.updated = self.skipped = 0
        self._walk(CONTENT, parent=None, order=10)
        self.stdout.write(self.style.SUCCESS(
            f"Готово: создано {self.created}, обновлено {self.updated}, "
            f"без изменений {self.skipped}."
        ))

    def _walk(self, nodes, parent, order):
        step = order
        for node in nodes:
            art = self._upsert(node, parent, step)
            step += 10
            children = node.get("children") or []
            if children:
                self._walk(children, parent=art, order=10)

    def _upsert(self, node, parent, order):
        art = WikiArticle.objects.filter(slug=node["slug"]).first()
        if art is None:
            art = WikiArticle.objects.create(
                slug=node["slug"],
                title=node["title"],
                body=node["body"],
                parent=parent,
                order=order,
                is_published=True,
            )
            self.created += 1
            self.stdout.write(f"  + {node['slug']}")
            return art

        # Существует: место в дереве синхронизируем всегда (чтобы порядок в
        # оглавлении соответствовал коду), текст — только по --force-text.
        changed = False
        if art.parent_id != (parent.pk if parent else None):
            art.parent = parent
            changed = True
        if art.order != order:
            art.order = order
            changed = True
        if self.force:
            if art.title != node["title"] or art.body != node["body"]:
                art.title = node["title"]
                art.body = node["body"]
                changed = True

        if changed:
            art.save()
            self.updated += 1
            self.stdout.write(f"  ~ {node['slug']}")
        else:
            self.skipped += 1
        return art
