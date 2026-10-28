#!/bin/bash
# =====================================================================
# Сборка ПланировщикПроизводства_v2.app на macOS — двойной клик.
# Требуется Python 3 (https://www.python.org/downloads/macos/) и интернет
# (для установки библиотек на время сборки).
#
# Перед первым запуском (один раз) в Терминале:  chmod +x build_mac.command
# Затем: двойной клик  (или правый клик -> «Открыть»).
# Готовый .app появится на Рабочем столе.
# =====================================================================
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display alert "Не найден Python 3" message "Установите Python 3 с python.org и запустите снова."' >/dev/null 2>&1 || true
  echo "Нужен Python 3: https://www.python.org/downloads/macos/"
  read -r -p "Enter для выхода..." _; exit 1
fi

echo "[1/4] Окружение и библиотеки (первый раз — пара минут)..."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt pyinstaller pillow

echo "[2/4] Иконка (.icns)..."
python make_icon.py
rm -rf app.iconset && mkdir -p app.iconset
sips -z 16 16     app_icon_1024.png --out app.iconset/icon_16x16.png      >/dev/null
sips -z 32 32     app_icon_1024.png --out app.iconset/icon_16x16@2x.png   >/dev/null
sips -z 32 32     app_icon_1024.png --out app.iconset/icon_32x32.png      >/dev/null
sips -z 64 64     app_icon_1024.png --out app.iconset/icon_32x32@2x.png   >/dev/null
sips -z 128 128   app_icon_1024.png --out app.iconset/icon_128x128.png    >/dev/null
sips -z 256 256   app_icon_1024.png --out app.iconset/icon_128x128@2x.png >/dev/null
sips -z 256 256   app_icon_1024.png --out app.iconset/icon_256x256.png    >/dev/null
sips -z 512 512   app_icon_1024.png --out app.iconset/icon_256x256@2x.png >/dev/null
sips -z 512 512   app_icon_1024.png --out app.iconset/icon_512x512.png    >/dev/null
cp app_icon_1024.png app.iconset/icon_512x512@2x.png
iconutil -c icns app.iconset -o app.icns

echo "[3/4] Сборка .app (PyInstaller)..."
pyinstaller --noconfirm --clean --windowed \
  --name "ПланировщикПроизводства_v2" \
  --icon app.icns \
  --collect-all gspread --collect-all google \
  --collect-all pydantic --collect-all pydantic_settings \
  --collect-submodules pandas \
  --hidden-import xlrd --hidden-import openpyxl \
  main_gui.py

echo "[4/4] Копирую на Рабочий стол..."
rm -rf "$HOME/Desktop/ПланировщикПроизводства_v2.app"
cp -R "dist/ПланировщикПроизводства_v2.app" "$HOME/Desktop/"
open "$HOME/Desktop"
osascript -e 'display notification "ПланировщикПроизводства_v2.app на Рабочем столе" with title "Сборка завершена"' >/dev/null 2>&1 || true
echo ""
echo "ГОТОВО. Приложение на Рабочем столе."
echo "Чтобы оно работало — положите рядом с .app файлы .env и credentials.json."
read -r -p "Enter для выхода..." _
