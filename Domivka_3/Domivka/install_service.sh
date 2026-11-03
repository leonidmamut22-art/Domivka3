#!/usr/bin/env bash
# Встановлює бота як службу: працює 24/7, сам стартує після перезавантаження сервера.
set -e
cd "$(dirname "$0")"
DIR=$(pwd); ME=$(whoami)
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip
python3 -m venv .venv
.venv/bin/pip install -q -r bot/requirements.txt
if ! grep -q '^BOT_TOKEN=.' .env 2>/dev/null; then
  read -rp "Токен бота від @BotFather: " T
  read -rp "Ваш Telegram ID (число від @userinfobot): " A
  printf "BOT_TOKEN=%s\nADMIN_IDS=%s\n" "$T" "$A" >> .env
fi
sudo tee /etc/systemd/system/domivka.service >/dev/null <<UNIT
[Unit]
Description=Domivka Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$ME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now domivka
sleep 3
sudo systemctl --no-pager status domivka | head -12
echo
echo "Готово. Логи: sudo journalctl -u domivka -f   Перезапуск: sudo systemctl restart domivka"
