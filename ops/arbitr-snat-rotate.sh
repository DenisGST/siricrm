#!/bin/bash
# arbitr-snat-rotate.sh — назначает outbound IP четырём параллельным
# arbitr-runner контейнерам по расписанию (МСК) и перевыставляет
# per-runner iptables SNAT-правила (match по docker source-IP).
#
# Каждый runner имеет «домашний» IP (или пару IP). Rotator для каждого
# runner'а выбирает первый активный из его списка. Так гарантируется что
# runner-d НЕ игнорируется когда активных IP < 4 — раньше был баг: rotator
# раздавал ACTIVE-IP по индексу [0]→a, [1]→b, [2]→c, [3]→d, а активных
# всегда ≤ 3 → d был disabled 24/7.
#
# Домашние IP:
#   a → 45.84.225.250 (00–08 МСК) ИЛИ 45.90.35.187 (21–05 МСК)
#       — ночной; 00–05 оба активны, приоритет 250 (свежий IP чаще).
#   b → 31.128.40.116 (05–15 МСК)  — утренний
#   c → 45.12.239.248 (09–17 МСК)  — дневной 1
#   d → 109.172.47.2  (11–20 МСК)  — дневной 2
#
# Покрытие runner'ов по часам (МСК):
#   00–05  a(250)                 = 1
#   05–08  a(250) + b(116)        = 2
#   08–09  b(116)                 = 1
#   09–11  b(116) + c(248)        = 2
#   11–15  b(116) + c(248) + d(002) = 3  (пик параллельности)
#   15–17  c(248) + d(002)        = 2
#   17–20  d(002)                 = 1
#   20–21  ничего активного       = 0
#   21–24  a(187)                 = 1
#
# SNAT-правила (POSTROUTING table nat) и в Redis `arbitr:runner_ip:<id>`
# для каждого активного runner'а. Docker source-IP берём через `docker inspect`.

set -e

KAD_IP="185.129.103.123"
RUNNERS=("a" "b" "c" "d")
CONTAINERS=("siricrm-arbitr-runner-1" "siricrm-arbitr-runner-b-1" "siricrm-arbitr-runner-c-1" "siricrm-arbitr-runner-d-1")

# Домашние IP каждого runner'а — упорядоченный список; выбираем первый
# активный из ACTIVE. Разделитель — пробел.
declare -A RUNNER_HOME_IPS
RUNNER_HOME_IPS["a"]="45.84.225.250 45.90.35.187"
RUNNER_HOME_IPS["b"]="31.128.40.116"
RUNNER_HOME_IPS["c"]="45.12.239.248"
RUNNER_HOME_IPS["d"]="109.172.47.2"

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

# Назначаем каждому runner'у первый АКТИВНЫЙ IP из его домашнего списка.
# Если ни один из «домашних» IP не активен сейчас — runner disabled.
declare -A RUNNER_ASSIGNED
in_active() {
    local needle="$1"
    for x in "${ACTIVE[@]}"; do
        [ "$x" = "$needle" ] && return 0
    done
    return 1
}
for R in "${RUNNERS[@]}"; do
    ASSIGNED=""
    for IP in ${RUNNER_HOME_IPS[$R]}; do
        if in_active "$IP"; then
            ASSIGNED="$IP"
            break
        fi
    done
    RUNNER_ASSIGNED[$R]="$ASSIGNED"
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
