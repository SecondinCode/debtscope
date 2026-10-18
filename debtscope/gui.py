"""Desktop window for DebtScope (double-click DebtScope.bat to open it).

Type tickers, press Analyze, the Excel report opens by itself.
Settings (your SEC identity, output folder) are remembered in
~/.debtscope.json so you type them once.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from .edgar import EMAIL_RE, EdgarClient, EdgarError
from .excel import write_comparison, write_workbook
from .pipeline import build_profile_from_documents, fetch
from .viewer import write_viewer

CONFIG = Path.home() / ".debtscope.json"
DEFAULT_OUT = Path.home() / "Documents" / "DebtScope reports"
CACHE = Path.home() / ".debtscope_cache"


# ------------------------------------------------------------ plain logic
def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass  # not fatal: user just retypes next time


def parse_tickers(text: str) -> list[str]:
    return [t.upper() for t in re.split(r"[\s,;]+", text.strip()) if t]


def open_path(path: Path) -> None:
    """Open a file or folder with the default app (Excel, Explorer...)."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - user's own report
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def run_job(identity: str, tickers: list[str], form: str, out: Path, log,
            viewer: bool = True) -> tuple[list[Path], list[Path]]:
    """Fetch, extract and write reports. Returns (excel files, crawler pages)."""
    client = EdgarClient(identity, cache_dir=CACHE)
    profiles, files, pages = [], [], []
    for t in tickers:
        log(f"{t}: downloading {form} from SEC EDGAR...", "dim")
        try:
            filing, html, facts = fetch(client, t, form)
            p = build_profile_from_documents(filing, html, facts, t)
        except EdgarError as exc:
            log(f"{t}: {exc}", "err")
            continue
        m = p.metrics
        stem = f"DebtScope_{p.ticker}_{p.form}_{p.report_date}"
        path = write_workbook(p, out / f"{stem}.xlsx")
        profiles.append(p)
        files.append(path)
        if viewer:
            log(f"{t}: building the visual crawler...", "dim")
            pages.append(write_viewer(html, p, out / f"{stem}_crawler.html"))
        cov = m.get("coverage_ratio")
        log(f"{t}: {p.company} | period {p.report_date} | "
            f"{len(p.instruments)} instruments | coverage "
            f"{'n/a' if cov is None else f'{cov:.0%}'}", "ok")
        for w in p.warnings:
            log(f"   ! {w}", "warn")
    if len(profiles) > 1:
        files.append(write_comparison(profiles, out / "DebtScope_comparison.xlsx"))
        log("Comparison sheet created.", "ok")
    return files, pages


