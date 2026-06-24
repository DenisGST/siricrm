# arbitr-snat-rotate — ротация SNAT IP для парсера kad

Host-side скрипт + systemd timer на prod. Каждую минуту:
1. Смотрит МСК-час.
2. Выбирает активные IP по расписанию.
3. Случайный из активных → SNAT-правило для kad.arbitr.ru.

Расписание (МСК):
- 21:00–05:00 → 45.90.35.187
- 05:00–15:00 → 31.128.40.116
- 09:00–17:00 → 45.12.239.248
- 11:00–20:00 → 109.172.47.2

В пересекающихся окнах — случайный из активных.
В 20:00–21:00 нет активных — SNAT снимается (трафик пойдёт с primary).

Установка:
    sudo cp ops/arbitr-snat-rotate.sh /usr/local/bin/
    sudo chmod +x /usr/local/bin/arbitr-snat-rotate.sh
    sudo cp ops/arbitr-snat-rotate.{service,timer} /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now arbitr-snat-rotate.timer

Логи: `journalctl -t arbitr-snat -n 50`
Диагностика: `iptables -t nat -L POSTROUTING -n -v --line-numbers`
