# Finance module — SiriCRM

Модуль финансового учёта. Реализован в `apps/finance/`, миграции в `apps/finance/migrations/`. Связан с `apps.crm` (Client, Service, ClientEvent) и `apps.core` (Employee).

## Сущности

### Справочники

| Модель | Назначение | Доступ |
| ------ | ---------- | ------ |
| `ExpenseType` | Типы расходов в разрезе услуги (`service_name` FK на `ServiceName`). Сиды: 12 типов для БФЛ (Агентское вознаграждение, Взнос СРО, Гос. пошлина, ФУ и т.д.) | редактирует `is_references_access` |
| `IncomeType` | Типы доходов в разрезе услуги. Сиды: 7 типов для БФЛ (Оплата юруслуг, Реализация имущества, Сбор документов, …) | то же |
| `IncomingAccount` | Куда поступил платёж (`account_type` = `cash`/`bank` + `name`). Сиды: основная касса, расчётный счёт | то же |
| `OutgoingAccount` | Откуда оплачено (та же структура, отдельная таблица) | то же |

Управление справочниками — `/references/` → вкладки «Типы расходов», «Типы доходов», «Кассы (приход)», «Кассы (расход)».

### Основные модели

#### `Payment`
Реальный платёж — входящий (доход) или исходящий (расход).

```python
direction: 'in' | 'out'
payment_date: date
amount_in / amount_out: Decimal nullable     # заполнено одно из двух по direction
payment_form: 'cash' | 'cashless'
expense_type? / income_type?: FK             # ровно один по direction
incoming_account? / outgoing_account?: FK    # ровно один по direction
client: FK (required)
service?: FK
charge?: FK                                  # связь с начислением — для входящего
created_by / updated_by: FK Employee
created_at / updated_at: auto
comments: text
```

Валидация направления — в `apps.finance.forms.PaymentForm.clean()`: лишние поля при сохранении обнуляются.

#### `Charge` (Начисление)
Плановый платёж клиента (выставленный счёт или строка графика рассрочки).

```python
client: FK (required)
service?: FK
due_date: date                               # планируемая дата оплаты
title: str                                   # «Юруслуги, платёж 1/6 за июнь 2026»
amount: Decimal
status: 'scheduled' | 'overdue' | 'paid'
comments: text
created_by / updated_by: FK Employee
```

Properties:
- `paid_amount` — сумма входящих платежей с `charge=self`
- `remaining` — `amount - paid_amount` (≥ 0)
- `display_status` — то, что показывается в UI: `paid` если погашено, `overdue` если `due_date < today` и не paid, иначе `status` поля. Используется в шаблонах для бейджей.
- `recalc_status(save=True)` — пересчитывает поле `status` на основе платежей. Ставит **только** `paid` или `scheduled`. `overdue` в БД пишет management-команда / celery-task (см. ниже).

#### Параметры графика на `Service` (apps/crm)
Service получил поля для генератора графика:

```python
# Финансовые параметры
legal_services_amount      # Юруслуги по договору, default 72000
installment_months         # Рассрочка, default 6
doc_collection             # Сбор документов, default 7500
postal_costs / state_duty / additional_costs   # default 0
fu_fee                     # Вознаграждение ФУ, default 25000
procedure_costs            # Расходы на процедуру, default 20000

# Смещения дат (мес, 0–6)
schedule_legal_offset      # default 2 — первый платёж юруслуг через N мес
schedule_fu_offset         # default 1
schedule_procedure_offset  # default 2

# Мета графика
schedule_date              # date — когда график был сгенерирован
schedule_created_by / schedule_updated_by  # FK Employee
```

## Логика генератора графика

`apps/finance/views.py` → функция `_generate_charges(service)`. Все даты считаются от **`service.date_start`** (Дата начала оказания услуг), которую пользователь редактирует прямо в модалке графика.

**Юруслуги** — `installment_months` строк рассрочки:
- Первая дата: `date_start + schedule_legal_offset` месяцев; если день > 10 → перенос на следующий месяц, день=10. Иначе тот же месяц, день=10.
- Далее — `+1 месяц` от первой.
- Сумма каждой строки: `legal_services_amount / installment_months` (округление до копеек).
- Title: `«Юруслуги, платёж N/M за <месяц> <год>»` — месяц/год из due_date.

**Прочие платежи** — по одной строке, если сумма > 0:
- Сбор документов / Почтовые / Гос. пошлина: `date_start + 7 дней`
- Доп. расходы: `date_start + 60 дней`
- ФУ: `date_start + schedule_fu_offset` мес
- Расходы на процедуру: `date_start + schedule_procedure_offset` мес

