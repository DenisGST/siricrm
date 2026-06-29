#!/bin/bash
# arbitr-snat-rotate.sh — назначает outbound IP четырём параллельным
# arbitr-runner контейнерам по расписанию (МСК) и перевыставляет
# per-runner iptables SNAT-правила (match по docker source-IP).
#
# Расписание (МСК) — какой IP активен в какие часы:
#   21:00–05:00 → 45.90.35.187
#   05:00–15:00 → 31.128.40.116
#   09:00–17:00 → 45.12.239.248
#   11:00–20:00 → 109.172.47.2
#   00:00–08:00 → 45.84.225.250
#
# Раннеры (a/b/c/d) получают по одному IP из активных. Если активных меньше
# четырёх — лишние раннеры пишут в Redis `arbitr:runner_ip:<id>` пустую
# строку (TTL 120с), сами таски это видят и спят тик. Порядок назначения
# IP по списку ACTIVE: 187, 116, 248, 002, 250 (новый — в конце, чтоб не
# сдвигать «насиженные» раннер-↔-IP в основных окнах).
#
# SNAT-правила (POSTROUTING table nat):
#   -s <docker_ip_runner_a> -d <kad> -j SNAT --to-source <assigned_ip_a>
#   -s <docker_ip_runner_b> -d <kad> -j SNAT --to-source <assigned_ip_b>
#   -s <docker_ip_runner_c> -d <kad> -j SNAT --to-source <assigned_ip_c>
#
# Docker source-IP контейнеров получаем через `docker inspect`. Они стабильны
# пока контейнер не пересоздан. Если контейнер не найден — правило не ставим
# и в Redis пишем «empty».

set -e

KAD_IP="185.129.103.123"
RUNNERS=("a" "b" "c" "d")
CONTAINERS=("siricrm-arbitr-runner-1" "siricrm-arbitr-runner-b-1" "siricrm-arbitr-runner-c-1" "siricrm-arbitr-runner-d-1")

HOUR=$(TZ=Europe/Moscow date +%H)
HOUR=$((10#$HOUR))

ACTIVE=()
{ [ $HOUR -ge 21 ] || [ $HOUR -lt 5 ]; } && ACTIVE+=("45.90.35.187")
{ [ $HOUR -ge 5 ] && [ $HOUR -lt 15 ]; } && ACTIVE+=("31.128.40.116")
{ [ $HOUR -ge 9 ] && [ $HOUR -lt 17 ]; } && ACTIVE+=("45.12.239.248")
{ [ $HOUR -ge 11 ] && [ $HOUR -lt 20 ]; } && ACTIVE+=("109.172.47.2")
{ [ $HOUR -ge 0 ] && [ $HOUR -lt 8 ]; }  && ACTIVE+=("45.84.225.250")

# Удаляем старые SNAT-правила для kad (по dst).
while true; do
    LINE=$(iptables -t nat -L POSTROUTING -n --line-numbers 2>/dev/null \
        | awk -v ip="$KAD_IP" '$0 ~ ip && $0 ~ /SNAT/ {print $1; exit}')
    [ -z "$LINE" ] && break
    iptables -t nat -D POSTROUTING "$LINE" 2>/dev/null || break
done

# Узнаём docker-IP каждого runner-контейнера. Если контейнер не запущен —
# IP пустой, правило не ставим, runner disabled.
declare -A RUNNER_DOCKER_IP
for i in "${!RUNNERS[@]}"; do
    R="${RUNNERS[$i]}"
    C="${CONTAINERS[$i]}"
    IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$C" 2>/dev/null || true)
    RUNNER_DOCKER_IP[$R]="$IP"
done

# Назначаем активные IP раннерам по порядку (a, b, c). Лишним — пусто.
declare -A RUNNER_ASSIGNED
N_ACTIVE=${#ACTIVE[@]}
for i in "${!RUNNERS[@]}"; do
    R="${RUNNERS[$i]}"
    if [ $i -lt $N_ACTIVE ]; then
        RUNNER_ASSIGNED[$R]="${ACTIVE[$i]}"
    else
        RUNNER_ASSIGNED[$R]=""
    fi
done

# Ставим per-runner SNAT-правила.
for R in "${RUNNERS[@]}"; do
    DOCKER_IP="${RUNNER_DOCKER_IP[$R]}"
    OUT_IP="${RUNNER_ASSIGNED[$R]}"
    if [ -n "$DOCKER_IP" ] && [ -n "$OUT_IP" ]; then
        iptables -t nat -I POSTROUTING 1 \
            -s "$DOCKER_IP" -d "$KAD_IP" -j SNAT --to-source "$OUT_IP"
    fi
done

iptables-save > /etc/iptables/rules.v4

# Пишем в Redis:
#   arbitr:runner_ip:<id> = assigned IP (или "" если disabled). TTL 120с
#                          (rotator тикает раз/мин — значение всегда свежее).
#   arbitr:current_snat_active = csv активных IP в этом часу (для UI).
#   arbitr:current_snat_ip = первый активный (для legacy UI, постепенно уберём).
ACTIVE_CSV=$(IFS=,; echo "${ACTIVE[*]}")
docker exec siricrm-redis-1 redis-cli SET arbitr:current_snat_active "$ACTIVE_CSV" EX 120 >/dev/null 2>&1 || true
docker exec siricrm-redis-1 redis-cli SET arbitr:current_snat_ip "${ACTIVE[0]:-}" EX 120 >/dev/null 2>&1 || true
for R in "${RUNNERS[@]}"; do
    docker exec siricrm-redis-1 redis-cli SET "arbitr:runner_ip:$R" "${RUNNER_ASSIGNED[$R]}" EX 120 >/dev/null 2>&1 || true
    docker exec siricrm-redis-1 redis-cli SET "arbitr:runner_docker_ip:$R" "${RUNNER_DOCKER_IP[$R]}" EX 120 >/dev/null 2>&1 || true
done

# Сводный лог для journalctl
MSG="hour=$HOUR active=[${ACTIVE[*]}]"
for R in "${RUNNERS[@]}"; do
    MSG="$MSG ${R}=${RUNNER_ASSIGNED[$R]:-disabled}(${RUNNER_DOCKER_IP[$R]:-no-container})"
done
logger -t arbitr-snat "$MSG"
