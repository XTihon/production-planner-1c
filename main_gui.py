"""
main_gui.py
===========
Desktop GUI front-end for the production planner (tkinter / ttk).

A non-technical production manager picks the two 1C export files, optionally
keeps "safe mode" on (a dry-run), and clicks one big button. The heavy pipeline
runs in a BACKGROUND THREAD so the window never freezes during Google Sheets
network calls; all logs stream live into the on-screen console, and the result
is announced with a native message box.

The GUI does not duplicate any business logic — it builds the CLI argv and calls
``main.run()``. ``.env`` and ``credentials.json`` are located by ``config.py``
relative to the app directory (the project folder, or next to the .exe when
frozen), so packaging "just works" if those files sit next to the executable.

Run in development:
    python main_gui.py

Build a standalone .exe (PowerShell, from the project dir):
    pyinstaller --noconfirm --clean --onefile --windowed ^
        --name "ПланировщикПроизводства_v2" ^
        --collect-all gspread --collect-all google ^
        --collect-all pydantic --collect-all pydantic_settings ^
        --collect-submodules pandas ^
        main_gui.py
(then place .env + credentials.json next to the produced .exe — see README)
"""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

APP_TITLE = "Автоматический Планировщик Производства v2.0"
DEFAULT_RATING_SHEET = "рейтинг май 2026"
DEFAULT_STOCK_SHEET = "TDSheet"


def _configured_mode() -> str:
    """Pre-select whatever PLANNING_MODE says, so .env stays the single source."""
    try:
        from config import get_settings
        return get_settings().planning_mode
    except Exception:  # noqa: BLE001 — a broken .env must not block the UI
        return "sales"


def _list_sheet_names(path: str) -> list[str]:
    """Best-effort worksheet-name listing; [] on any failure (file not picked
    yet, still mid-copy, wrong/corrupt format)."""
    try:
        import pandas as pd
        return pd.ExcelFile(path).sheet_names
    except Exception:
        return []

# main.run() exit code -> (messagebox title, message). 0 is handled separately.
EXIT_MESSAGES = {
    1: ("Непредвиденная ошибка",
        "Произошла непредвиденная ошибка. Подробности — в журнале ниже."),
    2: ("Ошибка конфигурации",
        "Не найдены или некорректны .env / credentials.json.\n"
        "Убедитесь, что оба файла лежат рядом с программой. Подробности — в журнале."),
    3: ("Ошибка данных 1С",
        "Проблема с файлами 1С: отсутствует столбец, файл не найден или нет данных.\n"
        "Проверьте выбранные файлы и названия листов. Подробности — в журнале."),
    4: ("Ошибка Google Таблицы",
        "Не удалось подключиться или записать в Google Таблицу\n"
        "(права доступа, квоты API или нет интернета). Подробности — в журнале."),
}


class _QueueStream:
    """File-like object that forwards everything written to it into a queue.

    Installed as sys.stdout/sys.stderr so that both ``print()`` and logger.py's
    StreamHandler end up in the GUI console — thread-safely (queue.Queue is
    safe; the Tk widget is only ever touched from the main thread).
    """

    def __init__(self, q: "queue.Queue"):
        self._q = q

    def write(self, text):
        if text:
            self._q.put(text)
        return len(text) if text else 0

    def flush(self):
        pass

    def isatty(self):
        return False


