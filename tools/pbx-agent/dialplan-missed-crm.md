# Правка диалплана АТС: пропущенные и голосовые → SiriCRM

Заменяет почтовые уведомления (`sc_send_missed.sh`, `sc_send_ticket_*.sh`) на
push в CRM. **Почта с АТС не уходит вообще**: postfix релеит через
`smtp.mail.ru`, а тот отвечает `535 5.7.0 Net dostupa na vashem tarife` на
любую попытку авторизации — за месяц наблюдений десятки тысяч отказов и ни
одной успешной внешней доставки. Опечатка в адресе РОПа (`rop@antydiolg.ru`)
была лишь вторым слоем той же беды.

## Порядок установки

```bash
# 0) сначала выкатить CRM: без эндпоинта /telephony/agent/missed/ события
#    просто лягут в спул и будут ждать
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
     https://siricrm.ru/telephony/agent/ping/          # ждём 200

# 1) скрипт уведомления
scp sc_notify_crm.sh root@94.233.72.30:/etc/_aster-scr/     # порт 2222
chmod 755 /etc/_aster-scr/sc_notify_crm.sh
mkdir -p /var/spool/pbx-agent/events && chmod 700 /var/spool/pbx-agent/events

# 2) обновлённый агент (умеет досылать события и голосовые)
scp pbx_agent.py root@94.233.72.30:/usr/local/bin/pbx_agent.py

# 3) бэкап диалплана и правка
cp /etc/asterisk/extensions.conf /etc/asterisk/extensions.conf.bak-$(date +%Y%m%d)
#    ... правки ниже ...
asterisk -rx "dialplan reload"

# 4) проверка: позвонить на входящий номер и бросить трубку до ответа
tail -f /var/log/pbx-notify.log
```

🛑 `dialplan reload`, а не рестарт Asterisk: рестарт рвёт активные разговоры.

## Что менять

### 1. Пропущенные — контексты `[miss_call_cc]`, `[miss_call_osd]`, `[miss_call_yuro]`

Было (три письма на каждый пропущенный, все три в никуда):

```
    same => n,Set(email=call-centr@antydolg.ru)
    same => n,system(/etc/_aster-scr/sc_send_missed.sh ${CALLERID(num)} ${datetime} ${SIDEA} ${email})
    same => n,Set(email=rop@antydiolg.ru)
    same => n,system(/etc/_aster-scr/sc_send_missed.sh ${CALLERID(num)} ${datetime} ${SIDEA} ${email})
    same => n,Set(email=nazarov@a3b.biz)
    same => n,system(/etc/_aster-scr/sc_send_missed.sh ${CALLERID(num)} ${datetime} ${SIDEA} ${email})
```

Стало (одна строка; получателей выбирает CRM по группе):

```
    same => n,system(/etc/_aster-scr/sc_notify_crm.sh missed cc ${CHANNEL(linkedid)} ${UNIQUEID} ${CALLERID(num)})
```

Код группы — третий позиционный: `cc` в `miss_call_cc`, `osd` в
`miss_call_osd`, `yuro` в `miss_call_yuro`. Он должен совпадать с
`CallGroup.code` в CRM (справочник «Группы входящих звонков»).

### 2. Голосовые — `[order_ticket]`, `[mess_rec_cc]`, `[mess_rec_osd]`, `[mess_rec_yuro]`

Уведомление о сообщении вешается на пост-обработчик `MixMonitor` — он
срабатывает, когда файл дописан:

```
same  => n,MixMonitor(${filename},W(2),/etc/_aster-scr/sc_notify_crm.sh voicemail cc ${CHANNEL(linkedid)} ${UNIQUEID} ${SIDEA} ${filename})
```

(в `mess_rec_osd` — `osd`, в `mess_rec_yuro` — `yuro`, в `order_ticket` — `cc`:
после 18:00 звонок идёт в колл-центр).

🛑 Сам wav шлёт не диалплан, а таймерный агент (`sync_voicemails`): в момент
события MixMonitor ещё дописывает файл, и отправленное сразу было бы обрезком.
Задержка до 5 минут — только у аудио; уведомление о самом факте уходит
мгновенно.

### 3. Старые скрипты

`sc_send_missed.sh` и `sc_send_ticket_*.sh` удалять НЕ нужно — они просто
перестают вызываться. Оставлены как след того, как это работало раньше.

## Разгрести почтовую очередь

После перевода на CRM очередь postfix продолжит копить недоставленное
(58 писем / 3.9 МБ на 25.08.2026, и постоянные попытки каждые 5 минут):

```bash
mailq | tail -1          # посмотреть объём
postsuper -d ALL         # очистить очередь целиком
```

Чинить сам SMTP смысла нет: на текущем тарифе mail.ru доступ по SMTP закрыт,
нужен либо другой почтовый провайдер, либо (что и сделано) другой канал
уведомлений.
