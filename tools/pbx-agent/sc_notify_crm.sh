#!/bin/sh
# Уведомление CRM о пропущенном звонке / голосовом сообщении.
# Ставится на АТС в /etc/_aster-scr/sc_notify_crm.sh (chmod 755).
#
# Пришёл на смену sc_send_missed.sh и sc_send_ticket_*.sh: почта с этой машины
# не уходит вообще — postfix релеит через smtp.mail.ru, а тот отвечает
# «535 Net dostupa na vashem tarife» на любую попытку авторизации, и письма
# просто копятся в очереди. Обращения из-за этого терялись молча.
#
# Вызов из диалплана:
#   System(/etc/_aster-scr/sc_notify_crm.sh missed   <group> <linkedid> <uniqueid> <номер> [вн.номер])
#   System(/etc/_aster-scr/sc_notify_crm.sh voicemail <group> <linkedid> <uniqueid> <номер> <файл.wav>)
#
# 🛑 Скрипт НЕ должен задерживать диалплан: событие кладётся в спул, отправка
# уходит в фон. Если CRM недоступна, файл остаётся в спуле и его дошлёт
# таймерный агент (pbx_agent.py --flush-events) — обращение не потеряется.
#
# 🛑 Повторная отправка безопасна: CRM склеивает события по linkedid и
# уведомляет по каждому звонку один раз.

set -u

CONF=/etc/pbx-agent.conf
SPOOL=/var/spool/pbx-agent/events
LOG=/var/log/pbx-notify.log

EVENT="${1:-missed}"
GROUP="${2:-}"
LINKEDID="${3:-}"
UNIQUEID="${4:-}"
PHONE="${5:-}"
EXTRA="${6:-}"

[ -n "$LINKEDID" ] || { echo "$(date '+%F %T') нет linkedid, событие пропущено" >>"$LOG"; exit 0; }

# Адрес и токен берём из конфига агента — второго места с секретами на АТС
# заводить незачем.
CRM_URL=$(sed -n 's/^[[:space:]]*crm_url[[:space:]]*=[[:space:]]*//p' "$CONF" 2>/dev/null | head -1)
TOKEN=$(sed -n 's/^[[:space:]]*token[[:space:]]*=[[:space:]]*//p' "$CONF" 2>/dev/null | head -1)
[ -n "${CRM_URL:-}" ] && [ -n "${TOKEN:-}" ] || {
    echo "$(date '+%F %T') нет crm_url/token в $CONF" >>"$LOG"; exit 0; }

mkdir -p "$SPOOL" 2>/dev/null
chmod 700 "$SPOOL" 2>/dev/null

VM_FILE=""
[ "$EVENT" = "voicemail" ] && VM_FILE="$EXTRA"
EXTENSION=""
[ "$EVENT" != "voicemail" ] && EXTENSION="$EXTRA"

STAMP=$(date '+%Y-%m-%d %H:%M:%S')
FILE="$SPOOL/$(date '+%s')-$$-${EVENT}.json"

# Кавычки в номере/имени файла не ждём (их формирует сам диалплан), но на
# всякий случай вычищаем — кривой JSON CRM отвергнет целиком.
clean() { printf '%s' "$1" | tr -d '"\\' ; }

cat > "$FILE" <<JSON
{"event":"$(clean "$EVENT")","group":"$(clean "$GROUP")",
 "linkedid":"$(clean "$LINKEDID")","uniqueid":"$(clean "$UNIQUEID")",
 "phone":"$(clean "$PHONE")","extension":"$(clean "$EXTENSION")",
 "voicemail_file":"$(clean "$VM_FILE")","occurred_at":"$STAMP"}
JSON

# Отправка — в фоне и со сжатым таймаутом: диалплан ждать не должен.
(
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
        -X POST "$CRM_URL/telephony/agent/missed/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        --data-binary @"$FILE" 2>/dev/null)
    case "$code" in
        200|201) rm -f "$FILE"
                 echo "$STAMP $EVENT $PHONE → CRM ok" >>"$LOG" ;;
        *)       echo "$STAMP $EVENT $PHONE → CRM код '$code', оставлено в спуле" >>"$LOG" ;;
    esac
) >/dev/null 2>&1 &

exit 0
