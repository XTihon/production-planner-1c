#!/bin/bash
# =====================================================================
# Планировщик Производства — самоустановка и запуск на macOS.
# ПЕРВЫЙ раз: правый клик по файлу -> «Открыть» -> «Открыть».
# Дальше — обычный двойной клик.
# Если двойной клик не срабатывает: открыть Терминал, набрать  bash  (с пробелом),
# перетащить этот файл в окно Терминала и нажать Enter.
# Первый запуск ставит Python и библиотеки (нужен интернет).
# =====================================================================
cd "$(dirname "$0")" || exit 1
say(){ printf "\n==> %s\n" "$1"; }
fail(){ printf "\n!! %s\n" "$1"; read -r -p "Нажмите Enter для выхода..." _; exit 1; }

# Найти Python с поддержкой графики (tkinter). python.org-сборка содержит Tk.
find_py(){
  local c
  for c in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 \
           /usr/local/bin/python3 /opt/homebrew/bin/python3 python3; do
    if "$c" -c 'import tkinter' >/dev/null 2>&1; then printf '%s' "$c"; return 0; fi
  done
  return 1
}

PY="$(find_py || true)"

# Если нет — поставить официальный Python (один раз; нужен пароль администратора)
if [ -z "$PY" ]; then
  say "Первый запуск: устанавливаю Python (нужно один раз)"
  PKG="/tmp/pp_python.pkg"
  curl -fL "https://www.python.org/ftp/python/3.12.7/python-3.12.7-macos11.pkg" -o "$PKG" \
    || fail "Не удалось скачать Python. Проверьте интернет и запустите снова."
  printf "\nСейчас Mac попросит ПАРОЛЬ от вашего компьютера (учётной записи).\n"
  printf "Введите его — символы на экране НЕ появляются, это нормально — и нажмите Enter.\n\n"
  sudo installer -pkg "$PKG" -target / || fail "Установка Python не удалась (нужны права администратора)."
  PY="$(find_py || true)"
fi
[ -z "$PY" ] && fail "Python не найден после установки."

# Библиотеки ставятся один раз; маркер .setup_done — чтобы не делать повторно.
if [ ! -f ".setup_done" ]; then
  say "Настраиваю программу (первый раз — пара минут)"
  "$PY" -m venv .venv || fail "Не удалось создать окружение."
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt \
    || fail "Не удалось установить библиотеки (проверьте интернет)."
  touch .setup_done
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

say "Запуск..."
python main_gui.py || { printf "\n!! Программа завершилась с ошибкой (см. выше).\n"; read -r -p "Нажмите Enter для выхода..." _; }
