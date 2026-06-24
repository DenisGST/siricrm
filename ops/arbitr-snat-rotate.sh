#!/bin/bash
# arbitr-snat-rotate.sh — выбирает активный IP для парсера kad.arbitr.ru
# по расписанию (МСК) и перевыставляет iptables SNAT-правило.
#
# Расписание:
#   21:00–05:00 → 45.90.35.187
#   05:00–15:00 → 31.128.40.116
#   09:00–17:00 → 45.12.239.248
#   11:00–20:00 → 109.172.47.2
# В пересекающихся окнах — случайный выбор из активных IP (rebalance kad).
# В окне 20:00–21:00 ни один IP не активен → удаляем SNAT-правило вообще
# (парсер пойдёт с основного 45.90.35.187 — это компромисс vs полная пауза).
set -e

KAD_IP="185.129.103.123"
ALL_IPS=("45.90.35.187" "31.128.40.116" "45.12.239.248" "109.172.47.2")

HOUR=$(TZ=Europe/Moscow date +%H)
HOUR=$((10#$HOUR))  # «09»→9, не восьмеричный

ACTIVE=()
{ [ $HOUR -ge 21 ] || [ $HOUR -lt 5 ]; } && ACTIVE+=("45.90.35.187")
{ [ $HOUR -ge 5 ] && [ $HOUR -lt 15 ]; } && ACTIVE+=("31.128.40.116")
{ [ $HOUR -ge 9 ] && [ $HOUR -lt 17 ]; } && ACTIVE+=("45.12.239.248")
{ [ $HOUR -ge 11 ] && [ $HOUR -lt 20 ]; } && ACTIVE+=("109.172.47.2")

# Удаляем ВСЕ старые SNAT-правила для kad (могло быть несколько, или прежний IP).
while true; do
    LINE=$(iptables -t nat -L POSTROUTING -n --line-numbers 2>/dev/null \
        | awk -v ip="$KAD_IP" '$0 ~ ip && $0 ~ /SNAT/ {print $1; exit}')
    [ -z "$LINE" ] && break
    iptables -t nat -D POSTROUTING "$LINE" 2>/dev/null || break
done

N=${#ACTIVE[@]}
if [ $N -eq 0 ]; then
    logger -t arbitr-snat "no active IP at $HOUR:xx — SNAT removed (parser will use default 45.90.35.187)"
    iptables-save > /etc/iptables/rules.v4
    exit 0
fi

# Случайный из активных. $RANDOM 0..32767.
IDX=$((RANDOM % N))
IP="${ACTIVE[$IDX]}"

iptables -t nat -I POSTROUTING 1 -d "$KAD_IP" -j SNAT --to-source "$IP"
iptables-save > /etc/iptables/rules.v4

logger -t arbitr-snat "hour=$HOUR active=[${ACTIVE[*]}] picked=$IP"