После генерации:
- `service.schedule_date = today`
- `schedule_created_by` при первой генерации; `schedule_updated_by` при последующих
- `service.contract_price = sum(charges.amount)` — цена договора = итого графика
- Событие `ClientEvent.schedule_created` или `schedule_updated`

## Авто-статус Charge

Источники изменения статуса:

1. **`Charge.recalc_status()`** — вызывается из сигналов `apps.finance.signals` после `Payment.save` / `Payment.delete`. Ставит только `paid` или `scheduled`. Возвращает новое значение.
2. **`apps.finance.services.mark_overdue()`** — единый источник перехода в `overdue`. Используется в:
   - management-команде `manage.py mark_overdue_charges` (ручной запуск)
   - celery-task `apps.finance.tasks.mark_overdue_charges` (ежедневный beat в 03:00)
3. **`display_status` property** — для рендера в шаблонах; учитывает `due_date < today` и возвращает `overdue` без записи в БД.

`mark_overdue` логирует **только переход** в overdue (был не-overdue, стал overdue) — повторные запуски не дублируют события.

## Лог событий

Все операции пишутся в `ClientEvent` через helper `_log_event` (см. `apps/finance/views.py`):

| event_type | Когда |
| ---------- | ----- |
| `schedule_created` | Генерация графика на пустую услугу |
| `schedule_updated` | Повторная генерация поверх существующих начислений |
| `payment_in_created` / `payment_out_created` | Новый платёж |
| `payment_in_edited` / `payment_out_edited` | Редактирование |
| `payment_in_deleted` / `payment_out_deleted` | Удаление |
| `charge_overdue` | Переход начисления в overdue (системное событие, `employee=None`) |

Описание события (`description`) включает сумму, дату и тип. Автор берётся из `request.user.employee` (для cron-таска — None).

## Доступы

Реализованы в `apps.finance.permissions`:

| Действие | Кто может |
| -------- | --------- |
| Создание / редактирование платежей и начислений | `admin`, `accountant`, superuser |
| Удаление платежа | `admin`, superuser |
| Удаление начисления | `admin`, `head_dep`, `consultant`, superuser + `agent` если он исполнитель услуги |
| Справочники типов / касс | `is_references_access`: superuser, `admin`, `head_dep` |
| Просмотр модалок Финансы / График | любой авторизованный (`@login_required`) |

Декораторы `@require_edit`, `@require_delete` — оборачивают view. Для UI-кнопок флаги `can_edit`, `can_delete` пробрасываются в контекст шаблона.

## UI-узлы

- **Кнопка «₽» (wallet)** на каждой карточке канбана (main, services, my) → `finance:finance_modal` (по клиенту).
- **Модалка «Финансы и расчёты»**: сводные цифры, фильтр сегментными кнопками (Начисления / Входящие / Исходящие), таблица с сортировкой, кнопки «+ Начисление» (с выбором услуги), «+ Входящий», «− Исходящий».
- **Кнопка «График»** на форме редактирования услуги (рядом с «Файл договора», после блока дат). Зелёная с датой если `schedule_date` задана.
- **Модалка «График платежей»**: параметры графика, селекты смещений (0–6 мес), таблица начислений с возможностью редактирования и удаления каждой строки, кнопка «+ Начисление» вручную.
- **Кнопка «Составить договор»** — рядом с «График». Disabled пока не заполнены: `schedule_date`, `date_dogovor`, `contract_price`, `payment_procedure`, `date_start`. Обработчик пока не реализован.

## Celery / Cron

- `apps.finance.tasks.mark_overdue_charges` — ежедневно в **03:00** (см. `config/celery.py`). Помечает просроченные начисления и пишет `charge_overdue` события.
- Ручной запуск той же логики: `docker compose exec -T web python manage.py mark_overdue_charges`.

## Миграции

- `crm/0050…0051` — `Client.is_identified`
- `crm/0052` — финансовые поля на Service (8 шт)
- `crm/0053` — параметры графика на Service (offset, schedule_date, created/updated_by)
- `crm/0054…0056` — расширение `ClientEvent.EVENT_CHOICES` (финансовые типы)
- `finance/0001` — начальная схема всех моделей
- `finance/0002` — сиды БФЛ-справочников

## Что осталось на будущее

- Обработчик кнопки **«Составить договор»** (генерация Word/PDF из шаблона).
- Возможность ручной привязки уже существующего платежа к Charge через UI (сейчас select при создании входящего платежа есть, но при редактировании можно потерять связь — стоит проверить).
- Перерасчёт `contract_price` при редактировании отдельных Charge (сейчас перезаписывается только при генерации графика).
- Отчёты по платежам (период / контрагенты / типы) — пока не делали.
