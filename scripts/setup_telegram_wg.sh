#!/bin/bash
# ============================================================
# Настройка WireGuard на хосте для маршрутизации
# трафика Telegram через AmneziaVPN
#
# Использование:
#   sudo bash scripts/setup_telegram_wg.sh
# ============================================================

set -euo pipefail

WG_IFACE="tg0"
WG_CONF="/etc/wireguard/${WG_IFACE}.conf"

# Диапазоны IP-адресов серверов Telegram
TELEGRAM_RANGES=(
  "149.154.160.0/20"
  "91.108.4.0/22"
  "91.108.8.0/22"
  "91.108.12.0/22"
  "91.108.16.0/22"
  "91.108.56.0/22"
  "160.79.104.0/20"
)

# ---- Зависимости -----------------------------------------------
if ! command -v wg &>/dev/null; then
  echo "→ Устанавливаем wireguard-tools..."
  apt-get update -qq && apt-get install -y wireguard-tools
fi

# ---- Конфиг ----------------------------------------------------
cat > "$WG_CONF" <<'EOF'
[Interface]
Address = 10.8.1.4/32
PrivateKey = UGBWsi79d3k3JQK2+/oFNZ/reyLWlz+SmQiDf+uNzHs=
# DNS намеренно не указан — Docker-контейнеры используют свой резолвер.
# Table = off — НЕ добавляем маршруты автоматически, сделаем вручную ниже.
Table = off
PostUp   = ip route add 149.154.160.0/20 dev tg0 && \
           ip route add 91.108.4.0/22 dev tg0 && \
           ip route add 91.108.8.0/22 dev tg0 && \
           ip route add 91.108.12.0/22 dev tg0 && \
           ip route add 91.108.16.0/22 dev tg0 && \
           ip route add 91.108.56.0/22 dev tg0 && \
           ip route add 160.79.104.0/20 dev tg0
PostDown = ip route del 149.154.160.0/20 dev tg0 2>/dev/null; \
           ip route del 91.108.4.0/22 dev tg0 2>/dev/null; \
           ip route del 91.108.8.0/22 dev tg0 2>/dev/null; \
           ip route del 91.108.12.0/22 dev tg0 2>/dev/null; \
           ip route del 91.108.16.0/22 dev tg0 2>/dev/null; \
           ip route del 91.108.56.0/22 dev tg0 2>/dev/null; \
           ip route del 160.79.104.0/20 dev tg0 2>/dev/null; true

[Peer]
PublicKey    = AaN/xHksznpiya+5+lqipVXeTIx2D5nMAUTFxicUoAU=
PresharedKey = SCk60y/F0MzZMMnRHGTnOq/gAMWZRZm9lvNj16GGQWY=
Endpoint     = 72.56.73.137:37539
# Только диапазоны Telegram — остальной трафик идёт напрямую
AllowedIPs   = 149.154.160.0/20, 91.108.4.0/22, 91.108.8.0/22,
               91.108.12.0/22, 91.108.16.0/22, 91.108.56.0/22,
               160.79.104.0/20
PersistentKeepalive = 25
EOF

chmod 600 "$WG_CONF"

# ---- Запуск ----------------------------------------------------
echo "→ Включаем и запускаем wg-quick@${WG_IFACE}..."
systemctl enable "wg-quick@${WG_IFACE}"
systemctl restart "wg-quick@${WG_IFACE}"

echo ""
echo "=== Статус ==="
wg show "$WG_IFACE" 2>/dev/null || echo "Интерфейс ещё поднимается..."

echo ""
echo "=== Маршруты Telegram ==="
for r in "${TELEGRAM_RANGES[@]}"; do
  ip route show "$r" 2>/dev/null && echo "  ✓ $r" || echo "  ✗ $r (не найден)"
done

echo ""
echo "=== Проверка связи с Telegram DC ==="
# DC1
ping -c 1 -W 3 149.154.175.50 &>/dev/null && echo "  ✓ DC1 (149.154.175.50) доступен" || echo "  ✗ DC1 недоступен"
# DC2
ping -c 1 -W 3 149.154.167.51 &>/dev/null && echo "  ✓ DC2 (149.154.167.51) доступен" || echo "  ✗ DC2 недоступен"
# DC4
ping -c 1 -W 3 149.154.167.91 &>/dev/null && echo "  ✓ DC4 (149.154.167.91) доступен" || echo "  ✗ DC4 недоступен"

echo ""
echo "✅ Готово! Перезапусти userbot:"
echo "   docker compose restart userbot"
