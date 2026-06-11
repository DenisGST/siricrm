"""Парсер kad.arbitr.ru на Selenium + selenium-stealth.

Запускается ТОЛЬКО внутри контейнера arbitr-runner — там стоят
google-chrome + chromedriver + Xvfb. На web/celery-основном воркере
эти зависимости не нужны — поэтому импорт selenium сделан ленивым
(внутри KadSession.__enter__), чтобы файл импортировался в tasks.py
с обычного воркера без падения.

Публичный API сохранён прежний (см. tasks.py / management/kad_probe):
    search_case_by_party(fio, court_code='', *, headless=None)
        -> list[KadSearchHit]
    parse_case_page(kad_url, *, headless=None) -> KadCaseInfo

Почему не headless: kad детектит headless-Chrome (через CDP-флаги,
WebGL-fingerprint и поведенческие признаки). Headed-Chrome через Xvfb
проходит. Параметр headless оставлен для локальной отладки на машине
с графикой — в проде всегда False.

Антидетект-стек:
  - google-chrome-stable + chromedriver (не Chromium-форк Playwright)
  - --disable-blink-features=AutomationControlled
  - excludeSwitches=['enable-automation'], useAutomationExtension=False
  - --disable-webgl / --disable-gpu / --enable-unsafe-swiftshader
  - selenium-stealth(vendor='Google Inc.', platform='Win32',
                     webgl_vendor='Intel Inc.', renderer='Intel Iris OpenGL Engine')
  - UA Windows/Chrome/120
  - UI-навигация: главная → input → клик «Найти» → результат → /Card/
    (а не AJAX-POST на /Kad/SearchInstances, который kad капчит сразу)
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

from django.conf import settings

logger = logging.getLogger("arbitr.kad")

KAD_BASE_URL = "https://kad.arbitr.ru"
# Дефолтный таймаут ожидания элементов в Selenium.
WAIT_TIMEOUT_S = 25
# Признаки страницы блокировки/капчи у kad. Маркер должен встречаться
# ТОЛЬКО на странице активной captcha challenge / IP-блока, а не в обычной
# kad-странице — иначе ложные срабатывания (на нормальной странице есть
# `<script type="x-jquery-tmpl" id="pravocaptcha_template">` с текстом
# капчи внутри как темплейт jquery).
CAPTCHA_MARKERS = (
    'id="tokenFrom"',                  # форма submit'а captcha challenge
    "pravocaptcha.execute",             # JS-вызов на странице challenge
    "Доступ заблокирован",              # IP-блок (HTTP 451)
    "Подтвердите, что вы не робот",
)


class KadCaptchaRequired(Exception):
    """Kad показал капчу — нужно вмешательство человека."""

    def __init__(self, screenshot_url: str = "", page_url: str = ""):
        super().__init__("kad captcha required")
        self.screenshot_url = screenshot_url
        self.page_url = page_url


class KadParserError(Exception):
    """Неожиданная разметка/таймаут/прочая ошибка парсера."""


@dataclass
class KadSearchHit:
    """Один результат поиска по ФИО/номеру дела на kad."""
    case_number: str
    kad_url: str
    court_name: str = ""
    parties: list[str] = field(default_factory=list)
    filed_at: str = ""


@dataclass
class KadEvent:
    """Событие на странице дела."""
    kad_event_id: str = ""
    instance_id: str = ""
    event_date: str = ""
    kind: str = ""
    title: str = ""
    description: str = ""
    attachments: list[dict] = field(default_factory=list)


def _hash_event_id(ev: "KadEvent") -> str:
    """Стабильный id события (kad data-id всегда пуст) для идемпотентности.

    Хешируем все смыслообразующие поля. При повторном парсинге той же
    страницы id выйдет тот же → UNIQUE-индекс по (case, kad_event_id)
    защитит от дублей.
    """
    payload = "|".join([
        ev.instance_id, ev.event_date, ev.kind,
        ev.title, ev.description,
    ])
    return "h:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


@dataclass
class KadCaseInfo:
    """Сводка по карточке дела."""
    case_number: str
    case_id: str = ""
    status: str = ""
    case_type: str = ""
    court_name: str = ""
    judge: str = ""
    instances: list[dict] = field(default_factory=list)
    participants: dict[str, list[str]] = field(default_factory=dict)
    events: list[KadEvent] = field(default_factory=list)


class KadSession:
    """Context manager — один Chrome-driver на сессию.

    Использование:
        with KadSession() as kad:
            hits = kad.search_by_party("Иванов И.И.")
            info = kad.parse_case("https://kad.arbitr.ru/Card/<uuid>")

    Один webdriver на batch — экономит cold-start (~3-5 сек) и сохраняет
    cookies между запросами, что критично для прохождения проверок kad.
    """

    def __init__(
        self,
        headless: Optional[bool] = None,
        *,
        download_mode: bool = False,
    ):
        """
        download_mode=True → Chrome с PDF-prefs (`always_open_pdf_externally`).
            Использовать ТОЛЬКО для download_pdf — в обычной сессии эти
            prefs ломают search на kad (anti-bot детектит отсутствие PDF
            Viewer plugin в navigator.plugins → click «Найти» игнорируется).
        download_mode=False (default) → чистый baseline Chrome → search/parse.
        """
        self.headless = settings.ARBITR_HEADLESS if headless is None else headless
        self.download_mode = download_mode
        self.driver = None
        self._wait = None
        # Флаг «прогретой» сессии. Прямой GET /Card/<uuid> даёт капчу,
        # но если до того в этой же Chrome-сессии прошёл поиск через UI —
        # переход на карточку работает. Прогреваем 1 раз на сессию.
        self._warmed = False
        # Папка для download'ов PDF — нужна только в download_mode.
        self._download_dir = (
            tempfile.mkdtemp(prefix="arbitr_dl_") if download_mode else ""
        )

    def __enter__(self) -> "KadSession":
        # Ленивые импорты — selenium есть только в arbitr-runner.
        from selenium import webdriver  # noqa: WPS433
        from selenium.webdriver.chrome.options import Options  # noqa: WPS433
        from selenium.webdriver.support.ui import WebDriverWait  # noqa: WPS433
        from selenium_stealth import stealth  # noqa: WPS433

        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--start-maximized")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-extensions")
        # Отключаем WebGL/GPU — это часть fingerprint, который kad чекает.
        opts.add_argument("--enable-unsafe-swiftshader")
        opts.add_argument("--disable-webgl")
        opts.add_argument("--disable-accelerated-2d-canvas")
        opts.add_argument("--disable-gpu")
        # UA — Windows/Chrome 120. Linux UA на kad подозрительный.
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        # PDF-prefs включаются ТОЛЬКО в download_mode. В обычной search/parse-
        # сессии `plugins.always_open_pdf_externally=True` ломает поиск:
        # kad-anti-bot читает navigator.plugins, не видит PDF Viewer → считает
        # клиента ботом → click «Найти» молча игнорируется (диагноз получен
        # эмпирически 11.06.2026 пошаговым isolating'ом prefs).
        if self.download_mode:
            opts.add_experimental_option("prefs", {
                "plugins.always_open_pdf_externally": True,
                "download.default_directory": self._download_dir,
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": False,
            })

        logger.info(
            "kad: starting Chrome (headless=%s download_mode=%s)",
            self.headless, self.download_mode,
        )
        self.driver = webdriver.Chrome(options=opts)

        # selenium-stealth подменяет navigator/webgl-fingerprint.
        # Параметры — те же что в его эталонном коде.
        stealth(
            self.driver,
            languages=["ru-RU", "ru"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )

        self._wait = WebDriverWait(self.driver, WAIT_TIMEOUT_S)
        # Дублируем download path через CDP — только в download_mode.
        # В обычной сессии этот CDP-вызов тоже подмешивает «automation»-флаг
        # в Chrome (теоретически — kad на это может реагировать).
        if self.download_mode:
            try:
                self.driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                    "behavior": "allow",
                    "downloadPath": self._download_dir,
                })
            except Exception as exc:  # noqa: BLE001 — non-fatal
                logger.debug("CDP setDownloadBehavior failed: %s", exc)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.driver:
                self.driver.quit()
        finally:
            self.driver = None
            if self._download_dir:
                try:
                    shutil.rmtree(self._download_dir, ignore_errors=True)
                except Exception:
                    pass
        return False

    # ---------- внутренние ----------

    def load_kad_cookies(self, cookies: list) -> None:
        """Загружает cookies kad в текущую Chrome-сессию.

        Используется чтобы download-сессия (с PDF prefs) переняла auth
        от main-сессии (которая уже прогрелась через поиск). После этого
        прямой GET /Card/<uuid> в download-сессии работает без капчи.

        Принимает формат `driver.get_cookies()` (list[dict]).
        """
        if not cookies:
            return
        # add_cookie работает только когда уже открыт страница того же домена.
        self.driver.get(KAD_BASE_URL)
        time.sleep(1)
        for c in cookies:
            ck = {
                "name": c["name"],
                "value": c["value"],
                "path": c.get("path", "/"),
            }
            if "domain" in c:
                ck["domain"] = c["domain"]
            if c.get("expiry"):
                try:
                    ck["expiry"] = int(c["expiry"])
                except (TypeError, ValueError):
                    pass
            if c.get("secure") is True:
                ck["secure"] = True
            if c.get("httpOnly") is True:
                ck["httpOnly"] = True
            if c.get("sameSite") in ("Strict", "Lax", "None"):
                ck["sameSite"] = c["sameSite"]
            try:
                self.driver.add_cookie(ck)
            except Exception as exc:  # noqa: BLE001
                logger.debug("kad: skip cookie %s: %s", c.get("name"), exc)
        logger.info("kad: loaded %d cookies into download session", len(cookies))

    def _is_captcha_page(self) -> bool:
        try:
            src = self.driver.page_source or ""
        except Exception:
            src = ""
        return self._is_captcha_response(src)

    @staticmethod
    def _is_captcha_response(body: str) -> bool:
        """True если в HTML-теле есть маркеры активного captcha challenge."""
        return any(marker in (body or "") for marker in CAPTCHA_MARKERS)

    def _raise_if_captcha(self) -> None:
        if self._is_captcha_page():
            url = ""
            try:
                url = self.driver.current_url
            except Exception:
                pass
            logger.warning("kad: captcha detected on %s", url)
            raise KadCaptchaRequired(page_url=url)

    def _close_promo_popup(self) -> None:
        """Закрывает всплывашку kad (если есть) — она перехватывает клики."""
        from selenium.webdriver.common.by import By  # noqa: WPS433
        from selenium.common.exceptions import NoSuchElementException  # noqa: WPS433
        try:
            close_btn = self.driver.find_element(
                By.CLASS_NAME, "b-promo_notification-popup-close",
            )
            close_btn.click()
            logger.info("kad: закрыта всплывашка b-promo_notification-popup-close")
            time.sleep(1)
        except NoSuchElementException:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("kad: ignore popup close error: %s", exc)

    def _ensure_main_loaded(self) -> None:
        """Открывает главную kad — стартовая точка любой сессии."""
        logger.info("kad: navigating to main page")
        self.driver.get(KAD_BASE_URL)
        time.sleep(3)
        self._raise_if_captcha()
        self._close_promo_popup()

    # На главной kad НЕСКОЛЬКО полей фильтра — каждое для своего scope:
    FIELD_PARTY = 'textarea[placeholder="название, ИНН или ОГРН"]'
    FIELD_CASE_NUMBER = 'input[placeholder="например, А50-5568/08"]'
    FIELD_COURT = 'input[placeholder="название суда"]'
    FIELD_JUDGE = 'input[placeholder="фамилия судьи"]'

    def _submit_search_form(
        self, query: str, *, field: str = FIELD_PARTY,
    ) -> None:
        """Заполнить указанное поле фильтра и отправить форму.

        Baseline-flow: `send_keys → click [alt="Найти"]`. Работает когда
        Chrome не triggers kad-anti-bot (см. download_mode PDF prefs).

        По умолчанию — поле «Участник дела» (textarea), для поиска по ФИО /
        названию ЮЛ / ИНН / ОГРН. Для поиска по номеру дела передавать
        `field=KadSession.FIELD_CASE_NUMBER`.
        """
        from selenium.webdriver.common.by import By  # noqa: WPS433
        from selenium.webdriver.support import expected_conditions as EC  # noqa: WPS433
        from selenium.common.exceptions import NoSuchElementException  # noqa: WPS433

        inp = self._wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, field)),
        )
        inp.clear()
        inp.send_keys(query)
        time.sleep(1)
        try:
            btn = self.driver.find_element(By.CSS_SELECTOR, '[alt="Найти"]')
        except NoSuchElementException as exc:
            raise KadParserError(
                'kad: нет кнопки [alt="Найти"]',
            ) from exc
        # JS-click обходит ElementClickInterceptedException — при вводе
        # в поле «Участник» kad показывает suggester-dropdown, который
        # перекрывает кнопку «Найти» поверх z-index.
        self.driver.execute_script('arguments[0].click();', btn)

    def _wait_search_loaded(self, max_s: int = 25) -> None:
        """После клика «Найти» ждёт пока скроется .b-case-loading.

        Проверка через computed-style (`getComputedStyle().display`), а не
        inline-атрибут: kad может скрывать loader CSS-классом или родителем
        без inline `style="display: none"` — и старый CSS-селектор
        `.b-case-loading:not([style*="display: none"])` тогда всегда true.
        """
        for _ in range(max_s):
            time.sleep(1)
            still_visible = self.driver.execute_script(
                """
                return Array.from(document.querySelectorAll('.b-case-loading'))
                    .some(function(el){
                        return window.getComputedStyle(el).display !== 'none';
                    });
                """
            )
            if not still_visible:
                return
        logger.warning("kad: .b-case-loading не скрылся за %ds", max_s)

    def _warm_up(self) -> None:
        """Делает один поиск в UI — без этого kad даёт капчу на /Card/<uuid>.

        Идея: search через UI ставит session-cookies, после чего kad доверяет
        этому браузеру и отдаёт страницы дел без challenge. Запускаем 1 раз
        на жизнь KadSession.
        """
        from selenium.webdriver.common.by import By  # noqa: WPS433
        from selenium.webdriver.support import expected_conditions as EC  # noqa: WPS433
        from selenium.common.exceptions import (  # noqa: WPS433
            NoSuchElementException, TimeoutException,
        )

        if self._warmed:
            return
        self._ensure_main_loaded()

        # Прогревочный поиск по заведомо-валидному формату номера дела.
        # Реальное наличие дела не важно — kad ставит куки на сам факт
        # отправки поиска.
        try:
            # Поле Участник: dummy запрос — kad ставит cookies/state
            # на сам факт submit, наличие результатов не важно.
            self._submit_search_form("Иванов Иван Иванович")
        except TimeoutException as exc:
            self._raise_if_captcha()
            raise KadParserError(
                "kad: не нашли поле ввода для прогрева сессии",
            ) from exc

        self._wait_search_loaded()
        time.sleep(1)
        self._raise_if_captcha()
        self._warmed = True
        logger.info("kad: session warmed up")

    # ---------- публичные ----------

    def search_by_party(
        self, fio_or_case: str, court_code: str = "",
    ) -> list[KadSearchHit]:
        """Поиск дел через UI: главная → input → клик «Найти» → результаты.

        fio_or_case может быть либо ФИО («Иванов Иван Иванович»), либо
        номером дела («А12-33291/2024»). На главной kad одно общее поле.

        court_code — фильтр по префиксу case_number («А12»).
        """
        from selenium.webdriver.common.by import By  # noqa: WPS433
        from selenium.webdriver.support import expected_conditions as EC  # noqa: WPS433
        from selenium.common.exceptions import (  # noqa: WPS433
            NoSuchElementException, TimeoutException,
        )

        # Прогрев заодно открывает главную и делает один dummy-search,
        # чтобы расставить session-cookies. Дальше уже не нужно.
        if not self._warmed:
            self._warm_up()
        else:
            self._ensure_main_loaded()

        driver = self.driver

        try:
            self._submit_search_form(fio_or_case)
        except TimeoutException as exc:
            self._raise_if_captcha()
            raise KadParserError(
                "Не нашёл поле ввода номера дела/сторон на главной kad",
            ) from exc
        logger.info("kad: search submitted q=%r", fio_or_case)

        # AJAX-поиск на kad нередко занимает 10-15 секунд.
        self._wait_search_loaded()
        time.sleep(1)
        self._raise_if_captcha()

        # Строки результатов — в tbody таблицы #b-cases. Структура колонок:
        #   td.num         — иконка типа + дата подачи + a.num_case с номером
        #   td.court       — .judge (судья) + второй div (имя суда)
        #   td.plaintiff   — список .js-rollover (истцы; включая скрытые .more)
        #   td.respondent  — список .js-rollover (ответчики)
        rows = driver.find_elements(By.CSS_SELECTOR, '#b-cases tbody tr')
        hits: list[KadSearchHit] = []
        prefix = court_code.upper().strip()
        for row in rows:
            try:
                # Ссылка + номер
                link = row.find_element(By.CSS_SELECTOR, 'td.num a.num_case')
                case_number = (link.text or "").strip()
                href = link.get_attribute("href") or ""
                if not href or "/Card/" not in href:
                    continue
                if prefix and not case_number.upper().startswith(prefix):
                    continue

                # Дата подачи (span внутри иконки .bankruptcy / .civilian / ...)
                filed_at = ""
                try:
                    filed_at = row.find_element(
                        By.CSS_SELECTOR, 'td.num .b-container > div > span',
                    ).text.strip()
                except NoSuchElementException:
                    pass

                # Судья и суд
                judge = ""
                court_name = ""
                try:
                    judge = row.find_element(
                        By.CSS_SELECTOR, 'td.court .judge',
                    ).text.strip()
                except NoSuchElementException:
                    pass
                try:
                    # Второй div в .b-container — суд (первый это .judge)
                    divs = row.find_elements(
                        By.CSS_SELECTOR, 'td.court .b-container > div',
                    )
                    for d in divs:
                        if "judge" not in (d.get_attribute("class") or ""):
                            t = (d.text or "").strip()
                            if t:
                                court_name = t
                                break
                except NoSuchElementException:
                    pass

                # Стороны: каждый .js-rollover — одно лицо. У него внутри ещё
                # есть скрытый .js-rolloverHtml — берём только первый текст-node.
                parties: list[str] = []
                for sel in ("td.plaintiff .js-rollover", "td.respondent .js-rollover"):
                    for el in row.find_elements(By.CSS_SELECTOR, sel):
                        try:
                            name = driver.execute_script(
                                "var n=arguments[0].firstChild;"
                                "return n && n.nodeType===3 ? n.textContent.trim() : '';",
                                el,
                            )
                            if name:
                                parties.append(name)
                        except Exception:
                            pass

                hits.append(KadSearchHit(
                    case_number=case_number,
                    kad_url=href,
                    court_name=" / ".join(filter(None, [court_name, judge])) if judge else court_name,
                    parties=parties,
                    filed_at=filed_at,
                ))
            except NoSuchElementException:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.debug("kad: skip search row: %s", exc)
                continue

        logger.info(
            "kad.search_by_party: q=%r → %d hits (court_filter=%r)",
            fio_or_case, len(hits), prefix,
        )
        return hits

    def parse_case(self, kad_url: str) -> KadCaseInfo:
        """Парсит карточку дела по прямой ссылке /Card/<uuid>.

        Перед прямым GET нужен «прогрев» — один поиск через UI, после
        которого kad ставит session-cookies и отдаёт карточки без капчи.
        """
        from selenium.webdriver.common.by import By  # noqa: WPS433
        from selenium.common.exceptions import NoSuchElementException  # noqa: WPS433

        if not self._warmed:
            self._warm_up()

        logger.info("kad: navigating to case %s", kad_url)
        self.driver.get(kad_url)
        time.sleep(5)
        self._raise_if_captcha()

        d = self.driver

        # case_number и case_id — из hidden input'ов в шапке: kad сам кладёт
        # их туда после рендера. Надёжнее, чем парсить <title> (он может быть
        # обрезан / содержать суффиксы).
        case_number = ""
        try:
            case_number = d.find_element(
                By.CSS_SELECTOR, '#caseName',
            ).get_attribute("value") or ""
        except NoSuchElementException:
            # Фоллбэк — title страницы.
            case_number = (d.title or "").strip()
        case_id = ""
        try:
            case_id = d.find_element(
                By.CSS_SELECTOR, '#caseId',
            ).get_attribute("value") or ""
        except NoSuchElementException:
            case_id = kad_url.rstrip("/").split("/")[-1]

        # Статус («Рассматривается в первой, апелляционной и кассационной...»)
        status = ""
        try:
            status = d.find_element(
                By.CSS_SELECTOR, '.b-case-header-desc',
            ).text.strip()
        except NoSuchElementException:
            pass

        # Тип дела (банкротство / экономспоры / админ) — иконка в .b-icon-case
        case_type = ""
        for sel in (
            "#b-container > div.b-noColumns-middle.g-fs-120 > dl > dt > span:nth-child(1)",
            ".b-case-header-icon",
            ".b-case-type",
        ):
            try:
                case_type = d.find_element(By.CSS_SELECTOR, sel).text.strip()
                if case_type:
                    break
            except NoSuchElementException:
                continue

        participants = self._extract_participants(case_type)
        instances = self._extract_instances()
        events = self._extract_events()

        # Достаём судью у каждой инстанции из её событий (самое частое значение
        # в title с признаком ФИО — «Фамилия И. О.»). kad на уровне шапки
        # инстанции имя судьи не пишет, но в каждом «Событие» оно прокидывается.
        self._fill_instance_judges(instances, events)

        # Судья и суд первой инстанции: ищем "первая" в типе, иначе последнюю
        # инстанцию (kad перечисляет сверху вниз от высшей к низшей).
        first_inst = next(
            (i for i in instances if "первая" in i.get("type", "").lower()),
            instances[-1] if instances else {},
        )
        court_name = first_inst.get("court", "")
        judge = first_inst.get("judge", "")

        info = KadCaseInfo(
            case_number=case_number,
            case_id=case_id,
            status=status,
            case_type=case_type,
            court_name=court_name,
            judge=judge,
            instances=instances,
            participants=participants,
            events=events,
        )
        logger.info(
            "kad.parse_case(%s): num=%r case_type=%r instances=%d events=%d",
            kad_url, info.case_number, info.case_type,
            len(info.instances), len(info.events),
        )
        return info

    # ---------- helpers для parse_case ----------

    def _extract_participants(self, case_type: str) -> dict[str, list[str]]:
        """Участники дела из таблицы под шапкой.

        Селекторы остались с эталона: td.plaintiffs / td.defendants /
        td.third / td.others (в этой таблице — другие классы, чем в
        результатах поиска!). В банкротстве истцы→кредиторы, ответчики→должники.
        """
        from selenium.webdriver.common.by import By  # noqa: WPS433
        from selenium.common.exceptions import NoSuchElementException  # noqa: WPS433

        participants = {
            "истцы": [], "ответчики": [],
            "третьи_лица": [], "кредиторы": [], "должники": [], "иные_лица": [],
        }
        # Таблица участников — внутри b-case-info / b-noColumns-middle.
        table = None
        for sel in (
            "table.b-case-info",
            ".b-noColumns-middle table",
        ):
            try:
                table = self.driver.find_element(By.CSS_SELECTOR, sel)
                break
            except NoSuchElementException:
                continue
        if table is None:
            logger.debug("kad: таблица участников не найдена")
            return participants

        bankruptcy = (
            "несостоятельности" in (case_type or "").lower()
            or "банкротстве" in (case_type or "").lower()
        )

        def collect(sel: str, target: str) -> None:
            try:
                for a in table.find_elements(By.CSS_SELECTOR, sel):
                    t = (a.text or "").strip()
                    if t:
                        participants[target].append(t)
            except NoSuchElementException:
                pass

        if bankruptcy:
            collect("td.plaintiffs a", "кредиторы")
            collect("td.defendants a", "должники")
        else:
            collect("td.plaintiffs a", "истцы")
            collect("td.defendants a", "ответчики")
        collect("td.third a", "третьи_лица")
        collect("td.others a", "иные_лица")
        return participants

    def _extract_instances(self) -> list[dict]:
        """Список инстанций из шапок .b-chrono-item-header.

        Возвращает [{type, number, court, instance_id}, ...] —
        в порядке как kad их рисует (сверху вниз).
        """
        from selenium.webdriver.common.by import By  # noqa: WPS433
        from selenium.common.exceptions import NoSuchElementException  # noqa: WPS433

        result: list[dict] = []
        headers = self.driver.find_elements(
            By.CSS_SELECTOR, '.b-chrono-item-header',
        )
        for h in headers:
            inst: dict = {}
            inst["instance_id"] = h.get_attribute("data-id") or ""
            inst["court_code"] = h.get_attribute("data-court") or ""
            # Тип инстанции: .l-col strong
            try:
                inst["type"] = h.find_element(
                    By.CSS_SELECTOR, '.l-col strong',
                ).text.strip()
            except NoSuchElementException:
                inst["type"] = ""
            # Номер инстанции: .b-case-instance-number
            try:
                inst["number"] = h.find_element(
                    By.CSS_SELECTOR, '.b-case-instance-number',
                ).text.strip()
            except NoSuchElementException:
                inst["number"] = ""
            # Имя суда: .instantion-name (ссылка на сайт суда)
            try:
                inst["court"] = h.find_element(
                    By.CSS_SELECTOR, '.instantion-name',
                ).text.strip()
            except NoSuchElementException:
                inst["court"] = ""
            # Судья появляется только в раскрытом виде — пока пропустим;
            # достаём из событий, если потребуется.
            inst["judge"] = ""
            result.append(inst)
        return result

    def _fill_instance_judges(
        self, instances: list[dict], events: list[KadEvent],
    ) -> None:
        """Заполняет instance['judge'] из event.title где kind == 'Событие'.

        В событиях типа «Событие» kad ставит ФИО судьи в case-subject.
        Берём самое частое ФИО в рамках конкретной инстанции.
        """
        import re  # локальный — функция дёргается редко
        from collections import Counter
        fio_re = re.compile(r"^[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.?)?$")
        for inst in instances:
            iid = inst.get("instance_id", "")
            if not iid:
                continue
            cnt: Counter[str] = Counter()
            for ev in events:
                if ev.instance_id != iid:
                    continue
                if ev.kind == "Событие" and ev.title and fio_re.match(ev.title):
                    cnt[ev.title] += 1
            if cnt:
                inst["judge"] = cnt.most_common(1)[0][0]

    def _extract_events(self) -> list[KadEvent]:
        """Раскрывает все инстанции (клик .b-collapse .b-sicon) → собирает события."""
        from selenium.webdriver.common.by import By  # noqa: WPS433
        from selenium.common.exceptions import NoSuchElementException  # noqa: WPS433

        events: list[KadEvent] = []
        try:
            self.driver.find_element(By.ID, "chrono_list_content")
        except NoSuchElementException:
            logger.warning("kad: блок хронологии #chrono_list_content не найден")
            return events

        # Кликаем «раскрыть» для каждой инстанции. Используем
        # execute_script('arguments[0].click()') — обходит проблемы с overlap'ом.
        headers = self.driver.find_elements(By.CLASS_NAME, "b-chrono-item-header")
        clicked = 0
        for h in headers:
            try:
                btn = h.find_element(By.CSS_SELECTOR, ".b-collapse .b-sicon")
                self.driver.execute_script("arguments[0].click()", btn)
                clicked += 1
                time.sleep(0.3)
            except NoSuchElementException:
                # Уже развёрнуто или нет кнопки — пропускаем
                continue
            except Exception as exc:  # noqa: BLE001
                logger.debug("kad: collapse error: %s", exc)
        logger.info("kad: раскрыто инстанций: %d/%d", clicked, len(headers))
        # Ждём AJAX-подгрузки контента хронологии.
        time.sleep(6)

        # Событие — .b-chrono-item.js-chrono-item (БЕЗ -header). Внутри:
        # .case-date / .case-type / .case-subject / .additional-info /
        # .b-case-result-text. data-instance_id связывает с инстанцией.
        nodes = self.driver.find_elements(
            By.CSS_SELECTOR, '.b-chrono-item.js-chrono-item',
        )
        for node in nodes:
            try:
                event = self._parse_event_node(node)
                if event.title or event.event_date or event.description:
                    events.append(event)
            except Exception as exc:  # noqa: BLE001
                logger.debug("kad: skip event: %s", exc)

        # data-id у событий kad всегда пуст — для идемпотентности генерируем
        # стабильный хеш от (instance_id, event_date, kind, title, description).
        # При повторном парсинге той же страницы id остаётся прежним.
        for ev in events:
            if not ev.kad_event_id:
                ev.kad_event_id = _hash_event_id(ev)

        logger.info("kad: собрано событий: %d", len(events))
        return events

    def _parse_event_node(self, node) -> KadEvent:
        """Извлекает поля одного события."""
        from selenium.webdriver.common.by import By  # noqa: WPS433
        from selenium.common.exceptions import NoSuchElementException  # noqa: WPS433

        def first_text(*selectors) -> str:
            for sel in selectors:
                try:
                    el = node.find_element(By.CSS_SELECTOR, sel)
                    t = (el.text or "").strip()
                    if t:
                        return t
                except NoSuchElementException:
                    continue
            return ""

        # data-date более надёжен чем .case-date (бывает многострочный с лишним).
        event_date = (node.get_attribute("data-date") or "").strip()
        if not event_date:
            event_date = first_text(".case-date")
        kind = first_text(".case-type")
        # Субъект (обычно ФИО судьи или сторона)
        title = first_text(".case-subject", ".b-case-result-text")
        # «Дополнительная информация» — описание события + входящий номер
        description = first_text(".additional-info")
        # Результат события (если есть)
        result_text = first_text(".b-case-result-text")
        if result_text and result_text != title:
            description = (description + "\n" + result_text).strip()

        kad_event_id = (
            node.get_attribute("data-id")
            or node.get_attribute("id")
            or ""
        )
        instance_id = node.get_attribute("data-instance_id") or ""

        # Документы внутри события: .pdf или /PdfDocument/.
        attachments: list[dict] = []
        try:
            for a in node.find_elements(
                By.CSS_SELECTOR,
                "a[href*='PdfDocument'], a[href*='.pdf']",
            ):
                href = a.get_attribute("href") or ""
                if not href:
                    continue
                attachments.append({
                    "name": (a.text or "").strip() or a.get_attribute("title") or "",
                    "kad_url": href,
                    "is_locked": "b-locked" in (a.get_attribute("class") or ""),
                })
        except NoSuchElementException:
            pass

        return KadEvent(
            kad_event_id=kad_event_id,
            instance_id=instance_id,
            event_date=event_date,
            kind=kind,
            title=title,
            description=description,
            attachments=attachments,
        )

    def download_pdf(
        self, url: str, timeout: int = 60, *, referer: str = "",
    ) -> tuple[bytes, str]:
        """Скачивает PDF kad через эту Chrome-сессию.

        Требует `KadSession(download_mode=True)` — иначе PDF откроется в
        PDF.js viewer'е, а не скачается на диск. Также требует чтобы перед
        вызовом сессия уже была «доверенной» kad — либо через `_warm_up()`
        (search-flow, но он сломан в download_mode из-за PDF prefs),
        либо через `load_kad_cookies(...)` из main-сессии (правильный путь).

        Карточка дела (`referer`) откроется перед скачиванием — kad даёт
        PDF только когда запрос идёт ВНУТРИ существующей session-сессии
        (cookies + JS-state от загруженной карточки). Прямой navigate на
        /Kad/PdfDocument/... → ПравоКапча.

        Возвращает (content_bytes, 'application/pdf').
        """
        if not self.download_mode:
            raise KadParserError(
                "download_pdf требует KadSession(download_mode=True) — "
                "иначе Chrome открывает PDF в viewer'е вместо скачивания"
            )

        # Карточка дела ОБЯЗАТЕЛЬНА для прохождения kad-защиты PDF.
        if not referer:
            raise KadParserError("download_pdf: нужен referer (URL карточки дела)")

        if self.driver.current_url != referer:
            logger.info("kad.download_pdf: navigate to card %s", referer)
            self.driver.get(referer)
            time.sleep(3)
            self._raise_if_captcha()

        # Чистим папку перед загрузкой
        for fname in os.listdir(self._download_dir):
            try:
                os.remove(os.path.join(self._download_dir, fname))
            except OSError:
                pass

        main_handle = self.driver.current_window_handle
        before_handles = set(self.driver.window_handles)

        logger.info("kad.download_pdf: window.open %s", url)
        # Через window.open сохраняется browsing-context (origin, opener),
        # и kad считает это легитимным click-like запросом.
        self.driver.execute_script("window.open(arguments[0], '_blank')", url)

        # Ждём появления PDF (не *.crdownload — partial). Лимит timeout сек.
        deadline = time.monotonic() + timeout
        pdf_path: Optional[str] = None
        while time.monotonic() < deadline:
            time.sleep(0.5)
            files = os.listdir(self._download_dir)
            # Chrome скачивает в *.crdownload → потом переименовывает.
            finished = [
                f for f in files
                if not f.endswith(".crdownload") and not f.startswith(".")
            ]
            if finished:
                pdf_path = os.path.join(self._download_dir, finished[0])
                # Дожидаемся стабильности размера (1 проверка через 0.5с).
                size1 = os.path.getsize(pdf_path)
                time.sleep(0.5)
                if os.path.exists(pdf_path) and os.path.getsize(pdf_path) == size1:
                    break
                pdf_path = None

        # Закрываем все «лишние» вкладки и возвращаемся к карточке.
        # Делаем это В FINALLY — чтобы при KadCaptchaRequired/Error
        # тоже подтереть вкладки.
        try:
            if not pdf_path:
                # Проверим — может PDF-вкладка показала capcha
                page_html = ""
                for h in self.driver.window_handles:
                    if h in before_handles:
                        continue
                    self.driver.switch_to.window(h)
                    try:
                        page_html = self.driver.page_source or ""
                    except Exception:
                        page_html = ""
                    if self._is_captcha_response(page_html):
                        break
                if self._is_captcha_response(page_html):
                    raise KadCaptchaRequired(page_url=url)
                raise KadParserError(
                    f"PDF не скачался за {timeout}с (url={url}; "
                    f"title={self.driver.title!r})"
                )

            with open(pdf_path, "rb") as f:
                content = f.read()
            try:
                os.remove(pdf_path)
            except OSError:
                pass

            # Грубая проверка что это PDF
            if not content[:5].startswith(b"%PDF-"):
                head = content[:300].decode("utf-8", errors="ignore")
                if self._is_captcha_response(head):
                    raise KadCaptchaRequired(page_url=url)
                raise KadParserError(f"Скачанный файл не PDF (head={head!r})")

            logger.info("kad.download_pdf: OK len=%d", len(content))
            return content, "application/pdf"
        finally:
            # Закрываем все вкладки кроме исходной (main_handle)
            try:
                for h in list(self.driver.window_handles):
                    if h != main_handle:
                        try:
                            self.driver.switch_to.window(h)
                            self.driver.close()
                        except Exception:
                            pass
                self.driver.switch_to.window(main_handle)
            except Exception as exc:  # noqa: BLE001
                logger.debug("kad.download_pdf: tab cleanup error: %s", exc)


# ---------- one-shot helpers (используются в tasks.py / management) ----------

def search_case_by_party(
    fio: str, court_code: str = "", *,
    headless: Optional[bool] = None,
) -> list[KadSearchHit]:
    with KadSession(headless=headless) as kad:
        return kad.search_by_party(fio, court_code)


def parse_case_page(
    kad_url: str, *, headless: Optional[bool] = None,
) -> KadCaseInfo:
    with KadSession(headless=headless) as kad:
        return kad.parse_case(kad_url)
