"""Импорт Bubble Сorrespondence → procedure.Request.

Структура Bubble: 1 запись Сorrespondence = 1 исходящий запрос + поля ответа
(`gDiskLink` — документ запроса, `answer` — Bubble UUID файла-ответа из Files,
`responceOK`/`dateresponce`/`numbResponse`/`textResponce` — данные ответа).

Маппинг: 22 типа TypeCorrespondence → 11 кодов RequestType. 3 не-запросных типа
(договор БФЛ, исковое, согласие СРО) — Request НЕ создают, их gDiskLink-файл
перекладывается в папку «Ввод» (slug `bfl_vvod`) файл-менеджера клиента.

Связи документов:
  - document_pdf  ← BubbleRecord(Files, linkGDrive=gDiskLink).target_id → StoredFile
  - response_scan ← BubbleRecord(Files, bubble_id=answer).target_id → StoredFile

Идемпотентность — Request.bubble_id = Сorrespondence._id.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.bubble_import import bubble_api
from apps.bubble_import.extractors import clean_str, parse_bubble_date, strip_bbcode
from apps.bubble_import.models import BubbleRecord
from apps.crm.models import Correspondence, LegalEntity, Service
from apps.files.folder_utils import create_bfl_folders
from apps.files.models import ClientFile, ClientFolder, StoredFile
from apps.procedure.models import BankruptcyCase, Request, RequestType
from apps.procedure.services import ensure_case


# Маппинг Bubble TypeCorrespondence UUID → Siri RequestType.code
TYPE_MAP = {
    # Росреестр
    "1592819854808x311194249196013600": "req_rosreestr",   # Запрос в Росреестр
    # МРЭО / ГИБДД
    "1592820297770x419034693604258800": "req_gibdd",       # Запрос в МРЭО
    "1627379400517x813205675558797600": "req_gibdd",       # Запрос в МРЭО при сборе документов
    # ГИМС
    "1592820011159x862704320202190700": "req_gims",
    # Гостехнадзор
    "1592820184512x737810964138474000": "req_gostehnadzor",
    # ДМИ
    "1592820206934x697556515168074000": "req_dmi",
    # ФНС/ИФНС (общий)
    "1592820239620x169687919615372900": "req_fns",         # Запрос в ИФНС о р/с
    # ФНС об участии в организациях
    "1592820281597x905437318540229400": "req_fns_orgs",
    # ПФР/СФР
    "1592820324851x722889247058348300": "req_sfr",
    # Суд
    "1595935824182x715569740374979440": "req_court",       # Запрос в районный суд
    # Банк
    "1628208712659x919657226419537500": "req_bank",        # Запрос в Банк при сборе документов
    "1592820353167x681594655515789200": "req_bank",        # Информационные письма в Банки и запросы выписок
    "1598352617036x373179712503447300": "req_bank",        # Требование о блокировке расчетного счета
    # Информационные письма в госорган
    "1592820376232x675566374235342200": "req_info_gov",    # Главному приставу
    "1595935784661x364241596872799200": "req_info_gov",    # РОСП
    "1595936014084x274707359409679170": "req_info_gov",    # в Росреестр
    "1595935953529x913078824250700000": "req_info_gov",    # в ФНС по месту жительства
    "1595935982950x906051552347703000": "req_info_gov",    # в Управление ФНС
    # Прочее / уведомления
    "1592820421851x148310869851861150": "req_other",       # Исходящее прочее
    "1631688350122x872602681135270900": "req_other",       # Уведомление Должнику
    "1596190362316x626070657515158300": "req_other",       # Уведомление о праве предъявления требований
    "1662557942257x349219838061163500": "req_other",       # Уведомление о получении исполлиста
}

# Не-запросные типы → файл (gDiskLink) перекладывается в папку «Ввод».
NON_REQUEST_TYPES = {
    "1639405998614x944388200425387400",  # Договор юридических услуг по банкротству ФЛ
    "1592820397426x761555076630345200",  # Исковое заявление о признании банкротом
    "1592819834107x572050886826387840",  # Согласие на процедуру
}

# typeOut → sent_method
DELIVERY_MAP = {
    "1594321992343x852569493544664300": "other",     # Телеграмма → other
    "1594322018052x693109484436059900": "post",      # Почта РФ
    "1594325344446x759457260371181600": "site",      # С сайта организации
    "1594325495522x992871103337332700": "email",     # На электронную почту
    "1594327225617x244581667327442940": "handed",    # Нарочно
}


class Command(BaseCommand):
    help = "Импорт Bubble Сorrespondence → procedure.Request (запрос + ответ + файлы)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Не сохранять — только посчитать.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Ограничить число обрабатываемых записей.")
        parser.add_argument("--service", type=str, default="",
                            help="UUID одной услуги (для точечного теста).")
        parser.add_argument("--reapply", action="store_true",
                            help="Обновлять уже импортированные Request (по bubble_id).")
        parser.add_argument("--purge-correspondence", action="store_true",
                            help="Перед импортом удалить ВСЕ crm.Correspondence "
                                 "с bubble_id (Bubble-импорт). Используй когда "
                                 "пересоздаёшь пары out/in после изменения логики.")
        parser.add_argument("--no-correspondence", action="store_true",
                            help="Не создавать пары crm.Correspondence (только Request).")

    # ── Кэши на прогон ────────────────────────────────────────────────────
    _types_by_code = None
    _service_cache = {}
    _legalentity_cache = {}
    _outgoing_counters = {}  # case.id → max outgoing_number
    # Files-кэши: предзагружаются единожды на старте handle()
    _file_by_bubble_id = {}   # Bubble Files._id → StoredFile.id (UUID str)
    _file_by_gdrive_url = {}  # Bubble Files.linkGDrive → StoredFile.id (точный URL)
    # Gosorgan-кэш: для recipient_name строкой когда LegalEntity FK не найден
    _gosorgan_by_id = {}      # Bubble Gosorgan._id → {name, adress}

    def _request_type(self, code):
        if self._types_by_code is None:
            self._types_by_code = {rt.code: rt for rt in RequestType.objects.all()}
        return self._types_by_code.get(code)

    def _service(self, project_bid):
        if not project_bid:
            return None
        if project_bid in self._service_cache:
            return self._service_cache[project_bid]
        svc = Service.objects.filter(bubble_id=project_bid).first()
        if svc is None:
            pf = BubbleRecord.objects.filter(
                entity="ProjectBFL", bubble_id=project_bid, status="imported",
            ).first()
            if pf and pf.target_id:
                svc = Service.objects.filter(pk=pf.target_id).first()
        self._service_cache[project_bid] = svc
        return svc

    def _recipient(self, kontragent_bid):
        """LegalEntity по Bubble Kontragent UUID (через map_gosorgan_to_legalentities).

        Возвращает (legal_entity_or_none, name_fallback_string). Если FK не
        найден — пытается отдать имя из кэша Gosorgan (для recipient_name).
        """
        if not kontragent_bid:
            return None, ""
        if kontragent_bid in self._legalentity_cache:
            le = self._legalentity_cache[kontragent_bid]
        else:
            le = LegalEntity.objects.filter(bubble_id=kontragent_bid).first()
            self._legalentity_cache[kontragent_bid] = le
        # Имя-фолбэк из Gosorgan (для recipient_name строки в Request)
        name = ""
        gos = self._gosorgan_by_id.get(kontragent_bid)
        if gos:
            name = (gos.get("name") or "")[:255]
        return le, name

    def _next_outgoing_number(self, case):
        cur = self._outgoing_counters.get(case.id)
        if cur is None:
            cur = case.requests.aggregate(m=Max("outgoing_number"))["m"] or 0
        nxt = cur + 1
        self._outgoing_counters[case.id] = nxt
        return nxt

    def _storedfile_by_bubble_files_id(self, files_bid):
        """Bubble Files._id → StoredFile (по предзагруженному кэшу)."""
        if not files_bid:
            return None
        sf_id = self._file_by_bubble_id.get(files_bid)
        if not sf_id:
            return None
        return StoredFile.objects.filter(pk=sf_id).first()

    def _storedfile_by_gdrive_link(self, gdrive_link):
        """Bubble Files.linkGDrive (точный URL) → StoredFile (по кэшу)."""
        if not gdrive_link:
            return None
        sf_id = self._file_by_gdrive_url.get(gdrive_link)
        if not sf_id:
            return None
        return StoredFile.objects.filter(pk=sf_id).first()

    def _preload_gosorgan(self):
        """Тянем Gosorgan из Bubble API один раз — нужен для recipient_name."""
        self._gosorgan_by_id = {}
        if not bubble_api.is_configured():
            self.stdout.write("  ⚠ BUBBLE_API_TOKEN не задан — recipient_name строкой не загружен")
            return
        cursor = 0
        try:
            while True:
                page = bubble_api.fetch_page("Gosorgan", cursor=cursor, limit=100)
                for o in page["results"]:
                    self._gosorgan_by_id[o.get("_id")] = {
                        "name": o.get("name") or "",
                        "adress": o.get("adress") or "",
                    }
                cursor += page["count"]
                if page["remaining"] <= 0:
                    break
        except Exception as e:
            self.stdout.write(f"  ⚠ Gosorgan fetch err: {e}")
        self.stdout.write(f"  Кэш Gosorgan: {len(self._gosorgan_by_id)}")

    def _preload_file_caches(self):
        """Грузим все imported BubbleRecord(Files) с target_id в память."""
        self._file_by_bubble_id = {}
        self._file_by_gdrive_url = {}
        qs = (BubbleRecord.objects
              .filter(entity="Files", status="imported")
              .only("bubble_id", "target_id", "raw"))
        for br in qs.iterator(chunk_size=2000):
            if not br.target_id:
                continue
            if br.bubble_id:
                self._file_by_bubble_id[br.bubble_id] = br.target_id
            link = (br.raw or {}).get("linkGDrive")
            if link:
                self._file_by_gdrive_url[link] = br.target_id
        self.stdout.write(
            f"  Загружено кэшей Files: bubble_id={len(self._file_by_bubble_id)} "
            f"linkGDrive={len(self._file_by_gdrive_url)}"
        )

    # ── Не-запросные: переложить файл gDiskLink в «Ввод» ──────────────────
    def _move_to_vvod(self, service, stored_file, dry_run, stats):
        if stored_file is None:
            stats["non_request_no_file"] += 1
            return
        client = service.client
        # Создать BFL-папки если ещё нет; bfl_vvod в составе.
        vvod = ClientFolder.objects.filter(client=client, slug="bfl_vvod").first()
        if vvod is None:
            if dry_run:
                stats["non_request_file_moved_dry"] += 1
                return
            create_bfl_folders(client)
            vvod = ClientFolder.objects.filter(client=client, slug="bfl_vvod").first()
        cfs = list(ClientFile.objects.filter(stored_file=stored_file, folder__client=client))
        if not cfs:
            stats["non_request_no_clientfile"] += 1
            return
        for cf in cfs:
            if cf.folder_id == vvod.id:
                continue
            if not dry_run:
                cf.folder = vvod
                cf.save(update_fields=["folder"])
            stats["non_request_file_moved" if not dry_run else "non_request_file_moved_dry"] += 1

    # ── Основная обработка одной BubbleRecord(Сorrespondence) ─────────────
    def _process(self, br, dry_run, reapply, stats):
        raw = br.raw or {}
        bid = br.bubble_id or raw.get("_id")
        type_uuid = raw.get("type")
        if not type_uuid:
            stats["no_type"] += 1
            return

        # 1. Не-запросные типы → положить gDiskLink-файл в Ввод
        if type_uuid in NON_REQUEST_TYPES:
            service = self._service(raw.get("ProjectBfl"))
            if service is None:
                stats["non_request_no_service"] += 1
                return
            stored = self._storedfile_by_gdrive_link(raw.get("gDiskLink"))
            self._move_to_vvod(service, stored, dry_run, stats)
            stats["non_request"] += 1
            return

        # 2. Маппинг типа
        code = TYPE_MAP.get(type_uuid)
        if not code:
            stats["unknown_type"] += 1
            stats["unknown_type_uuids"][type_uuid] += 1
            return
        rt = self._request_type(code)
        if rt is None:
            stats["request_type_missing"] += 1
            return

        # 3. Service + case
        service = self._service(raw.get("ProjectBfl"))
        if service is None:
            stats["no_service"] += 1
            return
        if dry_run:
            # ensure_case в dry-run не выполняем — просто проверим что Service есть
            case = getattr(service, "bankruptcy_case", None) or BankruptcyCase(
                pk=None, service=service,
            )
        else:
            case = ensure_case(service)

        # 4. Recipient (FK + name строкой из Gosorgan-кэша)
        recipient, recipient_name = self._recipient(raw.get("Kontragent"))
        if recipient is not None and not recipient_name:
            recipient_name = (recipient.short_name or recipient.name or "")[:255]

        # 5. Поля Request
        sent_date = parse_bubble_date(raw.get("DateOut"))
        due_date = parse_bubble_date(raw.get("dateKontrol"))
        response_date = parse_bubble_date(raw.get("dateresponce"))
        responce_ok = bool(raw.get("responceOK"))
        sent_method = DELIVERY_MAP.get(raw.get("typeOut"), "post" if sent_date else "")

        # Статус
        if responce_ok:
            status = "answered"
        elif sent_date:
            status = "sent"
        else:
            status = "draft"

        # Bubble исх.№ — нестандартный (строка) → в notes; siri outgoing_number автоинкремент
        numb_isx = clean_str(raw.get("numbIsx"))
        comments = strip_bbcode(raw.get("comments"))
        notes_parts = []
        if numb_isx:
            notes_parts.append(f"Bubble исх.№ {numb_isx}")
        if comments:
            notes_parts.append(comments)
        notes = "\n\n".join(notes_parts)

        # 6. Файлы
        document_pdf = self._storedfile_by_gdrive_link(raw.get("gDiskLink"))
        response_scan = self._storedfile_by_bubble_files_id(raw.get("answer"))

        if document_pdf is not None:
            stats["with_document"] += 1
        if response_scan is not None:
            stats["with_response_scan"] += 1
        elif raw.get("answer"):
            stats["answer_without_file"] += 1

        # 7. Создание/обновление
        existing = Request.objects.filter(bubble_id=bid).first()
        if existing and not reapply:
            stats["already_exists"] += 1
            return

        if dry_run:
            stats["would_create" if not existing else "would_update"] += 1
            return

        defaults = {
            "case": case,
            "request_type": rt,
            "title": rt.name[:255],
            "recipient": recipient,
            "recipient_name": recipient_name,
            "status": status,
            "sent_method": sent_method,
            "sent_date": sent_date,
            "response_days": rt.response_days,
            "due_date": due_date,
            "response_date": response_date if responce_ok else None,
            "response_number": clean_str(raw.get("numbResponse"))[:120],
            "response_text": strip_bbcode(raw.get("textResponce")),
            "response_scan": response_scan,
            "document_pdf": document_pdf,
            "notes": notes,
        }

        if existing:
            for k, val in defaults.items():
                setattr(existing, k, val)
            existing.save()
            req = existing
            stats["updated"] += 1
        else:
            defaults["outgoing_number"] = self._next_outgoing_number(case)
            defaults["bubble_id"] = bid
            req = Request.objects.create(**defaults)
            stats["created"] += 1

        # 8. Correspondence-пары для разделов «Входящие/Исходящие»
        if not self._make_correspondence:
            return
        self._upsert_correspondence_pair(
            req=req,
            service=service,
            recipient=recipient,
            recipient_name=recipient_name,
            sent_date=sent_date,
            due_date=due_date,
            response_date=response_date,
            responce_ok=responce_ok,
            raw=raw,
            bid=bid,
            stats=stats,
        )

    def _upsert_correspondence_pair(self, *, req, service, recipient, recipient_name,
                                     sent_date, due_date, response_date, responce_ok,
                                     raw, bid, stats):
        """Создать/обновить пару crm.Correspondence: одну исходящую (документ
        запроса) и одну входящую (скан ответа, если есть).

        bubble_id у пары:
          outgoing → bid
          incoming → f"{bid}:incoming"
        """
        from apps.crm.models import Correspondence  # локальный импорт во избежание циклов

        comments_text = strip_bbcode(raw.get("comments"))

        # Исходящее (документ запроса).
        out_defaults = {
            "service": service,
            "request": req,
            "counterparty": recipient,
            "direction": "outgoing",
            "subject_type": req.title[:255],
            "outgoing_number": clean_str(raw.get("numbIsx"))[:100],
            "sent_at": sent_date,
            "delivery_method": req.sent_method or "",
            "file_link": clean_str(raw.get("gDiskLink"))[:5000],
            "stored_file": req.document_pdf,
            "track_response": bool(raw.get("trackResponse")),
            "control_date": due_date,
            "response_received": responce_ok,
            "response_date": response_date if responce_ok else None,
            "response_text": req.response_text,
            "response_number": req.response_number,
            "comments": comments_text,
        }
        Correspondence.objects.update_or_create(bubble_id=bid, defaults=out_defaults)
        stats["corr_outgoing"] += 1

        # Входящее (скан ответа) — только если ответ зафиксирован.
        if responce_ok and (response_date or req.response_scan_id):
            in_bid = f"{bid}:incoming"
            recip_label = recipient_name or (recipient.short_name or recipient.name)[:255] if recipient else ""
            in_subject = f"Ответ: {req.title[:200]}"
            in_defaults = {
                "service": service,
                "request": req,
                "counterparty": recipient,
                "direction": "incoming",
                "subject_type": in_subject[:255],
                "outgoing_number": "",   # у входящего номера исходящего нет
                "sent_at": response_date,
                "delivery_method": "",
                "file_link": "",
                "stored_file": req.response_scan,
                "track_response": False,
                "control_date": None,
                "response_received": False,
                "response_date": None,
                "response_text": "",
                "response_number": req.response_number,  # № ответа
                "comments": "",
            }
            Correspondence.objects.update_or_create(bubble_id=in_bid, defaults=in_defaults)
            stats["corr_incoming"] += 1

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        limit = opts["limit"]
        reapply = opts["reapply"]
        service_uuid = opts["service"]
        self._make_correspondence = not opts["no_correspondence"]

        # Очистка старых Bubble-Correspondence — перед перезаливкой пар out/in.
        if opts["purge_correspondence"] and not dry_run:
            n = Correspondence.objects.exclude(bubble_id__isnull=True).exclude(bubble_id="").count()
            self.stdout.write(f"Удаляю {n} crm.Correspondence с bubble_id…")
            Correspondence.objects.exclude(bubble_id__isnull=True).exclude(bubble_id="").delete()

        qs = BubbleRecord.objects.filter(entity="Сorrespondence", status="imported")
        if service_uuid:
            svc = Service.objects.filter(pk=service_uuid).first()
            if not svc or not svc.bubble_id:
                self.stderr.write(f"Service {service_uuid} не найден или без bubble_id")
                return
            qs = qs.filter(raw__ProjectBfl=svc.bubble_id)
        total = qs.count()
        if limit:
            qs = qs[:limit]

        self.stdout.write(
            f"Обрабатываем {qs.count()} BubbleRecord(Сorrespondence) "
            f"(всего imported в базе: {total}). dry_run={dry_run} reapply={reapply}"
        )
        self.stdout.write("Предзагрузка кэшей Files…")
        self._preload_file_caches()
        self.stdout.write("Предзагрузка Gosorgan (для recipient_name)…")
        self._preload_gosorgan()

        stats = Counter()
        stats["unknown_type_uuids"] = Counter()

        batch = []
        for br in qs.iterator(chunk_size=500):
            batch.append(br)
            if len(batch) >= 500:
                if dry_run:
                    for b in batch:
                        self._process(b, True, reapply, stats)
                else:
                    with transaction.atomic():
                        for b in batch:
                            self._process(b, False, reapply, stats)
                batch = []
                self.stdout.write(
                    f"  …обработано {sum(stats[k] for k in ('created','updated','already_exists','would_create','would_update'))} "
                    f"created={stats['created']} updated={stats['updated']} "
                    f"non_req={stats['non_request']} no_svc={stats['no_service']} "
                    f"unknown={stats['unknown_type']}"
                )

        # хвост
        if batch:
            if dry_run:
                for b in batch:
                    self._process(b, True, reapply, stats)
            else:
                with transaction.atomic():
                    for b in batch:
                        self._process(b, False, reapply, stats)

        # Итоговый отчёт
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== ИТОГ ==="))
        for key in [
            "created", "updated", "already_exists",
            "would_create", "would_update",
            "non_request", "non_request_no_service",
            "non_request_no_file", "non_request_no_clientfile",
            "non_request_file_moved", "non_request_file_moved_dry",
            "no_service", "no_type", "unknown_type", "request_type_missing",
            "with_document", "with_response_scan", "answer_without_file",
            "corr_outgoing", "corr_incoming",
        ]:
            self.stdout.write(f"  {key}: {stats[key]}")

        if stats["unknown_type_uuids"]:
            self.stdout.write("")
            self.stdout.write("Неизвестные TypeCorrespondence UUIDs (top-10):")
            for u, n in stats["unknown_type_uuids"].most_common(10):
                self.stdout.write(f"  {u}: {n}")
