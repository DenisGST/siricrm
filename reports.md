# Отчёты — раздел отчётности (apps/reports)

Раздел `/reports/` — рабочее место аналитики/отчётности. Каркас с вкладками; отчёты добавляются поэтапно (по одному на вкладку). **На dev и проде с 30.06.2026** (сортировка/фильтр — 01.07.2026).

## Каркас раздела

- App `apps.reports` (в `INSTALLED_APPS`), URL `/reports/` (namespace `reports`, `config/urls.py`). Своих тяжёлых зависимостей нет.
- Лендинг `views.panel` → `templates/reports/panel.html` — строка вкладок, контент грузится в `#reports-tab`; сам раздел открывается в `#content-area` (use_htmx-пункт меню), chrome главной (сайдбар/шапка) сохраняется. Активную вкладку подсвечивает JS `reportsTab()`.
- Пункт меню **«Отчёты»** (секция «Инструменты», иконка `bar-chart-3`) — seed-миграция `reports/0001_seed_menu` (идемпотентно, добавляется во все активные `DashboardConfig`). 🛑 После деплоя, добавляющего пункт меню, залогиненным юзерам нужен полный Ctrl+Shift+R — левое меню рендерится при полной загрузке страницы, HTMX-навигация его не обновляет.

## Доступ

- `apps/reports/permissions.py`: `can_access_reports(user)` = superuser ∪ `is_management` (admin/head_dep/managing_partner) ∪ `can_access_accounting` (бухгалтеры + руководство). Декоратор `require_reports` на вьюхах.
- Шаблонный фильтр `{{ user|can_access_reports }}` (`apps/core/templatetags/permissions_tags.py`); гейт пункта меню — `apps/core/context_processors.py` (по url `/reports/`).
- Руководитель ОП (на проде — Власов, роль `head_dep`) доступ имеет через `is_management`.

## Отчёт «Отдел продаж» (вкладка «Отдел продаж»)

Помесячный реестр входящих платежей-юруслуг + бюджет отдела продаж. View `reports:tab_sales`, шаблон `templates/reports/partials/_tab_sales.html`. Ядро логики — `apps/reports/views.py:_compute_operations` / `_render_sales_tab`.

### Данные и колонки

- Источник строк — `finance.Payment` (`direction="in"`) за выбранный месяц (селектор `<input type="month">`, по умолчанию текущий).
- Фильтр «юруслуги/договорные» — флаг **`IncomeType.is_legal_services`** (BooleanField, миграция `finance/0004`). Правится в Справочниках → Типы доходов (чекбокс «Юруслуги (гонорар)» + столбец-бейдж в списке). 🛑 **Флаг per-DB** — отметить нужные типы на dev И на prod, иначе отчёт пуст. Кандидат — тип «Оплата юруслуг».
- **Строка = операция-поступление.** Бухгалтерия дробит платёж по начислениям (`finance.Payment` per `Charge`); юруслуги-части группируются по родительской `accounting.IncomingPayment` (её `amount` = «Сумма платежа» целиком). Платёж без такой привязки (ручной ввод / Bubble-импорт) → операция = сам платёж, тогда «Сумма платежа» = «Сумма».
- Колонки: № · ФИО клиента · Дата платежа · **Дата введения процедуры** (= дата решения, `decision_date`) · **Сумма платежа** (вся операция) · Тип (форма оплаты Наличный/Безналичный) · Назначение (`Charge.title`, иначе `IncomeType.name`) · Комментарии (`Payment.comments`) · **Сумма** (Σ `amount_in` юруслуги-частей операции) · Начислено в бюджет ОП (редактируемое).
- 🔴 **Подсветка просрочки:** если платёж позже, чем через 8 мес от даты введения процедуры (ветка 400 ₽, флаг `op["is_late"]`) — «Дата платежа» и «Сумма» выводятся тёмно-красным (`#8b0000`, inline-style). При пустой дате введения — «—», подсветки нет (падает в fallback 1000).
- Верхние карточки: «Итого (руб)» = Σ «Сумма»; «Поступило по операциям» = Σ «Сумма платежа»; «Бюджет отдела продаж»; «Итого начислено в бюджет ОП».

### Бюджет отдела продаж

Отдельные модели (`apps/reports/models.py`, миграция `reports/0002`):
- `SalesBudget` — на месяц (unique `month` = 1-е число), поле **`budget_total`** = «Бюджет отдела продаж», `calculated_at`/`calculated_by`.
- `SalesBudgetEntry` — начисление по операции: FK `budget`, FK `payment` (представитель операции = первый платёж группы), `computed` (расчёт по правилу), `accrued` (редактируемое «Начислено»); unique (`budget`, `payment`).

