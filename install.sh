#!/bin/bash
# Установщик Xray VLESS+REALITY + Hysteria2 + SplitHTTP + Telegram-бот
# Запуск: sudo bash install.sh

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
echo -e "${GREEN}=== Установка VPN-сервера (Hysteria2, VLESS, SplitHTTP) ===${NC}"

if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Пожалуйста, запустите скрипт с правами root (sudo).${NC}"; exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- 1. Системные пакеты ----------
echo -e "${YELLOW}[1/10] Установка системных пакетов...${NC}"
apt update
apt install -y curl wget unzip git jq python3 python3-pip python3-venv qrencode ufw openssl

# ---------- 2. Установка Xray ----------
echo -e "${YELLOW}[2/10] Установка Xray...${NC}"
if ! command -v xray &>/dev/null; then
    bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
else
    echo "Xray уже установлен."
fi

# ---------- 3. Генерация ключей REALITY ----------
echo -e "${YELLOW}[3/10] Генерация ключей REALITY...${NC}"
XRAY_KEYS_OUTPUT=$(xray x25519)
PRIVATE_KEY=$(echo "$XRAY_KEYS_OUTPUT" | grep -E 'Private[ ]?Key:' | awk '{print $NF}')
PUBLIC_KEY=$(echo "$XRAY_KEYS_OUTPUT" | grep -E 'Public[ ]?Key:|Password[ ]?\(PublicKey\):' | awk '{print $NF}')
SHORT_ID=$(openssl rand -hex 8)
HYSTERIA_PASSWORD=$(openssl rand -base64 24)

if [ -z "$PRIVATE_KEY" ] || [ -z "$PUBLIC_KEY" ]; then
    echo -e "${RED}Не удалось извлечь ключи REALITY.${NC}"; exit 1
fi

# ---------- 4. Сертификат для Hysteria2 ----------
echo -e "${YELLOW}[4/10] Создание сертификата для Hysteria2...${NC}"
mkdir -p /etc/xray
openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout /etc/xray/key.pem -out /etc/xray/cert.pem \
    -subj "/CN=www.microsoft.com"
chmod 600 /etc/xray/key.pem /etc/xray/cert.pem

# ---------- 5. Конфиг Xray ----------
echo -e "${YELLOW}[5/10] Создание config.json...${NC}"
CONFIG_FILE="/usr/local/etc/xray/config.json"
cp "$SCRIPT_DIR/config.json.example" "$CONFIG_FILE"
sed -i "s/PRIVATE_KEY_PLACEHOLDER/$PRIVATE_KEY/g" "$CONFIG_FILE"
sed -i "s/SHORT_ID_PLACEHOLDER/$SHORT_ID/g" "$CONFIG_FILE"
sed -i "s/HYSTERIA_PASSWORD_PLACEHOLDER/$HYSTERIA_PASSWORD/g" "$CONFIG_FILE"
chmod 644 "$CONFIG_FILE"

# ---------- 6. Запуск Xray ----------
echo -e "${YELLOW}[6/10] Проверка и запуск Xray...${NC}"
if ! xray -test -config "$CONFIG_FILE"; then
    echo -e "${RED}Ошибка в конфигурации Xray!${NC}"; exit 1
fi
systemctl restart xray
systemctl enable xray
sleep 2
if systemctl is-active --quiet xray; then
    echo -e "${GREEN}Xray запущен.${NC}"
else
    echo -e "${RED}Xray не запустился!${NC}"; exit 1
fi

# ---------- 7. NAT и IP forward ----------
echo -e "${YELLOW}[7/10] Настройка NAT...${NC}"
IFACE=$(ip route | grep default | awk '{print $5}')
iptables -t nat -A POSTROUTING -o "$IFACE" -j MASQUERADE
apt install -y iptables-persistent
netfilter-persistent save
sysctl -w net.ipv4.ip_forward=1

# ---------- 8. Окружение бота ----------
echo -e "${YELLOW}[8/10] Установка Python-окружения...${NC}"
BOT_DIR="/opt/xray-bot"
mkdir -p "$BOT_DIR"
cp "$SCRIPT_DIR/bot.py" "$BOT_DIR/"
cp "$SCRIPT_DIR/cleanup_expired.py" "$BOT_DIR/"
cp "$SCRIPT_DIR/config.py.example" "$BOT_DIR/config.py"
cp "$SCRIPT_DIR/requirements.txt" "$BOT_DIR/"

cd "$BOT_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Запрос данных
echo -e "${YELLOW}Введите данные для Telegram-бота:${NC}"
read -p "Токен бота: " BOT_TOKEN
read -p "Ваш Telegram ID: " ADMIN_ID
SERVER_IP=$(curl -s ifconfig.co)
read -p "Публичный IP сервера [$SERVER_IP]: " INPUT_IP
SERVER_IP=${INPUT_IP:-$SERVER_IP}

# Подстановка в config.py
sed -i "s/^TELEGRAM_BOT_TOKEN = .*/TELEGRAM_BOT_TOKEN = \"$BOT_TOKEN\"/" config.py
sed -i "s/^ALLOWED_USERS = .*/ALLOWED_USERS = {$ADMIN_ID}/" config.py
sed -i "s/^SERVER_IP = .*/SERVER_IP = \"$SERVER_IP\"/" config.py
sed -i "s/^PUBLIC_KEY = .*/PUBLIC_KEY = \"$PUBLIC_KEY\"/" config.py
sed -i "s/^SHORT_ID = .*/SHORT_ID = \"$SHORT_ID\"/" config.py
sed -i "s/^HYSTERIA_PASSWORD = .*/HYSTERIA_PASSWORD = \"$HYSTERIA_PASSWORD\"/" config.py
sed -i "s/^SPLIT_PORT = .*/SPLIT_PORT = 8081/" config.py
sed -i "s|^SPLIT_PATH = .*|SPLIT_PATH = \"/stream\"|" config.py

# ---------- 9. Сервисы ----------
echo -e "${YELLOW}[9/10] Установка systemd-сервисов...${NC}"
cp "$SCRIPT_DIR/xray-bot.service" /etc/systemd/system/
cp "$SCRIPT_DIR/xray-cleanup.service" /etc/systemd/system/
cp "$SCRIPT_DIR/xray-cleanup.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable xray-bot
systemctl restart xray-bot
systemctl enable xray-cleanup.timer
systemctl start xray-cleanup.timer

sleep 2
if systemctl is-active --quiet xray-bot; then
    echo -e "${GREEN}Бот запущен.${NC}"
else
    echo -e "${RED}Бот не запустился! Проверьте journalctl -u xray-bot${NC}"
fi

# ---------- 10. Автотестирование ----------
echo -e "${YELLOW}[10/10] Тестирование...${NC}"
echo "VLESS 443: $(ss -tulpn | grep -q ':443.*xray' && echo OK || echo FAIL)"
echo "Hysteria2 8443: $(ss -tulpn | grep -q ':8443.*xray' && echo OK || echo FAIL)"
echo "SplitHTTP 8081: $(ss -tulpn | grep -q ':8081.*xray' && echo OK || echo FAIL)"
echo "Бот: $(systemctl is-active xray-bot)"

echo -e "${GREEN}=== Установка завершена ===${NC}"
echo "Публичный ключ REALITY: $PUBLIC_KEY"
echo "ShortId: $SHORT_ID"
echo "Пароль Hysteria2: $HYSTERIA_PASSWORD"