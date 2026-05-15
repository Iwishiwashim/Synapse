"""
Synapse installer wizard — tkinter, no external dependencies.
Build to exe/app with:  python installer/build.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ── Paths ────────────────────────────────────────────────────────────────────

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent
else:
    ROOT = Path(__file__).resolve().parent.parent

# ── Palette ──────────────────────────────────────────────────────────────────

BG       = "#0d0d0d"
SURFACE  = "#141414"
BORDER   = "#2a2a2a"
ACCENT   = "#7c6aff"
SUCCESS  = "#22c55e"
ERROR    = "#ef4444"
WARNING  = "#eab308"
FG       = "#e8e8e8"
MUTED    = "#666666"
FG2      = "#aaaaaa"

FONT        = ("Segoe UI", 10)
FONT_SM     = ("Segoe UI", 9)
FONT_LG     = ("Segoe UI", 14, "bold")
FONT_TITLE  = ("Segoe UI", 11, "bold")
FONT_MONO   = ("Consolas", 9)

W, H = 580, 520


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sys_python() -> tuple[bool, str]:
    """Return (found, version_string) for system Python 3.10+."""
    for cmd in ("python", "python3"):
        try:
            out = subprocess.check_output(
                [cmd, "--version"], stderr=subprocess.STDOUT, text=True, timeout=5
            ).strip()
            parts = out.split()
            if len(parts) >= 2:
                ver = parts[1]
                major, minor = int(ver.split(".")[0]), int(ver.split(".")[1])
                if major == 3 and minor >= 10:
                    return True, ver
        except Exception:
            pass
    return False, ""


def _python_cmd() -> str:
    for cmd in ("python", "python3"):
        try:
            subprocess.check_output([cmd, "--version"], stderr=subprocess.STDOUT, timeout=5)
            return cmd
        except Exception:
            pass
    return "python"


def _test_gemini_key(key: str) -> tuple[bool, str]:
    """Return (ok, message). Uses urllib only — no external deps."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            count = len(data.get("models", []))
            return True, f"Valid — {count} models available"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        try:
            msg = json.loads(body).get("error", {}).get("message", "Invalid key")
        except Exception:
            msg = "Invalid key"
        return False, msg
    except Exception as e:
        return False, f"Network error: {e}"