**Правило начисления на строку** (`views._accrual`; `S` = колонка «Сумма» = юруслуги-часть):

| Условие | Начислено |
|---|---|
| S < 5000 ₽ | 0 ₽ |
| S ≥ 5000 ₽ и дата платежа `<` (дата решения + 8 мес) | 1000 ₽ |
| S ≥ 5000 ₽ и дата платежа `≥` (дата решения + 8 мес) | 400 ₽ |
| нет даты решения | как «до 8 мес»: S ≥ 5000 → 1000 ₽, иначе 0 |

- «Дата решения» = «дата вынесения судом первого решения» = **минимальная `Procedure.intro_date`** («Дата принятия решения о введении процедуры») среди процедур дела. Путь `Payment.service → Service.bankruptcy_case → procedures`; fallback — самая ранняя `intro_date` по клиенту, если у платежа нет услуги (`_decision_dates`).
- 8 месяцев считаются `_add_months` (без внешних зависимостей). Ровно 8 мес → трактуется как «после» → 400 ₽.
- 🛑 **Данные:** если `Procedure.intro_date` не заполнена — расчёт падает в fallback «до 8 мес» (1000 ₽), ветка 400 ₽ не сработает. Строки без даты подсвечены `border-warning` + tooltip. Чтобы заработала 400 ₽ — юристы должны заполнять «Дату принятия решения о введении процедуры» в карточках дел.

**Кнопка «Рассчитать»** (`reports:budget_calculate`, POST, hx-confirm) — считает по **ВСЕМ** операциям месяца (без фильтра сотрудника — бюджет отдела целиком): `update_or_create` `SalesBudgetEntry` (`computed`=`accrued`=правило), удаляет устаревшие entry, `budget_total` = Σ `computed`. Перезаписывает ручные правки «Начислено».

**Онлайн-правка «Начислено»** — `<input>` hx-post `reports:budget_entry_save/<payment_id>` (on change) → сохраняет `accrued`, возвращает обновлённое «Итого начислено» (партиал `_budget_accrued_total.html`): основной swap в `#budget-accrued-total` + OOB в футер `#budget-accrued-foot`. Считается с учётом текущего фильтра сотрудника.

### Сортировка и фильтр

- **Сортировка** — клик по `<th>` → hx-get `tab_sales` с `sort` (`fio`/`date`/`decision_date`/`amount_full`/`type`/`purpose`/`comments`/`amount`/`accrued`) + `dir` (`asc`/`desc`, toggle). Сортировка выполняется в Python в `_render_sales_tab` (после оверлея начислений), № перенумеровывается, активный столбец помечен стрелкой ↑/↓.
- **Фильтр по сотруднику** — `<select>` «Сотрудник (закреплён за клиентом)»: список `_assigned_employees()` (Employee с ≥1 клиентом, активные; PK у Employee — int). Фильтрует `Payment.client__employees=emp`. При активном фильтре «Итого (руб)»/«Итого начислено» — по подвыборке (вклад конкретного сотрудника).
- Все контролы (месяц / emp / заголовки / «Рассчитать» / inline-input) несут текущее состояние через `hx-vals` — без формы и JS-хранения состояния. CSRF для hx-post — глобально через `htmx:configRequest` (dashboard.html).
- 🛑 `budget_calculate` **игнорирует** emp/sort (всегда весь месяц); `budget_entry_save` считает «Итого начислено» с учётом emp (`entries.filter(payment__client__employees=emp)`) — совпадает с отфильтрованным видом.

## Деплой

- Обычный `deploy` (зависимостей нет). Миграции: `finance/0004` (флаг `is_legal_services`), `reports/0001` (пункт меню), `reports/0002` (модели бюджета). Сиды НЕ нужны (пункт меню — в миграции).
- 🛑 После деплоя вручную на КАЖДОМ сервере (per-DB): (1) отметить юруслуги-типы дохода в Справочниках; (2) для ветки 400 ₽ — обеспечить заполнение `Procedure.intro_date` в делах.

## Файлы

- `apps/reports/` — `views.py` (логика отчёта + правило + endpoints), `models.py` (`SalesBudget`/`SalesBudgetEntry`), `permissions.py`, `urls.py`, `migrations/` (`0001_seed_menu`, `0002_initial`).
- `templates/reports/panel.html` + `partials/_tab_sales.html` + `partials/_budget_accrued_total.html`.
- Флаг юруслуг: `apps/finance/models.py:IncomeType.is_legal_services` + `apps/finance/forms.py` + `templates/finance/partials/income_type_form_modal.html` / `references_income_types.html`.