# --------------------------------------------------------------------- UI
def main() -> None:
    import tkinter as tk
    from tkinter import ttk

    NAVY, BG, FG = "#1F2A44", "#F5F7FA", "#1B1F27"
    cfg = load_config()
    msgs: queue.Queue = queue.Queue()

    root = tk.Tk()
    root.title("DebtScope")
    root.geometry("760x560")
    root.minsize(640, 480)
    root.configure(bg=BG)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
    style.configure("Hint.TLabel", foreground="#5B6B82", font=("Segoe UI", 9))
    style.configure("Go.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 8),
                    background=NAVY, foreground="white")
    style.map("Go.TButton", background=[("active", "#2E3F66"), ("disabled", "#9AA5B8")])
    style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6))

    header = tk.Frame(root, bg=NAVY, height=70)
    header.pack(fill="x")
    tk.Label(header, text="DebtScope", bg=NAVY, fg="white",
             font=("Segoe UI", 20, "bold")).pack(side="left", padx=20, pady=12)
    tk.Label(header, text="Debt structure & maturity wall from SEC filings", bg=NAVY,
             fg="#C9D1DC", font=("Segoe UI", 10)).pack(side="left", pady=(22, 12))

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=20, pady=14)
    body.columnconfigure(1, weight=1)

    ttk.Label(body, text="Tickers").grid(row=0, column=0, sticky="w", pady=4)
    tick_var = tk.StringVar(value=cfg.get("last_tickers", "AAPL"))
    tick_entry = ttk.Entry(body, textvariable=tick_var, font=("Segoe UI", 12))
    tick_entry.grid(row=0, column=1, sticky="ew", pady=4)
    form_var = tk.StringVar(value=cfg.get("form", "10-K"))
    ttk.Combobox(body, textvariable=form_var, values=["10-K", "10-Q", "20-F"], width=7,
                 state="readonly").grid(row=0, column=2, padx=(8, 0))
    ttk.Label(body, text="One or more, separated by spaces (e.g. AAPL MSFT ORCL). US-listed issuers only.",
              style="Hint.TLabel").grid(row=1, column=1, columnspan=2, sticky="w")

    ttk.Label(body, text="Your name + email").grid(row=2, column=0, sticky="w", pady=(12, 4))
    id_var = tk.StringVar(value=cfg.get("identity", ""))
    ttk.Entry(body, textvariable=id_var).grid(row=2, column=1, columnspan=2, sticky="ew",
                                              pady=(12, 4))
    ttk.Label(body, text="Required by the SEC to download filings. Saved, you type it once.",
              style="Hint.TLabel").grid(row=3, column=1, columnspan=2, sticky="w")

    out_dir = Path(cfg.get("out", str(DEFAULT_OUT)))

    buttons = tk.Frame(body, bg=BG)
    buttons.grid(row=4, column=0, columnspan=3, sticky="w", pady=14)
    go = ttk.Button(buttons, text="Analyze", style="Go.TButton")
    go.pack(side="left")
    view_var = tk.BooleanVar(value=cfg.get("viewer", True))
    ttk.Checkbutton(buttons, text="Open visual crawler", variable=view_var).pack(side="left", padx=(14, 0))
    ttk.Button(buttons, text="Open reports folder",
               command=lambda: (out_dir.mkdir(parents=True, exist_ok=True), open_path(out_dir))
               ).pack(side="left", padx=10)

    logbox = tk.Text(body, height=14, wrap="word", font=("Consolas", 10), bg="white",
                     fg=FG, relief="flat", padx=10, pady=8)
    logbox.grid(row=5, column=0, columnspan=3, sticky="nsew")
    body.rowconfigure(5, weight=1)
    for tag, colour in {"ok": "#1E7B34", "err": "#B3261E", "warn": "#9C5A00",
                        "dim": "#5B6B82"}.items():
        logbox.tag_configure(tag, foreground=colour)
    logbox.configure(state="disabled")

    def write(text: str, tag: str = "") -> None:
        logbox.configure(state="normal")
        logbox.insert("end", text + "\n", tag)
        logbox.see("end")
        logbox.configure(state="disabled")

    def pump() -> None:
        """Move messages from the worker thread into the UI (Tk is single-threaded)."""
        try:
            while True:
                kind, payload = msgs.get_nowait()
                if kind == "log":
                    write(*payload)
                elif kind == "done":
                    go.state(["!disabled"])
                    files, pages = payload
                    if files:
                        write(f"Done. Reports saved in: {out_dir}", "ok")
                        open_path(files[-1])  # comparison if several, else the report
                    for page in pages:
                        open_path(page)       # opens in the default browser
        except queue.Empty:
            pass
        root.after(120, pump)

    def start(_event=None) -> None:
        identity = id_var.get().strip()
        tickers = parse_tickers(tick_var.get())
        if not EMAIL_RE.search(identity):
            write("Enter your name and a real email in the second field (SEC requirement).", "err")
            return
        if not tickers:
            write("Enter at least one ticker.", "err")
            return
        save_config({**cfg, "identity": identity, "last_tickers": " ".join(tickers),
                     "form": form_var.get(), "out": str(out_dir), "viewer": view_var.get()})
        go.state(["disabled"])
        write(f"--- {', '.join(tickers)} ({form_var.get()}) ---", "dim")

        def worker() -> None:
            result: tuple[list[Path], list[Path]] = ([], [])
            try:
                result = run_job(identity, tickers, form_var.get(), out_dir,
                                 lambda t, tag="": msgs.put(("log", (t, tag))), view_var.get())
            except Exception:  # show anything unexpected instead of dying silently
                msgs.put(("log", ("Unexpected error - send a screenshot of this:\n"
                                  + traceback.format_exc(), "err")))
            msgs.put(("done", result))

        threading.Thread(target=worker, daemon=True).start()

    go.configure(command=start)
    root.bind("<Return>", start)
    write("Type a ticker and press Analyze (or Enter). The Excel report opens when ready.", "dim")
    tick_entry.focus_set()
    tick_entry.select_range(0, "end")
    root.after(120, pump)
    root.mainloop()


if __name__ == "__main__":
    main()
