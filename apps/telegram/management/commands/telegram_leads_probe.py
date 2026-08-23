"""Разведка канала лидов: что бот видит в getUpdates и как это парсится.

Зачем: чтобы включить приём лидов, нужен `chat_id` канала (в него бот должен
быть добавлен администратором) и уверенность, что формат сообщений разбирается.
Команда НИЧЕГО не создаёт в CRM и по умолчанию не двигает offset — сообщения
останутся в очереди Telegram для обычного поллера.

    docker exec siricrm-web-1 python manage.py telegram_leads_probe

🛑 getUpdates на один токен можно дёргать только из одного места. Если в beat
включён поллер (`poll-telegram-leads` / `poll-telegram-bot-private` / бот
мониторинга), команда возьмёт тот же SETNX-лок и дождётся окна; при 409
Conflict — выключите поллер на время разведки.
"""
import json

import requests
from django.core.cache import cache
from django.core.management.base import BaseCommand

from apps.telegram.leads_bot import BOT_TOKEN, LEADS_CHANNEL_ID, _parse_lead
from apps.telegram.tasks import OFFSET_CACHE_KEY, POLL_LOCK_KEY


class Command(BaseCommand):
    help = "Показать апдейты бота (chat_id канала) и результат парсинга заявок"

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=5,
                            help="long-poll timeout, сек (по умолчанию 5)")
        parser.add_argument("--limit", type=int, default=20,
                            help="сколько апдейтов показать")
        parser.add_argument("--commit-offset", action="store_true",
                            help="сдвинуть offset (апдейты больше не придут)")
        parser.add_argument("--full", action="store_true",
                            help="печатать текст сообщений целиком")

    def handle(self, *args, **o):
        if not BOT_TOKEN:
            self.stderr.write("TELEGRAM_BOT_TOKEN пуст — нечем поллить")
            return

        me = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",
                          timeout=15).json()
        if not me.get("ok"):
            self.stderr.write(f"getMe: {me.get('description')} — токен недействителен")
            return
        bot = me["result"]
        self.stdout.write(f"Бот: @{bot.get('username')} (id={bot.get('id')})")
        self.stdout.write(f"TELEGRAM_LEADS_CHANNEL_ID в env: {LEADS_CHANNEL_ID or '— не задан'}")

        got_lock = cache.add(POLL_LOCK_KEY, "probe", timeout=o["timeout"] + 10)
        if not got_lock:
            self.stdout.write(self.style.WARNING(
                "Лок поллинга занят — рядом работает beat-задача. "
                "Возможен 409 Conflict; выключите поллер на время разведки."))
        try:
            params = {
                "timeout": o["timeout"],
                "allowed_updates": json.dumps(
                    ["channel_post", "edited_channel_post", "message"]),
            }
            offset = cache.get(OFFSET_CACHE_KEY)
            if offset is not None:
                params["offset"] = offset
                self.stdout.write(f"offset из кэша: {offset}")
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                             params=params, timeout=o["timeout"] + 15)
            data = r.json()
        finally:
            if got_lock:
                cache.delete(POLL_LOCK_KEY)

        if not data.get("ok"):
            self.stderr.write(f"getUpdates: {data.get('description')}")
            return

        updates = data.get("result") or []
        self.stdout.write(f"\nАпдейтов получено: {len(updates)}\n")
        if not updates:
            self.stdout.write(
                "Пусто. Причины: бот не админ в канале · сообщений не было за 24ч · "
                "апдейты уже вычитал другой поллер (offset сдвинут).")

        chats = {}
        for upd in updates[: o["limit"]]:
            msg = (upd.get("channel_post") or upd.get("edited_channel_post")
                   or upd.get("message"))
            if not msg:
                self.stdout.write(f"— update {upd.get('update_id')}: "
                                  f"{', '.join(k for k in upd if k != 'update_id')}")
                continue
            chat = msg.get("chat") or {}
            title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            chats[str(chat.get("id"))] = f"{chat.get('type')} · {title}"
            text = msg.get("text") or msg.get("caption") or ""
            parsed = _parse_lead(text)

            self.stdout.write("─" * 70)
            self.stdout.write(f"update {upd.get('update_id')} | chat_id={chat.get('id')} "
                              f"| {chat.get('type')} | {title}")
            self.stdout.write(text if o["full"] else (text[:300] + ("…" if len(text) > 300 else "")))
            if parsed is None:
                self.stdout.write(self.style.WARNING("  → не заявка (парсер вернул None)"))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"  → заявка: тел={parsed['phone'] or '—'} №={parsed['number'] or '—'} "
                    f"лендинг={parsed['page'] or '—'} форма={parsed['form'] or '—'} "
                    f"имя={parsed['name'] or '—'}"))
                for q, a in parsed["answers"]:
                    self.stdout.write(f"     • {q}: {a[:80]}")

        if chats:
            self.stdout.write("\nНайденные чаты (кандидаты в TELEGRAM_LEADS_CHANNEL_ID):")
            for cid, descr in chats.items():
                mark = "  ← совпадает с env" if cid == str(LEADS_CHANNEL_ID) else ""
                self.stdout.write(f"  {cid}  {descr}{mark}")

        if o["commit_offset"] and updates:
            cache.set(OFFSET_CACHE_KEY, updates[-1]["update_id"] + 1, 7 * 24 * 3600)
            self.stdout.write(self.style.WARNING(
                f"offset сдвинут на {updates[-1]['update_id'] + 1}"))
