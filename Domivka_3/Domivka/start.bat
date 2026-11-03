@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=
where python >nul 2>&1 && set PY=python
if "%PY%"=="" (where py >nul 2>&1 && set PY=py -3)
if "%PY%"=="" (
  echo Python не знайдено. Встановіть його з python.org і поставте галочку "Add python.exe to PATH".
  echo Потім запустіть цей файл ще раз.
  pause
  exit /b 1
)
if not exist .venv (
  echo Перший запуск: готую середовище, зачекайте 1-2 хвилини...
  %PY% -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q -r bot\requirements.txt
python bot\bot.py
echo.
echo Бот зупинився. Якщо вище є повідомлення про помилку, прочитайте його.
pause