class PlannerApp:
    POLL_MS = 80

    def __init__(self, root: tk.Tk):
        self.root = root
        self.log_q: "queue.Queue" = queue.Queue()
        self._running = False

        # Redirect process output into the GUI console for the app's lifetime.
        sys.stdout = _QueueStream(self.log_q)
        sys.stderr = _QueueStream(self.log_q)

        self._build_styles()
        self._build_ui()
        self.root.after(self.POLL_MS, self._poll_log)

    # ----------------------------------------------------------------- styles
    def _build_styles(self):
        self.root.title(APP_TITLE)
        self.root.geometry("840x660")
        self.root.minsize(720, 560)
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 15))
        style.configure("Hint.TLabel", foreground="#666", font=("Segoe UI", 9))
        style.configure("Run.TButton", font=("Segoe UI Semibold", 12), padding=12)
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("TCheckbutton", font=("Segoe UI", 10))

    # --------------------------------------------------------------------- UI
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)  # log row grows

        ttk.Label(main, text=APP_TITLE, style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10))

        # --- inputs ---
        inp = ttk.LabelFrame(main, text=" Исходные файлы 1С ", padding=10)
        inp.grid(row=1, column=0, sticky="ew")
        inp.columnconfigure(1, weight=1)

        self.var_rating = tk.StringVar()
        self.var_rating_sheet = tk.StringVar(value=DEFAULT_RATING_SHEET)
        self.var_stock = tk.StringVar()
        self.var_stock_sheet = tk.StringVar(value=DEFAULT_STOCK_SHEET)

        self._file_row(inp, 0, "Файл рейтинга (.xlsx):", self.var_rating,
                       self._browse_rating)
        self.combo_rating_sheet = self._combo_row(
            inp, 1, "Лист рейтинга:", self.var_rating_sheet)
        self._file_row(inp, 2, "Файл остатков (.xls/.xlsx):", self.var_stock,
                       self._browse_stock)
        self.combo_stock_sheet = self._combo_row(
            inp, 3, "Лист остатков:", self.var_stock_sheet)

        self._build_stores_box(main)

        # --- options + action ---
        act = ttk.Frame(main, padding=(0, 12))
        act.grid(row=3, column=0, sticky="ew")
        act.columnconfigure(0, weight=1)

        mode = ttk.LabelFrame(act, text=" Как считать потребность ", padding=8)
        mode.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.var_mode = tk.StringVar(value=_configured_mode())
        ttk.Radiobutton(
            mode, text="Восполнять проданное — сколько продали, минус что уже есть",
            value="sales", variable=self.var_mode).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            mode, text="Держать запас на точках — по N штук каждой ходовой позиции",
            value="stores", variable=self.var_mode).grid(row=1, column=0, sticky="w")
        ttk.Label(mode, text="Размер учитывается отдельно: чёрный S и чёрный M — "
                             "разные позиции.",
                  style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 0))

        self.var_safe = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            act, text="Безопасный режим (Тест без записи в Google Таблицу)",
            variable=self.var_safe).grid(row=1, column=0, sticky="w")
        ttk.Label(act, text="Снимите галочку, чтобы записать планы в таблицу клиента.",
                  style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=(2, 10))

        self.btn_run = ttk.Button(
            act, text="РАССЧИТАТЬ И ОБНОВИТЬ ПЛАНЫ", style="Run.TButton",
            command=self._on_run)
        self.btn_run.grid(row=3, column=0, sticky="ew")

        self.progress = ttk.Progressbar(act, mode="indeterminate")
        self.progress.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        # --- log ---
        logf = ttk.LabelFrame(main, text=" Журнал выполнения ", padding=(6, 4))
        logf.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(
            logf, height=14, wrap="word", state="disabled",
            font=("Consolas", 9), background="#1e1e1e", foreground="#dcdcdc",
            insertbackground="#dcdcdc")
        self.log.grid(row=0, column=0, sticky="nsew")

        # --- status bar ---
        self.var_status = tk.StringVar(value="Готово к работе.")
        ttk.Label(main, textvariable=self.var_status, style="Hint.TLabel",
                  anchor="w").grid(row=5, column=0, sticky="ew", pady=(6, 0))

    # ----------------------------------------------------------- stores editor
    ROLES = ("Пополнять", "Только учитывать", "Не учитывать", "Склад готовой продукции")

    def _build_stores_box(self, parent):
        """Let the manager assign every warehouse a role without editing .env.

        Captions are pulled straight from the stock export, because a mistyped
        warehouse name drops that location from the plan silently.
        """
        box = ttk.LabelFrame(parent, text=" Магазины и склады ", padding=10)
        box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        box.columnconfigure(0, weight=1)

        ttk.Label(box, style="Hint.TLabel", text=
                  "«Пополнять» — точки, под которые планируется производство.   "
                  "«Только учитывать» — примерка, химчистка, ремонт: остатки "
                  "засчитываются, шить под них не надо.").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.stores_area = ttk.Frame(box)
        self.stores_area.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.stores_area.columnconfigure(0, weight=1)
        self._store_rows: list[tuple[str, tk.StringVar]] = []

        add = ttk.Frame(box)
        add.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        add.columnconfigure(0, weight=1)
        self.var_new_store = tk.StringVar()
        ttk.Entry(add, textvariable=self.var_new_store).grid(row=0, column=0, sticky="ew")
        ttk.Button(add, text="Добавить точку", command=self._add_store).grid(
            row=0, column=1, padx=(8, 0))
        ttk.Button(add, text="Взять из файла остатков",
                   command=self._load_stores_from_file).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(add, text="Сохранить", command=self._save_stores).grid(
            row=0, column=3, padx=(8, 0))

        self._load_stores_from_config()

    def _render_stores(self, roles: dict):
        for child in self.stores_area.winfo_children():
            child.destroy()
        self._store_rows = []
        for r, (name, role) in enumerate(roles.items()):
            ttk.Label(self.stores_area, text=name).grid(
                row=r, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=role)
            ttk.Combobox(self.stores_area, textvariable=var, values=self.ROLES,
                         state="readonly", width=26).grid(row=r, column=1,
                                                          sticky="e", pady=2)
            self._store_rows.append((name, var))

    def _current_roles(self) -> dict:
        return {name: var.get() for name, var in self._store_rows}

    def _load_stores_from_config(self):
        try:
            from config import get_settings
            cfg = get_settings()
        except Exception:  # noqa: BLE001 — a broken .env must not block the UI
            self._render_stores({})
            return
        roles = {s: "Пополнять" for s in cfg.target_stores}
        roles.update({s: "Только учитывать" for s in cfg.stock_only_stores})
        roles[cfg.finished_goods_warehouse] = "Склад готовой продукции"
        self._render_stores(roles)

    def _load_stores_from_file(self):
        path = self.var_stock.get().strip()
        sheet = self.var_stock_sheet.get().strip()
        if not path or not Path(path).is_file():
            messagebox.showinfo("Файл остатков",
                                "Сначала выберите файл остатков.")
            return
        try:
            from pipeline_1c import list_warehouses
            found = list_warehouses(Path(path), sheet)
        except Exception as exc:  # noqa: BLE001 — show the reason, never crash
            messagebox.showwarning("Не удалось прочитать файл",
                                   f"Склады не прочитались:\n{exc}")
            return
        if not found:
            messagebox.showinfo("Склады не найдены",
                                "В этом файле не нашлось строки со складами.\n"
                                "Проверьте, что выбран правильный лист.")
            return
        known = self._current_roles()
        merged = {name: known.get(name, "Не учитывать") for name in found}
        for name, role in known.items():          # keep points absent from the export
            merged.setdefault(name, role)
        self._render_stores(merged)
        self.var_status.set(f"Прочитано складов из файла: {len(found)}. "
                            "Проставьте роли и нажмите «Сохранить».")

    def _add_store(self):
        name = self.var_new_store.get().strip()
        if not name:
            return
        roles = self._current_roles()
        if name in roles:
            messagebox.showinfo("Уже в списке", f"«{name}» уже есть в списке.")
            return
        roles[name] = "Пополнять"
        self._render_stores(roles)
        self.var_new_store.set("")

    def _save_stores(self):
        roles = self._current_roles()
        targets = [n for n, r in roles.items() if r == "Пополнять"]
        counted = [n for n, r in roles.items() if r == "Только учитывать"]
        gp = [n for n, r in roles.items() if r == "Склад готовой продукции"]
        if not targets:
            messagebox.showwarning(
                "Некому пополнять",
                "Отметьте хотя бы одну точку как «Пополнять»,\n"
                "иначе планировать производство будет не для кого.")
            return
        if len(gp) > 1:
            messagebox.showwarning(
                "Слишком много складов ГП",
                "Склад готовой продукции должен быть один.")
            return
        values = {"TARGET_STORES_CSV": ";".join(targets),
                  "STOCK_ONLY_STORES_CSV": ";".join(counted)}
        if gp:
            values["FINISHED_GOODS_WAREHOUSE"] = gp[0]
        try:
            from config import update_env
            path = update_env(values)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Не удалось сохранить",
                                 f"Настройки не записались:\n{exc}")
            return
        self.var_status.set(f"Настройки складов сохранены: {path.name}")
        messagebox.showinfo(
            "Сохранено",
            "Настройки складов сохранены.\n\n"
            f"Пополняем: {', '.join(targets)}\n"
            f"Только учитываем: {', '.join(counted) or '—'}")

    def _file_row(self, parent, r, label, var, browse_cmd):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w",
                                           padx=(0, 8), pady=5)
        ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", pady=5)
        ttk.Button(parent, text="Обзор…", command=browse_cmd).grid(
            row=r, column=2, padx=(8, 0), pady=5)

    def _combo_row(self, parent, r, label, var):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w",
                                           padx=(0, 8), pady=5)
        combo = ttk.Combobox(parent, textvariable=var, state="normal")
        combo.grid(row=r, column=1, sticky="ew", pady=5)
        return combo

    def _refresh_sheet_choices(self, combo, path, var, default):
        names = _list_sheet_names(path)
        combo["values"] = names
        if names and var.get().strip() not in names:
            var.set(names[0])
        elif not names and not var.get().strip():
            var.set(default)

    # ----------------------------------------------------------------- browse
    @staticmethod
    def _initial_dir():
        for cand in (Path.home() / "Downloads", Path.home()):
            if cand.exists():
                return str(cand)
        return "."

    def _browse_rating(self):
        path = filedialog.askopenfilename(
            title="Выберите файл рейтинга (.xlsx)", initialdir=self._initial_dir(),
            filetypes=[("Excel", "*.xlsx *.xls"), ("Все файлы", "*.*")])
        if path:
            self.var_rating.set(path)
            self._refresh_sheet_choices(
                self.combo_rating_sheet, path, self.var_rating_sheet, DEFAULT_RATING_SHEET)

    def _browse_stock(self):
        path = filedialog.askopenfilename(
            title="Выберите файл остатков (.xls/.xlsx)", initialdir=self._initial_dir(),
            filetypes=[("Excel", "*.xls *.xlsx"), ("Все файлы", "*.*")])
        if path:
            self.var_stock.set(path)
            self._refresh_sheet_choices(
                self.combo_stock_sheet, path, self.var_stock_sheet, DEFAULT_STOCK_SHEET)

    # -------------------------------------------------------------------- run
    def _on_run(self):
        if self._running:
            return
        rating = self.var_rating.get().strip()
        stock = self.var_stock.get().strip()
        rsheet = self.var_rating_sheet.get().strip()
        ssheet = self.var_stock_sheet.get().strip()

        problems = []
        if not rating or not Path(rating).is_file():
            problems.append("• Не выбран существующий файл рейтинга.")
        if not stock or not Path(stock).is_file():
            problems.append("• Не выбран существующий файл остатков.")
        if not rsheet:
            problems.append("• Не указан лист рейтинга.")
        if not ssheet:
            problems.append("• Не указан лист остатков.")
        if problems:
            messagebox.showwarning("Проверьте данные", "\n".join(problems))
            return

        safe = self.var_safe.get()
        if not safe and not messagebox.askyesno(
                "Подтверждение записи",
                "Безопасный режим ВЫКЛЮЧЕН.\n\n"
                "Планы будут ЗАПИСАНЫ в Google Таблицу клиента\n"
                "(План кроя, План пошива, Перемещения, Бестселлеры).\n\n"
                "Продолжить?", icon="warning"):
            return

        argv = ["--source", "1c",
                "--rating", rating, "--rating-sheet", rsheet,
                "--stock", stock, "--stock-sheet", ssheet,
                "--mode", self.var_mode.get()]
        if safe:
            argv.append("--dry-run")

        self._set_running(True, safe)
        self._clear_log()
        threading.Thread(target=self._worker, args=(argv, safe), daemon=True).start()

    def _worker(self, argv, safe):
        """Runs in a background thread — never touches Tk widgets directly."""
        rc = 1
        try:
            import main  # lazy import keeps GUI startup instant
            rc = main.run(argv)
        except SystemExit as exc:                      # e.g. argparse error
            rc = int(exc.code) if isinstance(exc.code, int) else 1
        except Exception:                              # noqa: BLE001
            import traceback
            print("\n[GUI] ВНУТРЕННЯЯ ОШИБКА:\n" + traceback.format_exc())
            rc = 1
        finally:
            self.log_q.put(("__DONE__", rc, safe))

    # ------------------------------------------------------------- log/queue
    def _poll_log(self):
        try:
            while True:
                item = self.log_q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__DONE__":
                    self._on_done(item[1], item[2])
                else:
                    self._append(item)
        except queue.Empty:
            pass
        self.root.after(self.POLL_MS, self._poll_log)

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_running(self, running, safe):
        self._running = running
        self.btn_run.configure(state="disabled" if running else "normal")
        if running:
            self.progress.start(12)
            self.var_status.set("Идёт расчёт… (тест, без записи)" if safe
                                else "Идёт расчёт и запись в Google Таблицу…")
        else:
            self.progress.stop()

    def _on_done(self, rc, safe):
        self._set_running(False, safe)
        if rc == 0:
            self.var_status.set("Готово.")
            if safe:
                messagebox.showinfo(
                    "Тест завершён",
                    "Безопасный режим: расчёт выполнен, в таблицу ничего не "
                    "записано.\nПроверьте результаты в журнале ниже.")
            else:
                messagebox.showinfo(
                    "Готово",
                    "Планы успешно обновлены в Google Таблице:\n"
                    "• План кроя\n• План пошива\n• Перемещения\n"
                    "• Бестселлеры к распределению")
        else:
            self.var_status.set(f"Ошибка (код {rc}). Подробности в журнале.")
            title, msg = EXIT_MESSAGES.get(rc, ("Ошибка", "Произошла ошибка. См. журнал."))
            messagebox.showerror(title, msg)


