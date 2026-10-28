#!/bin/bash
# =====================================================================
# Планировщик Производства v2.0 — запуск на macOS (из исходников).
# Требуется Python 3 (скачать с https://www.python.org/downloads/macos/).
# Первый запуск создаёт окружение и ставит зависимости (нужен интернет).
#
# Как пользоваться на Mac:
#   1) Положите ВСЮ папку с исходниками в одно место.
#   2) В Терминале один раз выполните:  chmod +x run_mac.command
#   3) Двойной клик по run_mac.command  (или: правый клик → «Открыть»).
# =====================================================================
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display alert "Не найден Python 3" message "Установите Python 3 с python.org, затем запустите снова."' >/dev/null 2>&1
  echo "Python 3 не найден. Установите его с https://www.python.org/downloads/macos/"
  read -r -p "Нажмите Enter для выхода..." _
  exit 1
fi

# Изолированное окружение рядом со скриптом
if [ ! -d ".venv" ]; then
  echo "Первая настройка: создаю окружение..."
  python3 -m venv .venv || { echo "Не удалось создать venv"; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "Проверяю зависимости (может занять минуту при первом запуске)..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || {
  echo "Не удалось установить зависимости. Проверьте интернет."
  read -r -p "Нажмите Enter для выхода..." _
  exit 1
}

echo "Запуск приложения..."
python main_gui.py
