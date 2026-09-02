from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"
VENV_PYW = VENV / "Scripts" / "pythonw.exe"
LOG = ROOT / "setup.log"
RELEASE_DATA_SUMMARY = ROOT / "data" / "release_data_summary.json"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000 if os.name == "nt" else 0)


def _system_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        peer = exe.with_name("python.exe")
        if peer.exists():
            return str(peer)
    return str(exe)


def _launch_main() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    subprocess.Popen(
        [str(VENV_PYW), str(ROOT / "field_crafter_entry.py")],
        cwd=str(ROOT),
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )


def _runtime_ready() -> bool:
    if not VENV_PYW.exists():
        return False
    try:
        result = subprocess.run(
            [str(VENV_PY), "-c", "import rapidocr, PIL, tkinterdnd2, requests, bs4"],
            cwd=str(ROOT), capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _release_data_ready() -> bool:
    try:
        data = json.loads(RELEASE_DATA_SUMMARY.read_text(encoding="utf-8"))
        return bool(data.get("release_data_ready"))
    except Exception:
        return False


if _runtime_ready() and _release_data_ready():
    _launch_main()
    raise SystemExit(0)

# First-launch setup uses a small GUI rather than exposing PowerShell/cmd.
import tkinter as tk
from tkinter import messagebox, ttk

root = tk.Tk()
root.title("Field Crafter 1.16-dev - First launch")
root.resizable(False, False)
root.geometry("520x170")
try:
    icon_ico = ROOT / "assets" / "field_crafter.ico"
    if icon_ico.exists():
        try:
            root.iconbitmap(default=str(icon_ico))
        except Exception:
            pass
except Exception:
    pass
frame = ttk.Frame(root, padding=18)
frame.pack(fill="both", expand=True)
status = tk.StringVar(value="Preparing local components...")
ttk.Label(
    frame,
    text="First launch setup",
    font=("Segoe UI", 12, "bold"),
).pack(anchor="w")
ttk.Label(
    frame,
    text=("Field Crafter is preparing its private Python environment. The recipe database and game-memory recipe map "
          "are bundled with this release and do not require a Wiki connection on first launch."),
    wraplength=480, justify="left",
).pack(anchor="w", pady=(6, 10))
ttk.Label(frame, textvariable=status).pack(anchor="w")
bar = ttk.Progressbar(frame, mode="indeterminate")
bar.pack(fill="x", pady=(6, 0))
bar.start(10)


def setup_worker() -> None:
    try:
        commands = []
        if not VENV_PY.exists():
            commands.append([_system_python(), "-m", "venv", str(VENV)])
        commands.extend([
            [str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip"],
            [str(VENV_PY), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
            [str(VENV_PY), "-m", "pip", "install", "-r", str(ROOT / "requirements-ocr.txt")],
        ])
        with LOG.open("w", encoding="utf-8") as log:
            for idx, cmd in enumerate(commands, 1):
                root.after(0, lambda i=idx, n=len(commands): status.set(f"Setup step {i} of {n}..."))
                proc = subprocess.run(
                    cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                    text=True, creationflags=CREATE_NO_WINDOW,
                )
                if proc.returncode != 0:
                    raise RuntimeError(f"Setup command failed with exit code {proc.returncode}: {' '.join(cmd)}")
            if not _runtime_ready():
                raise RuntimeError("Setup completed, but one or more required runtime components could not be imported.")
            if not _release_data_ready():
                raise RuntimeError(
                    "This Field Crafter package does not contain validated release data. "
                    "Use a properly generated release package; first launch will not download crafting data from the Wiki."
                )
        root.after(0, setup_success)
    except Exception as exc:
        root.after(0, lambda e=exc: setup_failed(e))


def setup_success() -> None:
    bar.stop()
    status.set("Setup complete. Starting Field Crafter...")
    root.update_idletasks()
    try:
        _launch_main()
    except Exception as exc:
        setup_failed(exc)
        return
    root.after(300, root.destroy)


def setup_failed(exc: Exception) -> None:
    bar.stop()
    status.set("Setup failed.")
    messagebox.showerror(
        "Field Crafter",
        f"First-launch setup failed:\n\n{exc}\n\nA detailed log was written to:\n{LOG}",
        parent=root,
    )

threading.Thread(target=setup_worker, daemon=True).start()
root.mainloop()