def _enable_dpi_awareness():
    """Crisp text on high-DPI Windows displays (no-op elsewhere)."""
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


def _run_selftest() -> int:
    """Import every bundled dependency and write a JSON report next to the app.

    A ``--windowed`` .exe has no console, so a freshly built bundle is verified
    by running it with ``--selftest``: it tries to import all third-party and
    project modules and writes ``selftest_result.json`` (exit 0 = all OK).
    """
    import json

    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent

    mods = [
        "pandas", "openpyxl", "xlrd", "gspread",
        "google.oauth2.service_account", "google.auth.transport.requests",
        "pydantic", "pydantic_settings",
        "config", "models", "normalizer", "data_loader", "analyzer",
        "google_sheets", "pipeline_1c", "main",
    ]
    checks, ok = {}, True
    for m in mods:
        try:
            __import__(m)
            checks[m] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks[m] = f"FAIL: {type(exc).__name__}: {exc}"
            ok = False

    report = {
        "ok": ok,
        "frozen": bool(getattr(sys, "frozen", False)),
        "base_dir": str(base),
        "python": sys.version.split()[0],
        "checks": checks,
    }
    (base / "selftest_result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        raise SystemExit(_run_selftest())
    _enable_dpi_awareness()
    root = tk.Tk()
    PlannerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
