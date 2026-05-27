#!/bin/sh
# Точка входа arbitr-runner: поднимаем Xvfb на :99, потом запускаем переданную
# команду (по умолчанию — celery worker очереди arbitr).
#
# Зачем Xvfb: kad.arbitr.ru детектит headless-Chrome и сразу выдаёт капчу.
# Headed-Chrome через Xvfb проходит проверки. Дисплей :99 фиксированный,
# чтобы и worker, и docker exec (kad_probe) ходили на один Xvfb.

set -e

# Подчищаем stale-lock'ы Xvfb на случай нечистого выхода предыдущего контейнера.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

# Стартуем Xvfb на :99, лог в /tmp/xvfb.log. nohup + disown — чтобы
# процесс не помер от SIGHUP при exec нижеследующего CMD.
nohup Xvfb :99 -screen 0 1920x1080x24 -ac +extension RANDR > /tmp/xvfb.log 2>&1 &
XVFB_PID=$!

# Ждём появления X-сокета (~1 сек обычно).
for i in 1 2 3 4 5 6 7 8; do
    if [ -e /tmp/.X11-unix/X99 ]; then
        break
    fi
    sleep 0.5
done

# Sanity-check: Xvfb должен быть жив.
if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "ERROR: Xvfb не стартовал, лог:" >&2
    cat /tmp/xvfb.log >&2 || true
    exit 1
fi

echo "Xvfb :99 запущен (pid=$XVFB_PID, display=$DISPLAY)"

# Передаём управление переданной команде (целевому процессу контейнера).
exec "$@"
