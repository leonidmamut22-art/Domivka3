#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Перший запуск: готую середовище..."
  python3 -m venv .venv || { echo "Встановіть python3-venv: sudo apt install python3-venv"; exit 1; }
fi
.venv/bin/pip install -q -r bot/requirements.txt
exec .venv/bin/python bot/bot.py