def _write_env(env: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in env.items() if v]
    (ROOT / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_config(vault: str, mode: str) -> None:
    import yaml
    cfg_path = ROOT / "config.yaml"
    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    existing["vault_path"] = vault
    existing["write_mode"] = mode
    lines = [
        f"vault_path: {vault}",
        "",
        f"raw_archive_path: {existing.get('raw_archive_path', '')}",
        "",
        f"encryption: {str(existing.get('encryption', False)).lower()}",
        f"cloud_search: {str(existing.get('cloud_search', False)).lower()}",
        f"git_enabled: {str(existing.get('git_enabled', True)).lower()}",
        f"weekly_report_day: {existing.get('weekly_report_day', 'monday')}",
        f"life_mode: {str(existing.get('life_mode', False)).lower()}",
        f"pending_auto_expire_days: {existing.get('pending_auto_expire_days', 7)}",
        "",
        f'gemini_api_key: ""',
        "",
        f"write_mode: {mode}",
    ]
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_mcp(api_key: str) -> tuple[str, str]:
    """Write Claude Desktop + Claude Code configs. Returns (desktop_path, code_path)."""
    venv_python = str(
        ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    )
    if sys.platform == "win32":
        venv_python += ".exe"
    launcher = str(ROOT / "run_server.py")

    # Claude Desktop
    desktop_path = _desktop_config_path()
    desktop_result = ""
    if desktop_path:
        existing: dict = {}
        if desktop_path.exists():
            try:
                existing = json.loads(desktop_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.setdefault("mcpServers", {})["synapse"] = {
            "command": venv_python,
            "args": [launcher],
            "env": {"GEMINI_API_KEY": api_key},
        }
        desktop_path.parent.mkdir(parents=True, exist_ok=True)
        desktop_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        desktop_result = str(desktop_path)

    # Claude Code
    code_path = Path.home() / ".claude.json"
    existing2: dict = {}
    if code_path.exists():
        try:
            existing2 = json.loads(code_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing2.setdefault("mcpServers", {})["synapse"] = {
        "type": "stdio",
        "command": venv_python,
        "args": [launcher],
        "env": {"GEMINI_API_KEY": api_key},
    }
    code_path.write_text(json.dumps(existing2, indent=2), encoding="utf-8")

    return desktop_result, str(code_path)


def _desktop_config_path() -> Path | None:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


# ── Styled widgets ────────────────────────────────────────────────────────────

def _frame(parent, **kw) -> tk.Frame:
    return tk.Frame(parent, bg=BG, **kw)


def _label(parent, text, font=FONT, fg=FG, **kw) -> tk.Label:
    return tk.Label(parent, text=text, font=font, fg=fg, bg=BG, **kw)


def _entry(parent, show=None, width=40) -> tk.Entry:
    e = tk.Entry(
        parent, show=show, width=width,
        bg=SURFACE, fg=FG, insertbackground=FG,
        relief="flat", highlightthickness=1,
        highlightbackground=BORDER, highlightcolor=ACCENT,
        font=FONT_MONO, bd=0,
    )
    return e


def _button(parent, text, command, primary=True, **kw) -> tk.Button:
    bg = ACCENT if primary else SURFACE
    fg = "#fff" if primary else FG2
    return tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
        relief="flat", bd=0, padx=20, pady=8,
        font=("Segoe UI", 10, "bold") if primary else FONT,
        cursor="hand2", **kw
    )


def _divider(parent) -> tk.Frame:
    return tk.Frame(parent, bg=BORDER, height=1)


# ── Progress indicator ────────────────────────────────────────────────────────

class StepBar(tk.Canvas):
    STEPS = ["Welcome", "API Keys", "Config", "Install", "Done"]
    R = 12
    SPACING = 100

    def __init__(self, parent):
        total_w = self.SPACING * (len(self.STEPS) - 1) + self.R * 2 + 60
        super().__init__(parent, bg=BG, height=50, width=total_w,
                         highlightthickness=0, bd=0)
        self._current = 1
        self._draw()

    def set(self, step: int):
        self._current = step
        self._draw()

    def _cx(self, i):
        return 30 + i * self.SPACING

    def _draw(self):
        self.delete("all")
        n = len(self.STEPS)
        for i in range(n - 1):
            x1 = self._cx(i) + self.R
            x2 = self._cx(i + 1) - self.R
            color = SUCCESS if i + 1 < self._current else BORDER
            self.create_line(x1, 25, x2, 25, fill=color, width=2)

        for i, label in enumerate(self.STEPS):
            cx = self._cx(i)
            step_num = i + 1
            if step_num < self._current:
                fill, outline, text_col, text = SUCCESS, SUCCESS, "#fff", "✓"
            elif step_num == self._current:
                fill, outline, text_col, text = ACCENT, ACCENT, "#fff", str(step_num)
            else:
                fill, outline, text_col, text = BG, BORDER, MUTED, str(step_num)

            self.create_oval(cx - self.R, 25 - self.R, cx + self.R, 25 + self.R,
                             fill=fill, outline=outline, width=2)
            self.create_text(cx, 25, text=text, fill=text_col,
                             font=("Segoe UI", 8, "bold"))
            self.create_text(cx, 25 + self.R + 10, text=label,
                             fill=text_col if step_num <= self._current else MUTED,
                             font=FONT_SM)


# ── Main wizard ───────────────────────────────────────────────────────────────

class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Synapse Installer")
        self.geometry(f"{W}x{H}")
        self.resizable(False, False)
        self.configure(bg=BG)
        self._center()

        # State
        self.gemini_key = tk.StringVar()
        self.groq_key = tk.StringVar()
        self.or_key = tk.StringVar()
        self.vault_path = tk.StringVar(value="./vault")
        self.write_mode = tk.StringVar(value="review")
        self.key_valid = False

        # Header
        hdr = _frame(self)
        hdr.pack(fill="x", padx=30, pady=(20, 0))
        _label(hdr, "Synapse", font=("Segoe UI", 16, "bold"), fg=ACCENT).pack(side="left")
        _label(hdr, " Installer", font=("Segoe UI", 16, "bold")).pack(side="left")

        _label(self, "A memory agent for Claude — reduce token usage by up to 99.9%",
               font=FONT_SM, fg=MUTED).pack(anchor="w", padx=30, pady=(2, 10))

        _divider(self).pack(fill="x", padx=0)

        # Step bar
        self.bar = StepBar(self)
        self.bar.pack(pady=(14, 4))

        _divider(self).pack(fill="x")

        # Content area
        self.content = _frame(self)
        self.content.pack(fill="both", expand=True, padx=30, pady=20)

        self._build_steps()
        self._show(1)

    def _center(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - W) // 2
        y = (self.winfo_screenheight() - H) // 2
        self.geometry(f"{W}x{H}+{x}+{y}")

    # ── Step builder ─────────────────────────────────────────────────────────

    def _build_steps(self):
        self.frames: dict[int, tk.Frame] = {}
        for n in range(1, 6):
            f = _frame(self.content)
            self.frames[n] = f
            getattr(self, f"_step{n}")(f)

    def _show(self, n: int):
        for f in self.frames.values():
            f.pack_forget()
        self.frames[n].pack(fill="both", expand=True)
        self.bar.set(n)
        self._current = n

    # ── Step 1: Welcome ───────────────────────────────────────────────────────

    def _step1(self, f):
        _label(f, "Welcome", font=FONT_LG).pack(anchor="w")
        _label(f, "This wizard installs Synapse and connects it to Claude.\nNo terminal needed — just follow the steps.",
               font=FONT, fg=FG2, justify="left").pack(anchor="w", pady=(4, 16))

        # System checks
        checks_frame = tk.Frame(f, bg=SURFACE, relief="flat", bd=0)
        checks_frame.pack(fill="x", pady=(0, 16))

        py_ok, py_ver = _sys_python()
        self._check_row(checks_frame, "Python 3.10+",
                        py_ver if py_ok else "Not found — install from python.org",
                        py_ok)
        self._check_row(checks_frame, "Synapse folder",
                        str(ROOT) if (ROOT / "run_server.py").exists() else "Not found",
                        (ROOT / "run_server.py").exists())

        if not py_ok:
            msg = tk.Frame(f, bg="#1a1000")
            msg.pack(fill="x", pady=(0, 10))
            _label(msg,
                   "⚠  Python not found. Install Python 3.10+ from python.org\n"
                   "   (check 'Add Python to PATH' during install), then re-run this wizard.",
                   font=FONT_SM, fg=WARNING, bg="#1a1000", justify="left").pack(padx=12, pady=8)

        btn = _button(f, "Get started →", lambda: self._show(2))
        btn.pack(side="bottom", anchor="e")
        if not py_ok:
            btn.configure(state="disabled", bg=BORDER, fg=MUTED)

    def _check_row(self, parent, label, sub, ok):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", padx=12, pady=5)
        dot = tk.Label(row, text="✓" if ok else "✗",
                       bg=SUCCESS if ok else ERROR,
                       fg="#fff", font=("Segoe UI", 8, "bold"),
                       width=2, relief="flat")
        dot.pack(side="left", padx=(0, 10))
        tk.Label(row, text=label, bg=SURFACE, fg=FG, font=FONT).pack(side="left")
        tk.Label(row, text=sub, bg=SURFACE, fg=MUTED, font=FONT_SM).pack(side="left", padx=(8, 0))

    # ── Step 2: API Keys ──────────────────────────────────────────────────────

    def _step2(self, f):
        _label(f, "API Keys", font=FONT_LG).pack(anchor="w")
        _label(f, "Gemini is required. Groq and OpenRouter are optional\n(only needed for the ChatGPT export filtering pipeline).",
               font=FONT, fg=FG2, justify="left").pack(anchor="w", pady=(4, 14))

        # Gemini
        _label(f, "Gemini API Key  *required", font=FONT_SM, fg=FG2).pack(anchor="w")
        gem_row = _frame(f)
        gem_row.pack(fill="x", pady=(2, 2))
        gem_entry = _entry(gem_row, show="•", width=38)
        gem_entry.pack(side="left", ipady=5)
        gem_entry.configure(textvariable=self.gemini_key)
        tk.Button(gem_row, text="👁", bg=SURFACE, fg=FG2, relief="flat",
                  command=lambda: gem_entry.configure(show="" if gem_entry.cget("show") == "•" else "•"),
                  cursor="hand2").pack(side="left", padx=(4, 0))

        self.key_status = _label(f, "  Get a free key at aistudio.google.com/apikey",
                                  font=FONT_SM, fg=MUTED)
        self.key_status.pack(anchor="w", pady=(0, 8))

        test_btn = _button(f, "Test key", self._test_key, primary=False)
        test_btn.pack(anchor="w", pady=(0, 12))

        _divider(f).pack(fill="x", pady=(0, 10))

        # Optional keys
        _label(f, "Groq API Key  (optional)", font=FONT_SM, fg=FG2).pack(anchor="w")
        _entry(f, width=50).pack(anchor="w", pady=(2, 2), ipady=4,
                                  **{"textvariable": self.groq_key} if False else {})
        groq_e = _entry(f, width=50)
        groq_e.configure(textvariable=self.groq_key)
        # re-layout
        groq_e.pack_forget()
        groq_e = _entry(f, width=50)
        groq_e.pack(anchor="w", pady=(2, 2), ipady=4)
        groq_e.configure(textvariable=self.groq_key)
        _label(f, "  Only for Diagnostics/Triage.py  —  console.groq.com",
               font=FONT_SM, fg=MUTED).pack(anchor="w", pady=(0, 8))

        _label(f, "OpenRouter API Key  (optional)", font=FONT_SM, fg=FG2).pack(anchor="w")
        or_e = _entry(f, width=50)
        or_e.pack(anchor="w", pady=(2, 2), ipady=4)
        or_e.configure(textvariable=self.or_key)
        _label(f, "  Only for Diagnostics/Triage.py  —  openrouter.ai/keys",
               font=FONT_SM, fg=MUTED).pack(anchor="w")

        btn_row = _frame(f)
        btn_row.pack(side="bottom", fill="x")
        _button(btn_row, "← Back", lambda: self._show(1), primary=False).pack(side="left")
        self.btn2_next = _button(btn_row, "Continue →", lambda: self._show(3))
        self.btn2_next.pack(side="right")
        self.btn2_next.configure(state="disabled", bg=BORDER, fg=MUTED)

    def _test_key(self):
        key = self.gemini_key.get().strip()
        if not key:
            self.key_status.configure(text="  ✗ Enter your key first", fg=ERROR)
            return
        self.key_status.configure(text="  Testing...", fg=MUTED)
        self.update()

        def _run():
            ok, msg = _test_gemini_key(key)
            self.after(0, lambda: self._on_key_result(ok, msg))

        threading.Thread(target=_run, daemon=True).start()

    def _on_key_result(self, ok: bool, msg: str):
        if ok:
            self.key_status.configure(text=f"  ✓ {msg}", fg=SUCCESS)
            self.key_valid = True
            self.btn2_next.configure(state="normal", bg=ACCENT, fg="#fff")
        else:
            self.key_status.configure(text=f"  ✗ {msg}", fg=ERROR)
            self.key_valid = False

    # ── Step 3: Config ────────────────────────────────────────────────────────

    def _step3(self, f):
        _label(f, "Configuration", font=FONT_LG).pack(anchor="w")
        _label(f, "Set your vault location and how Claude handles memory writes.",
               font=FONT, fg=FG2).pack(anchor="w", pady=(4, 16))

        # Vault path
        _label(f, "Vault Path", font=FONT_SM, fg=FG2).pack(anchor="w")
        vp_row = _frame(f)
        vp_row.pack(fill="x", pady=(2, 4))
        vp_entry = _entry(vp_row, width=36)
        vp_entry.pack(side="left", ipady=5)
        vp_entry.configure(textvariable=self.vault_path)
        _button(vp_row, "Browse", self._browse_vault, primary=False).pack(side="left", padx=(6, 0))
        _label(f, "  Folder where your memory files are stored.",
               font=FONT_SM, fg=MUTED).pack(anchor="w", pady=(0, 14))

        # Write mode
        _label(f, "Write Mode", font=FONT_SM, fg=FG2).pack(anchor="w", pady=(0, 6))

        mode_frame = _frame(f)
        mode_frame.pack(fill="x", pady=(0, 4))

        def _mk_mode(parent, val, title, desc):
            row = tk.Frame(parent, bg=SURFACE, cursor="hand2")
            row.pack(fill="x", pady=3, ipady=8, ipadx=10)
            rb = tk.Radiobutton(row, variable=self.write_mode, value=val,
                                bg=SURFACE, activebackground=SURFACE,
                                selectcolor=ACCENT, fg=ACCENT,
                                command=lambda: None)
            rb.pack(side="left", padx=(8, 4))
            tk.Label(row, text=title, bg=SURFACE, fg=FG,
                     font=FONT_TITLE).pack(side="left")
            tk.Label(row, text=f"  —  {desc}", bg=SURFACE, fg=MUTED,
                     font=FONT_SM).pack(side="left")
            row.bind("<Button-1>", lambda e: self.write_mode.set(val))

        _mk_mode(mode_frame, "review", "Review",
                 "Claude proposes a diff — you approve before anything is written")
        _mk_mode(mode_frame, "auto",   "Auto",
                 "Claude writes directly, no confirmation needed (faster)")

        btn_row = _frame(f)
        btn_row.pack(side="bottom", fill="x")
        _button(btn_row, "← Back", lambda: self._show(2), primary=False).pack(side="left")
        _button(btn_row, "Install →", lambda: self._show(4)).pack(side="right")

    def _browse_vault(self):
        d = filedialog.askdirectory(title="Choose vault folder", initialdir=str(ROOT))
        if d:
            self.vault_path.set(d)

    # ── Step 4: Install ───────────────────────────────────────────────────────

    def _step4(self, f):
        _label(f, "Installing", font=FONT_LG).pack(anchor="w")
        _label(f, "Creating virtual environment and installing dependencies.",
               font=FONT, fg=FG2).pack(anchor="w", pady=(4, 10))

        # Log area
        log_frame = tk.Frame(f, bg=SURFACE, relief="flat")
        log_frame.pack(fill="both", expand=True, pady=(0, 10))

        self.log = tk.Text(
            log_frame, bg=SURFACE, fg=FG2, font=FONT_MONO,
            relief="flat", bd=0, state="disabled",
            wrap="word", height=12,
        )
        sb = tk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True, padx=8, pady=6)

        self.progress = ttk.Progressbar(f, mode="indeterminate", length=500)
        self.progress.pack(fill="x", pady=(0, 8))

        self._style_progress()

        btn_row = _frame(f)
        btn_row.pack(fill="x")
        _button(btn_row, "← Back", lambda: self._show(3), primary=False).pack(side="left")
        self.btn4_next = _button(btn_row, "Next →", lambda: self._show(5))
        self.btn4_next.pack(side="right")
        self.btn4_next.configure(state="disabled", bg=BORDER, fg=MUTED)

        # Auto-start install when step shown
        f.bind("<Visibility>", lambda e: threading.Thread(
            target=self._run_install, daemon=True).start() if not self._install_started() else None)
        self._installed = False

    def _style_progress(self):
        s = ttk.Style()
        s.theme_use("default")
        s.configure("TProgressbar", troughcolor=BORDER, background=ACCENT, thickness=4)

    def _install_started(self):
        return getattr(self, "_installed", False)

    def _log(self, text: str, color=None):
        self.log.configure(state="normal")
        tag = f"c{color}" if color else None
        if color:
            self.log.tag_configure(tag, foreground=color)
        self.log.insert("end", text + "\n", tag or "")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _run_install(self):
        if self._install_started():
            return
        self._installed = True
        self.after(0, self.progress.start)

        def log(msg, color=None):
            self.after(0, lambda: self._log(msg, color))

        def fail(msg):
            log(f"\n✗ {msg}", ERROR)
            self.after(0, self.progress.stop)

        try:
            py = _python_cmd()
            venv = ROOT / ".venv"

            # 1. Create venv
            log("→ Creating virtual environment...")
            r = subprocess.run([py, "-m", "venv", str(venv)],
                               capture_output=True, text=True, cwd=str(ROOT))
            if r.returncode != 0:
                fail(r.stderr or "venv creation failed")
                return
            log("  ✓ Virtual environment created", SUCCESS)

            # 2. pip install
            pip = str(venv / ("Scripts" if sys.platform == "win32" else "bin") / "pip")
            log("→ Installing dependencies (this may take a minute)...")
            proc = subprocess.Popen(
                [pip, "install", "--prefer-binary", "-r",
                 str(ROOT / "requirements.txt"), "pytest"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(ROOT),
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line.startswith("Successfully installed"):
                    log(f"  {line}", SUCCESS)
                elif "error" in line.lower():
                    log(f"  {line}", ERROR)
                elif line:
                    log(f"  {line}", MUTED)
            proc.wait()
            if proc.returncode != 0:
                fail("pip install failed — check the log above")
                return
            log("  ✓ Dependencies installed", SUCCESS)

            # 3. Write .env
            log("→ Writing .env...")
            env: dict[str, str] = {}
            if self.gemini_key.get().strip():
                env["GEMINI_API_KEY"] = self.gemini_key.get().strip()
            if self.groq_key.get().strip():
                env["GROQ_API_KEY"] = self.groq_key.get().strip()
            if self.or_key.get().strip():
                env["OPENROUTER_API_KEY"] = self.or_key.get().strip()
            _write_env(env)
            log("  ✓ .env written", SUCCESS)

            # 4. Write config.yaml
            log("→ Writing config.yaml...")
            _write_config(self.vault_path.get().strip() or "./vault",
                          self.write_mode.get())
            log("  ✓ config.yaml written", SUCCESS)

            # 5. Run tests
            log("→ Running tests...")
            venv_py = str(venv / ("Scripts" if sys.platform == "win32" else "bin") / "python")
            if sys.platform == "win32":
                venv_py += ".exe"
            r2 = subprocess.run(
                [venv_py, "-m", "pytest", "Diagnostics/test_core.py", "-q"],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            if r2.returncode == 0:
                lines = [l for l in r2.stdout.splitlines() if l.strip()]
                log(f"  ✓ {lines[-1] if lines else 'All tests passed'}", SUCCESS)
            else:
                log("  ⚠  Some tests failed — install may still work", WARNING)
                log(r2.stdout[-500:], MUTED)

            log("\n✓ Installation complete!", SUCCESS)
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.btn4_next.configure(
                state="normal", bg=ACCENT, fg="#fff"))

        except Exception as e:
            fail(str(e))

    # ── Step 5: Done ──────────────────────────────────────────────────────────

    def _step5(self, f):
        _label(f, "Connect to Claude", font=FONT_LG).pack(anchor="w")
        _label(f, "Writing MCP config for Claude Desktop and Claude Code...",
               font=FONT, fg=FG2).pack(anchor="w", pady=(4, 10))

        self.done_text = tk.Text(
            f, bg=SURFACE, fg=FG2, font=FONT_MONO,
            relief="flat", bd=0, state="disabled",
            wrap="word", height=8,
        )
        self.done_text.pack(fill="both", expand=True, pady=(0, 10))

        self.done_status = _label(f, "", font=FONT, fg=FG2, justify="left")
        self.done_status.pack(anchor="w", pady=(0, 10))

        btn_row = _frame(f)
        btn_row.pack(fill="x", side="bottom")
        _button(btn_row, "← Back", lambda: self._show(4), primary=False).pack(side="left")
        _button(btn_row, "Finish", self.destroy).pack(side="right")

        f.bind("<Visibility>", lambda e: self._write_mcp_config())
        self._mcp_done = False

    def _write_mcp_config(self):
        if getattr(self, "_mcp_done", False):
            return
        self._mcp_done = True

        def _log(msg, color=None):
            self.done_text.configure(state="normal")
            tag = f"dc{color}" if color else None
            if color:
                self.done_text.tag_configure(tag, foreground=color)
            self.done_text.insert("end", msg + "\n", tag or "")
            self.done_text.see("end")
            self.done_text.configure(state="disabled")

        try:
            key = self.gemini_key.get().strip()
            desktop, code = _write_mcp(key)
            if desktop:
                _log(f"✓ Claude Desktop config written:", SUCCESS)
                _log(f"  {desktop}", MUTED)
            else:
                _log("⚠  Claude Desktop config not found — write manually", WARNING)
            _log(f"\n✓ Claude Code config written:", SUCCESS)
            _log(f"  {code}", MUTED)
            _log(f"\n✓ Restart Claude Desktop to activate Synapse.", SUCCESS)
            _log(f"\nStart a conversation and say:  \"Load my memory context\"", FG)

            self.done_status.configure(
                text="✓  All done! Restart Claude Desktop and start chatting.",
                fg=SUCCESS,
            )
        except Exception as e:
            _log(f"✗ {e}", ERROR)
            self.done_status.configure(text="MCP config failed — see error above.", fg=ERROR)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = Wizard()
    app.mainloop()


if __name__ == "__main__":
    main()
