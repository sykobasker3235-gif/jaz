# language: python, file: jax_god.py, target: Railway / VPS / Windows / Linux
# ULTRA OP — PYTHON-ONLY, BUTTON-DRIVEN
# v3 fixes:
#  * Injector injects any of bot_token/BOT_TOKEN/TOKEN, chat_id/USER_ID/... when
#    used-before-assigned (fixes NameError on child scripts using lowercase).
#  * Crash-report dedupe — one report per unique signature, then every 3rd, then 5min.
#  * Auto-stop after 10 identical crashes (kills infinite restart loop).
#  * Global error handler for handler exceptions; 409-Conflict logs a one-liner.

import ast
import asyncio
import json
import logging
import os
import re
import sys
import time
import tempfile
import zipfile
import subprocess
import importlib.util
import shutil
import html
from pathlib import Path
from threading import Thread

import psutil
from dotenv import load_dotenv
from flask import Flask, jsonify

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, InputFile
)
from telegram.error import Conflict
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ==================== CONFIG ====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
if not BOT_TOKEN:
    raise SystemExit("[-] BOT_TOKEN missing in environment variables")

PORT = int(os.getenv("PORT", "8080"))
MAX_ZIP_BYTES = int(os.getenv("MAX_ZIP_BYTES", str(50 * 1024 * 1024)))
MAX_RAM_MB_PER_PROCESS = int(os.getenv("MAX_RAM_MB", "2048"))
DEFAULT_RESTART_INTERVAL = int(os.getenv("RESTART_INTERVAL_SEC", "0"))
MAX_SAME_CRASH = int(os.getenv("MAX_SAME_CRASH", "10"))

BASE_DIR = Path(__file__).resolve().parent
WORKSPACES_DIR = BASE_DIR / "workspaces"
DATA_FILE = BASE_DIR / "jax_data.json"
AUDIT_FILE = BASE_DIR / "audit.log"
WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("JAX_GOD")

ACTIVE_RUNNERS: dict[int, "LiveRunner"] = {}
USER_ENV: dict[int, dict[str, str]] = {}
ANSI_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

MODULE_MAPPER = {
    "user_agent": "user_agent", "fake_useragent": "fake-useragent", "bs4": "beautifulsoup4",
    "cv2": "opencv-python", "PIL": "Pillow", "telegram": "python-telegram-bot",
    "sklearn": "scikit-learn", "yaml": "pyyaml", "crypto": "pycryptodome",
    "Crypto": "pycryptodome", "fitz": "PyMuPDF", "dns": "dnspython", "docx": "python-docx",
    "pptx": "python-pptx", "serial": "pyserial", "dateutil": "python-dateutil",
    "jwt": "PyJWT", "websocket": "websocket-client", "requests": "requests"
}

MENU_BUTTONS = {
    "🛑 Stop", "⚡ Running", "📁 Files", "🖥 Stats",
    "📦 Pip", "🔧 Env", "📥 Clone", "🧹 Clean",
    "🗑️ Delete All", "⚙️ Tools", "👑 Admin",
}

# ==================== AUDIT ====================
def audit(user_id: int, action: str, detail: str = ""):
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] uid={user_id} {action} {detail}\n")
    except Exception:
        pass

# ==================== DATABASE ====================
def load_db():
    if not DATA_FILE.exists(): return {"users": {}, "banned": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {"users": {}, "banned": []}

def save_db(db_data):
    with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump(db_data, f, indent=4)

db = load_db()
for admin_id in ADMIN_IDS:
    db["users"][str(admin_id)] = {"status": "approved"}

def is_approved(user_id: int) -> bool:
    if user_id in ADMIN_IDS: return True
    return db["users"].get(str(user_id), {}).get("status") == "approved"

def is_banned(user_id: int) -> bool:
    return user_id in db.get("banned", [])

# ==================== PYTHON-ONLY: SYNTAX / DEPS ====================
def scan_imports(path: Path) -> list[str]:
    if path.suffix.lower() != ".py":
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    except SyntaxError:
        return []
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names: imports.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()
    missing = []
    for m in sorted(imports):
        if m in stdlib: continue
        try:
            if importlib.util.find_spec(m) is None:
                missing.append(MODULE_MAPPER.get(m, m))
        except (ValueError, ImportError):
            missing.append(MODULE_MAPPER.get(m, m))
    return missing

def auto_install_requirements(script_path: Path) -> list[str]:
    installed = []
    for pkg in scan_imports(script_path):
        try:
            logger.info(f"Installing missing dependency: {pkg}")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--upgrade", pkg, "-q"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            installed.append(pkg)
        except Exception as e:
            logger.error(f"pip install {pkg} failed: {e}")
    return installed

def syntax_check(path: Path) -> tuple[bool, str]:
    if path.suffix.lower() != ".py":
        return False, "Only .py files are executable."
    try:
        compile(path.read_text(encoding="utf-8", errors="replace"), str(path), "exec")
        return True, ""
    except SyntaxError as e:
        loc = f"line {e.lineno}" + (f", col {e.offset}" if e.offset else "")
        return False, f"{e.msg} ({loc})\n{(e.text or '').rstrip()}"

def _make_preexec(ram_mb: int = 0):
    if not ram_mb or sys.platform == "win32":
        return None
    def _fn():
        try:
            import resource
            lim = ram_mb * 3 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
        except Exception:
            pass
    return _fn

# ==================== CREDENTIAL INJECTOR (alias-aware) ====================
_TOKEN_ALIASES = ("BOT_TOKEN", "TOKEN", "bot_token")
_ID_ALIASES = (
    "USER_ID", "CHAT_ID", "ADMIN_ID",
    "user_id", "chat_id", "admin_id",
    "OWNER_ID", "owner_id",
)

_TOKEN_LINE_RE = re.compile(
    r'^[ \t]*(BOT_TOKEN|TOKEN|bot_token)[ \t]*=(.*)$', re.MULTILINE)
_ID_PURE_INT_LINE_RE = re.compile(
    r'^[ \t]*(USER_ID|CHAT_ID|ADMIN_ID|user_id|chat_id|admin_id|OWNER_ID|owner_id)'
    r'[ \t]*=[ \t]*(["\']?)(\d+)\2[ \t]*(?:#.*)?$')
_TG_TOKEN_RE = re.compile(r'^\d{6,}:[A-Za-z0-9_\-]{30,}$')

def _is_real_token(rhs: str) -> bool:
    v = rhs.strip().strip('"').strip("'")
    return bool(_TG_TOKEN_RE.match(v))

def _get_token_value(text: str) -> str | None:
    m = _TOKEN_LINE_RE.search(text)
    if not m: return None
    v = m.group(2).strip().strip('"').strip("'")
    return v if _TG_TOKEN_RE.match(v) else None

def _first_use_before_assign(text: str, name: str) -> bool:
    """True if `name` is referenced AND either never assigned, or its first
    reference precedes its first assignment."""
    esc = re.escape(name)
    assign_re = re.compile(rf'^[ \t]*{esc}\s*(?::[^=\n]+)?=(?!=)', re.MULTILINE)
    use_re = re.compile(rf'\b{esc}\b')
    m = assign_re.search(text)
    if m is None:
        return use_re.search(text) is not None
    first_use = use_re.search(text)
    return first_use is not None and first_use.start() < m.start()

def inject_credentials(file_path, token: str, user_id: int) -> bool:
    try:
        p = Path(file_path)
        original = p.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines(keepends=True)

        out = []
        for line in lines:
            body = line.rstrip("\r\n")
            nl = line[len(body):]

            m_tok = _TOKEN_LINE_RE.match(body)
            if m_tok:
                alias, rhs = m_tok.group(1), m_tok.group(2)
                if _is_real_token(rhs):
                    out.append(line)
                else:
                    out.append(f'{alias} = "{token}"{nl or os.linesep}')
                continue

            m_id = _ID_PURE_INT_LINE_RE.match(body)
            if m_id:
                out.append(f'{m_id.group(1)} = {user_id}{nl or os.linesep}')
                continue

            out.append(line)

        joined = "".join(out)
        tok_val = _get_token_value(joined) or token

        preamble = []
        for alias in _TOKEN_ALIASES:
            if _first_use_before_assign(joined, alias):
                preamble.append(f'{alias} = "{tok_val}"')
        for alias in _ID_ALIASES:
            if _first_use_before_assign(joined, alias):
                preamble.append(f'{alias} = {user_id}')

        final = ("\n".join(preamble) + "\n" if preamble else "") + joined
        p.write_text(final, encoding="utf-8")
        logger.info(
            f"Injection OK on {p.name} (preamble={len(preamble)} alias(es), "
            f"tok_val={'script' if _get_token_value(joined) else 'engine'})")
        return True
    except Exception as e:
        logger.error(f"Injection failed: {e}")
        return False

# ==================== FLASK HEALTH ====================
app = Flask(__name__)

@app.route("/")
def health(): return jsonify(status="ok", service="jax-god-engine")

def run_flask():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ==================== KEYBOARDS ====================
def main_menu(user_id):
    kb = [
        ["🛑 Stop", "⚡ Running"],
        ["📁 Files", "🖥 Stats"],
        ["📦 Pip", "🔧 Env"],
        ["📥 Clone", "🧹 Clean"],
        ["🗑️ Delete All", "⚙️ Tools"],
    ]
    if user_id in ADMIN_IDS:
        kb.append(["👑 Admin"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, is_persistent=True)

def deploy_kb(filename):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Execute (24/7)", callback_data=f"exec_{filename}")],
        [InlineKeyboardButton("👁️ View", callback_data=f"view_{filename}"),
         InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{filename}"),
         InlineKeyboardButton("🔍 Dry", callback_data=f"dry_{filename}"),
         InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{filename}")]
    ])

def file_row_kb(fname):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Exec", callback_data=f"exec_{fname}"),
         InlineKeyboardButton("👁️ View", callback_data=f"view_{fname}")],
        [InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{fname}"),
         InlineKeyboardButton("🔍 Dry", callback_data=f"dry_{fname}"),
         InlineKeyboardButton("🗑️ Del", callback_data=f"del_{fname}")],
    ])

def tools_kb(user_id):
    rows = [
        [InlineKeyboardButton("📥 Logs", callback_data="tools_logs"),
         InlineKeyboardButton("📊 Status", callback_data="tools_status")],
    ]
    if user_id in ADMIN_IDS:
        rows.append([
            InlineKeyboardButton("📋 Audit", callback_data="tools_audit"),
            InlineKeyboardButton("▶️ Run All", callback_data="tools_runall"),
        ])
    return InlineKeyboardMarkup(rows)

def env_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add", callback_data="env_add"),
        InlineKeyboardButton("🧹 Clear All", callback_data="env_clear"),
    ]])

# ==================== LIVE RUNNER ====================
class LiveRunner:
    def __init__(self, user_id, chat_id, script_path, workspace,
                 auto_restart=True, max_ram_mb=0, restart_interval_sec=0):
        self.user_id = user_id
        self.chat_id = chat_id
        self.script_path = script_path
        self.workspace = workspace
        self.auto_restart = auto_restart
        self.max_ram_mb = max_ram_mb
        self.restart_interval_sec = restart_interval_sec
        self.proc = None
        self.logs = ""
        self.live_msg_id = None
        self.buffer = ""
        self.start_time = time.time()
        self.last_prompt = ""
        self.stopped = False
        self.restart_count = 0
        self.backoff = 2
        self._last_ram_check = 0
        self._last_crash_sig = ""
        self._crash_repeat = 0
        self._last_crash_report_ts = 0.0
        self.log_dir = workspace / "logs"
        self.log_dir.mkdir(exist_ok=True)
        self.log_path = self.log_dir / f"{script_path.stem}.log"

    async def start(self, context: ContextTypes.DEFAULT_TYPE):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛑 Kill", callback_data="proc_kill"),
             InlineKeyboardButton("📥 Logs", callback_data="proc_log"),
             InlineKeyboardButton("♻️ Restart", callback_data="proc_restart")]
        ])
        header = (
            f"🚀 <b>LIVE EXECUTION — 24/7 MODE</b>\n"
            f"📄 <b>File:</b> <code>{self.script_path.name}</code>\n"
            f"♻️ <b>Auto-restart:</b> <code>{'ON' if self.auto_restart else 'OFF'}</code>\n")
        if self.max_ram_mb:
            header += f"🧠 <b>RAM cap:</b> <code>{self.max_ram_mb}MB</code>\n"
        if self.restart_interval_sec:
            header += f"⏰ <b>Restart every:</b> <code>{self.restart_interval_sec}s</code>\n"
        header += "⌨️ <i>Send text in chat to feed stdin.</i>"
        await context.bot.send_message(self.chat_id, header, parse_mode="HTML")
        live_msg = await context.bot.send_message(
            self.chat_id,
            "🖥 <b>Terminal:</b>\n<pre>Booting supervisor...</pre>",
            parse_mode="HTML", reply_markup=kb)
        self.live_msg_id = live_msg.message_id
        asyncio.create_task(self._supervise(context))

    async def _supervise(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            while not self.stopped:
                await self._spawn(context)
                run_started = time.time()
                exit_code = await self._stream_until_exit(context, run_started)
                run_duration = time.time() - run_started
                if self.stopped:
                    break
                if exit_code != 0:
                    await self._send_crash_report(context, exit_code)

                if self._crash_repeat >= MAX_SAME_CRASH:
                    try:
                        await context.bot.send_message(
                            self.chat_id,
                            f"🛑 <b>Stopping — {self._crash_repeat} identical crashes.</b>\n"
                            f"Fix <code>{html.escape(self.script_path.name)}</code> then start again.",
                            parse_mode="HTML")
                    except Exception: pass
                    break

                if self.restart_interval_sec and run_duration >= self.restart_interval_sec:
                    try:
                        await context.bot.send_message(
                            self.chat_id,
                            f"⏰ <b>Scheduled restart</b> (ran {int(run_duration)}s)",
                            parse_mode="HTML")
                    except Exception: pass
                    continue

                if not self.auto_restart or exit_code == 0:
                    break
                if run_duration > 60:
                    self.backoff = 2
                    self.restart_count = 0

                self.restart_count += 1
                delay = self.backoff
                self.backoff = min(self.backoff * 2, 60)
                try:
                    await context.bot.send_message(
                        self.chat_id,
                        f"♻️ <b>Auto-Restart</b> — exit <code>{exit_code}</code>, "
                        f"attempt <b>{self.restart_count}</b> in <b>{delay}s</b>",
                        parse_mode="HTML")
                except Exception: pass
                await asyncio.sleep(delay)
        finally:
            ACTIVE_RUNNERS.pop(self.chat_id, None)
            try:
                await context.bot.send_message(
                    self.chat_id, "🏁 <b>Runner stopped.</b>", parse_mode="HTML")
            except Exception: pass

    async def _spawn(self, context: ContextTypes.DEFAULT_TYPE):
        env = os.environ.copy()
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "TERM": "xterm-256color"})
        env.update(USER_ENV.get(self.user_id, {}))
        kwargs = {}
        preexec = _make_preexec(self.max_ram_mb)
        if preexec: kwargs["preexec_fn"] = preexec
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(self.script_path),
            cwd=str(self.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
            env=env, **kwargs)
        try:
            await context.bot.edit_message_text(
                chat_id=self.chat_id, message_id=self.live_msg_id,
                text=(f"🖥 <b>Terminal:</b> (PID <code>{self.proc.pid}</code>, "
                      f"run <b>#{self.restart_count + 1}</b>)\n<pre>Booting...</pre>"),
                parse_mode="HTML")
        except Exception: pass

    async def _stream_until_exit(self, context: ContextTypes.DEFAULT_TYPE, run_started: float):
        last_update = 0
        self.buffer = ""
        self.last_prompt = ""
        log_fp = None
        try:
            log_fp = open(self.log_path, "a", encoding="utf-8")
            log_fp.write(f"\n===== RUN {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            log_fp.flush()
        except Exception: pass
        try:
            while True:
                if self.restart_interval_sec and (time.time() - run_started) >= self.restart_interval_sec:
                    try: self.proc.terminate()
                    except Exception: pass
                    try: await asyncio.wait_for(self.proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        try: self.proc.kill()
                        except Exception: pass

                now_t = time.time()
                if self.max_ram_mb and (now_t - self._last_ram_check) > 5:
                    self._last_ram_check = now_t
                    if self.proc and self.proc.returncode is None:
                        try:
                            proc_ = psutil.Process(self.proc.pid)
                            rss_mb = proc_.memory_info().rss / (1024 * 1024)
                            if rss_mb > self.max_ram_mb:
                                logger.warning(f"RAM cap exceeded: {rss_mb:.0f}MB > {self.max_ram_mb}MB")
                                proc_.kill()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                try:
                    chunk = await asyncio.wait_for(self.proc.stdout.read(1024), timeout=0.5)
                except asyncio.TimeoutError:
                    if self.buffer and not self.buffer.endswith(('\n', '\r')):
                        tail = self.buffer.strip().split('\n')[-1].strip()
                        if tail and tail != self.last_prompt and len(tail) < 150:
                            self.last_prompt = tail
                            try:
                                await context.bot.send_message(
                                    self.chat_id,
                                    f"⚠️ <b>SCREEN INPUT REQUIRED</b>\n"
                                    f"<code>{html.escape(tail)}</code>\n\n"
                                    f"<i>Reply to feed stdin.</i>",
                                    parse_mode="HTML")
                            except Exception: pass
                    continue

                if not chunk: break

                decoded = ANSI_REGEX.sub('', chunk.decode("utf-8", errors="replace"))
                self.logs += decoded
                self.buffer += decoded
                if len(self.buffer) > 35000: self.buffer = self.buffer[-35000:]
                if log_fp:
                    try: log_fp.write(decoded); log_fp.flush()
                    except Exception: pass

                loop = asyncio.get_running_loop()
                now = loop.time()
                if now - last_update > 2.0:
                    last_update = now
                    display = html.escape(self.buffer[-3500:].strip() or "Process active...")
                    try:
                        await context.bot.edit_message_text(
                            chat_id=self.chat_id, message_id=self.live_msg_id,
                            text=f"🖥 <b>Terminal:</b>\n<pre>{display}</pre>",
                            parse_mode="HTML")
                    except Exception: pass

            await self.proc.wait()
            return self.proc.returncode
        finally:
            if log_fp:
                try: log_fp.close()
                except Exception: pass

    async def _send_crash_report(self, context: ContextTypes.DEFAULT_TYPE, exit_code: int):
        if not ADMIN_IDS or exit_code == 0: return
        tail = self.buffer[-3000:] if self.buffer else ""
        sig = tail[-300:].strip() or "(no output)"
        now = time.time()
        if sig == self._last_crash_sig:
            self._crash_repeat += 1
        else:
            self._crash_repeat = 0
        self._last_crash_sig = sig

        should_send = (
            self._crash_repeat == 0
            or self._crash_repeat >= 3
            or (now - self._last_crash_report_ts) >= 300
        )
        if not should_send:
            return
        self._last_crash_report_ts = now

        repeat_note = f"\n🔁 <i>identical crash ×{self._crash_repeat}</i>" if self._crash_repeat > 0 else ""
        report = (
            f"🚨 <b>CRASH REPORT</b>{repeat_note}\n"
            f"👤 User: <code>{self.user_id}</code>\n"
            f"📄 Script: <code>{self.script_path.name}</code>\n"
            f"💥 Exit: <code>{exit_code}</code>\n"
            f"♻️ Restart #: <code>{self.restart_count}</code>\n\n"
            f"<pre>{html.escape(tail[-2500:])}</pre>")
        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(admin, report, parse_mode="HTML")
                break
            except Exception:
                continue

    async def send_input(self, text: str):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.write((text + "\n").encode("utf-8"))
                await self.proc.stdin.drain()
                self.last_prompt = ""
            except Exception: pass

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.returncode is None:
            try: self.proc.terminate()
            except Exception: pass

# ==================== SHARED VIEW HELPERS ====================
async def _send_status(target, context: ContextTypes.DEFAULT_TYPE, chat_id: int, edit: bool = False):
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    msg = (
        f"🖥 <b>SYSTEM</b>\n"
        f"⚙️ CPU: <code>{cpu}%</code>\n"
        f"🧠 RAM: <code>{ram.percent}%</code> ({ram.used//(1024**3)}/{ram.total//(1024**3)}GB)\n"
        f"💾 Disk: <code>{disk.percent}%</code>\n")
    runner = ACTIVE_RUNNERS.get(chat_id)
    if runner and runner.proc and runner.proc.returncode is None:
        try:
            p = psutil.Process(runner.proc.pid)
            rss = p.memory_info().rss / (1024 * 1024)
            cpu_p = p.cpu_percent(interval=0.1)
            uptime = int(time.time() - runner.start_time)
            msg += (
                f"\n🚀 <b>RUNNER</b>\n"
                f"📄 <code>{html.escape(runner.script_path.name)}</code>\n"
                f"⚡ PID: <code>{runner.proc.pid}</code>\n"
                f"♻️ Restarts: <code>{runner.restart_count}</code>\n"
                f"⏱️ Uptime: <code>{uptime}s</code>\n"
                f"🧠 RSS: <code>{rss:.1f}MB</code>\n"
                f"⚙️ CPU: <code>{cpu_p:.1f}%</code>")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            msg += "\n\n💤 Process exited."
    else:
        msg += "\n\n💤 No active runner."
    if edit:
        try:
            await target.edit_message_text(msg, parse_mode="HTML")
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id, msg, parse_mode="HTML")

async def _send_logs(target, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    runner = ACTIVE_RUNNERS.get(chat_id)
    logs = ""
    if runner:
        logs = runner.logs or ""
        if not logs:
            try: logs = runner.log_path.read_text(errors="replace")
            except Exception: pass
    if not logs:
        log_dir = WORKSPACES_DIR / str(user_id) / "logs"
        if log_dir.exists():
            candidates = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
            if candidates:
                logs = candidates[-1].read_text(errors="replace")
    if not logs:
        msg = "📭 No logs yet."
        try: return await target.edit_message_text(msg)
        except Exception: return await context.bot.send_message(user_id, msg)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(logs); path = tf.name
    try:
        with open(path, "rb") as f:
            await context.bot.send_document(user_id, document=InputFile(f, filename="logs.txt"))
    finally:
        try: os.remove(path)
        except Exception: pass

async def _send_audit(target, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if user_id not in ADMIN_IDS:
        try: return await target.edit_message_text("⛔ Admin only.")
        except Exception: return await context.bot.send_message(user_id, "⛔ Admin only.")
    if not AUDIT_FILE.exists():
        try: return await target.edit_message_text("📭 No audit log.")
        except Exception: return await context.bot.send_message(user_id, "📭 No audit log.")
    lines = AUDIT_FILE.read_text(errors="replace").splitlines()[-500:]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write("\n".join(lines)); path = tf.name
    try:
        with open(path, "rb") as f:
            await context.bot.send_document(user_id, document=InputFile(f, filename="audit.log"))
    finally:
        try: os.remove(path)
        except Exception: pass

# ==================== FILE ACTION DISPATCH ====================
async def do_exec(query, context: ContextTypes.DEFAULT_TYPE, filename: str):
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    ws_path = WORKSPACES_DIR / str(user_id)
    script_path = ws_path / filename
    if not script_path.exists():
        return await query.edit_message_text("❌ File not found.")
    if script_path.suffix.lower() != ".py":
        return await query.edit_message_text("❌ Only .py files can be executed.")
    if chat_id in ACTIVE_RUNNERS:
        return await query.edit_message_text("⚠️ A script is already running. Stop it first.")

    ok, err = syntax_check(script_path)
    if not ok:
        audit(user_id, "exec.refused.syntax", filename)
        return await query.edit_message_text(
            f"❌ <b>SYNTAX ERROR</b> — refusing to execute.\n"
            f"📄 <code>{html.escape(filename)}</code>\n"
            f"<pre>{html.escape(err[:2000])}</pre>",
            parse_mode="HTML")

    missing = scan_imports(script_path)
    if missing:
        await query.edit_message_text(
            f"📦 <i>Installing {len(missing)} missing dep(s): "
            f"<code>{', '.join(missing[:8])}</code>...</i>",
            parse_mode="HTML")
        await asyncio.to_thread(auto_install_requirements, script_path)

    await query.edit_message_text(
        f"💉 <i>Injecting credentials into <code>{html.escape(filename)}</code>...</i>",
        parse_mode="HTML")
    inject_credentials(script_path, BOT_TOKEN, user_id)
    audit(user_id, "exec.inject", filename)

    await query.edit_message_text(
        "🚀 <i>Starting 24/7 execution...</i>", parse_mode="HTML")
    runner = LiveRunner(user_id, chat_id, script_path, ws_path,
                        auto_restart=True,
                        max_ram_mb=MAX_RAM_MB_PER_PROCESS,
                        restart_interval_sec=DEFAULT_RESTART_INTERVAL)
    ACTIVE_RUNNERS[chat_id] = runner
    audit(user_id, "exec.start", filename)
    await runner.start(context)

async def do_dryrun(query, context: ContextTypes.DEFAULT_TYPE, filename: str):
    user_id = query.from_user.id
    path = WORKSPACES_DIR / str(user_id) / filename
    if not path.exists():
        return await query.edit_message_text("❌ File not found.")
    ok, err = syntax_check(path)
    missing = scan_imports(path)
    audit(user_id, "dryrun", filename)
    if ok:
        msg = f"✅ <b>Syntax OK</b> — <code>{html.escape(filename)}</code>\n"
    else:
        msg = f"❌ <b>Syntax error</b>\n<pre>{html.escape(err[:1500])}</pre>\n"
    msg += (f"📦 <b>Missing:</b> <code>{', '.join(missing)}</code>" if missing
            else "📦 <b>All imports satisfied.</b>")
    await query.edit_message_text(msg, parse_mode="HTML")

# ==================== TEXT HANDLER ====================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_approved(user_id) or is_banned(user_id): return

    text = update.message.text.strip()

    # 1) Editing flow
    editing = context.user_data.get("editing")
    if editing:
        context.user_data.pop("editing", None)
        if text.lower() in ("cancel", "❌ cancel"):
            return await update.message.reply_text("❌ Edit cancelled.", reply_markup=main_menu(user_id))
        try:
            Path(editing["path"]).write_text(update.message.text, encoding="utf-8")
            audit(user_id, "edit.save", editing["file"])
            ok, err = syntax_check(Path(editing["path"]))
            if ok:
                msg = f"✅ <b>Saved</b> <code>{html.escape(editing['file'])}</code> ({len(update.message.text)} bytes)"
            else:
                msg = (f"⚠️ <b>Saved with syntax error</b> — <code>{html.escape(editing['file'])}</code>\n"
                       f"<pre>{html.escape(err[:800])}</pre>")
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=main_menu(user_id))
        except Exception as e:
            await update.message.reply_text(f"❌ Save failed: {e}", reply_markup=main_menu(user_id))
        return

    # 2) Awaiting-argument states
    awaiting = context.user_data.get("awaiting")
    if awaiting:
        context.user_data.pop("awaiting", None)
        if text.lower() in ("cancel", "❌ cancel"):
            return await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu(user_id))

        if awaiting == "pip":
            msg = await update.message.reply_text(
                f"⚙️ Installing <code>{html.escape(text)}</code>...", parse_mode="HTML")
            try:
                await asyncio.to_thread(
                    subprocess.check_call,
                    [sys.executable, "-m", "pip", "install", text, "-q"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                await msg.edit_text(f"✅ Installed <code>{html.escape(text)}</code>!",
                                    parse_mode="HTML", reply_markup=main_menu(user_id))
            except Exception:
                await msg.edit_text(f"❌ Failed to install <code>{html.escape(text)}</code>.",
                                    parse_mode="HTML", reply_markup=main_menu(user_id))

        elif awaiting == "clone":
            if not shutil.which("git"):
                return await update.message.reply_text(
                    "❌ git not installed.", reply_markup=main_menu(user_id))
            ws = WORKSPACES_DIR / str(user_id)
            name = re.sub(r'[^a-zA-Z0-9_.-]', '_',
                          text.rstrip('/').split('/')[-1].replace('.git', '') or 'repo')
            target = ws / name
            if target.exists():
                return await update.message.reply_text(
                    f"❌ <code>{html.escape(name)}</code> already exists.",
                    parse_mode="HTML", reply_markup=main_menu(user_id))
            msg = await update.message.reply_text(
                f"📥 <i>Cloning {html.escape(text)}...</i>", parse_mode="HTML")
            try:
                r = await asyncio.to_thread(
                    subprocess.run, ["git", "clone", "--depth", "1", text, str(target)],
                    capture_output=True, text=True, timeout=180)
                if r.returncode != 0:
                    raise Exception((r.stderr or "")[-400:])
                py_files = list(target.rglob("*.py"))
                audit(user_id, "clone", text)
                await msg.edit_text(
                    f"✅ <b>Cloned</b> <code>{html.escape(name)}</code>\n"
                    f"🐍 {len(py_files)} .py files — see 📁 Files.",
                    parse_mode="HTML", reply_markup=main_menu(user_id))
            except Exception as e:
                await msg.edit_text(
                    f"❌ Clone failed: <code>{html.escape(str(e)[:300])}</code>",
                    parse_mode="HTML", reply_markup=main_menu(user_id))

        elif awaiting == "env_add":
            if "=" not in text:
                return await update.message.reply_text(
                    "❌ Format is <code>KEY=VALUE</code>.",
                    parse_mode="HTML", reply_markup=main_menu(user_id))
            k, v = text.split("=", 1)
            USER_ENV.setdefault(user_id, {})[k.strip()] = v.strip()
            audit(user_id, "env.set", k.strip())
            await update.message.reply_text(
                f"✅ Set <code>{html.escape(k.strip())}</code>",
                parse_mode="HTML", reply_markup=main_menu(user_id))
        return

    # 3) Feed stdin to a running process (skip menu labels)
    if chat_id in ACTIVE_RUNNERS and text not in MENU_BUTTONS:
        await ACTIVE_RUNNERS[chat_id].send_input(text)
        await update.message.reply_text(
            f"⌨️ Sent to terminal: <code>{html.escape(text)}</code>", parse_mode="HTML")
        return

    # 4) Menu buttons
    if text == "🛑 Stop":
        runner = ACTIVE_RUNNERS.get(chat_id)
        if runner:
            runner.stop()
            audit(user_id, "kill", runner.script_path.name)
            await update.message.reply_text("🛑 <b>Stop signal sent.</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("💤 No active scripts.", parse_mode="HTML")

    elif text == "⚡ Running":
        await _send_status(update.message, context, chat_id)

    elif text == "📁 Files":
        ws = WORKSPACES_DIR / str(user_id)
        files = sorted(f for f in ws.iterdir()
                       if f.is_file() and not f.name.startswith("."))
        dirs = sorted(d for d in ws.iterdir() if d.is_dir() and d.name != "logs")
        if not files and not dirs:
            return await update.message.reply_text("📭 Workspace empty.")
        runner = ACTIVE_RUNNERS.get(chat_id)
        running_path = runner.script_path if runner else None
        lines = ["📁 <b>Workspace</b>\n"]
        for f in files:
            icon = "🐍" if f.suffix.lower() == ".py" else "📄"
            size = f.stat().st_size
            marker = " ▶️ <b>RUNNING</b>" if running_path and f == running_path else ""
            lines.append(f"{icon} <code>{html.escape(f.name)}</code> — {size/1024:.1f}KB{marker}")
        for d in dirs:
            lines.append(f"📂 <code>{html.escape(d.name)}/</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        for f in files:
            if f.suffix.lower() == ".py":
                await update.message.reply_text(
                    f"<code>{html.escape(f.name)}</code>",
                    reply_markup=file_row_kb(f.name), parse_mode="HTML")

    elif text == "🖥 Stats":
        await _send_status(update.message, context, chat_id)

    elif text == "📦 Pip":
        context.user_data["awaiting"] = "pip"
        await update.message.reply_text(
            "📦 <b>Send the package name to install.</b>\n<i>Reply</i> <code>cancel</code> <i>to abort.</i>",
            parse_mode="HTML")

    elif text == "🔧 Env":
        env = USER_ENV.get(user_id, {})
        body = "🔧 <b>Env vars</b>\n"
        if env:
            for k, v in sorted(env.items()):
                body += f"<code>{html.escape(k)}</code> = <code>{html.escape(v[:80])}</code>\n"
        else:
            body += "<i>(none set)</i>\n"
        await update.message.reply_text(body, parse_mode="HTML", reply_markup=env_kb())

    elif text == "📥 Clone":
        if not shutil.which("git"):
            return await update.message.reply_text("❌ git not installed on server.")
        context.user_data["awaiting"] = "clone"
        await update.message.reply_text(
            "📥 <b>Send the git URL to clone.</b>\n<i>Reply</i> <code>cancel</code> <i>to abort.</i>",
            parse_mode="HTML")

    elif text == "🧹 Clean":
        ws = WORKSPACES_DIR / str(user_id)
        for f in list(ws.iterdir()):
            if f.name.endswith(".log") or f.name == "__pycache__":
                shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink()
        logs_dir = ws / "logs"
        if logs_dir.exists():
            shutil.rmtree(logs_dir, ignore_errors=True)
        await update.message.reply_text("🧹 <b>Workspace cleaned.</b>", parse_mode="HTML")

    elif text == "🗑️ Delete All":
        if chat_id in ACTIVE_RUNNERS:
            return await update.message.reply_text(
                "⚠️ Stop the running script first.", reply_markup=main_menu(user_id))
        ws = WORKSPACES_DIR / str(user_id)
        count = 0
        for f in list(ws.iterdir()):
            try:
                if f.is_file(): f.unlink(); count += 1
                elif f.is_dir(): shutil.rmtree(f, ignore_errors=True); count += 1
            except Exception: pass
        audit(user_id, "delete_all", str(count))
        await update.message.reply_text(
            f"🗑️ <b>Deleted {count} item(s) from workspace.</b>", parse_mode="HTML")

    elif text == "⚙️ Tools":
        await update.message.reply_text(
            "⚙️ <b>Tools</b>", parse_mode="HTML", reply_markup=tools_kb(user_id))

    elif text == "👑 Admin" and user_id in ADMIN_IDS:
        users_cnt = len(db["users"])
        banned_cnt = len(db.get("banned", []))
        kb = [
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_bc"),
             InlineKeyboardButton("💾 Backup DB", callback_data="admin_backup")],
            [InlineKeyboardButton("📋 Audit Log", callback_data="admin_audit")]
        ]
        await update.message.reply_text(
            f"👑 <b>ADMIN DASHBOARD</b>\n"
            f"👥 Users: <code>{users_cnt}</code> · 🚫 Banned: <code>{banned_cnt}</code>\n"
            f"🔥 Active: <code>{len(ACTIVE_RUNNERS)}</code>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ==================== DOCUMENT HANDLER ====================
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id) or is_banned(user_id): return
    doc = update.message.document
    if doc.file_size > MAX_ZIP_BYTES:
        return await update.message.reply_text("❌ Size Limit Exceeded (50MB).")

    fname = os.path.basename(doc.file_name or "")
    if not (fname.lower().endswith(".py") or fname.lower().endswith(".zip")):
        return await update.message.reply_text("❌ Only .py or .zip allowed.")

    ws_path = WORKSPACES_DIR / str(user_id)
    ws_path.mkdir(parents=True, exist_ok=True)
    clean_name = fname.replace(" ", "_")
    target_path = ws_path / clean_name

    msg = await update.message.reply_text("📥 <i>Downloading...</i>", parse_mode="HTML")
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(target_path)

    if target_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(target_path, 'r') as zf: zf.extractall(ws_path)
        os.remove(target_path)
        py_files = sorted(ws_path.glob("*.py"))
        if not py_files:
            return await msg.edit_text("❌ No .py file found in ZIP.", parse_mode="HTML")
        target_path = py_files[0]
        await msg.edit_text(
            f"📦 <b>EXTRACTED & DEPLOYED:</b> <code>{target_path.name}</code>\n"
            f"<i>{len(py_files)} .py file(s) in workspace</i>",
            parse_mode="HTML", reply_markup=deploy_kb(target_path.name))
    else:
        await msg.edit_text(
            f"📥 <b>DEPLOYED:</b> <code>{target_path.name}</code>\n🔍 <i>Checking...</i>",
            parse_mode="HTML")
        ok, err = syntax_check(target_path)
        missing = scan_imports(target_path)
        if not ok:
            await msg.edit_text(
                f"⚠️ <b>DEPLOYED (syntax error)</b>\n<code>{html.escape(target_path.name)}</code>\n"
                f"<pre>{html.escape(err[:800])}</pre>\n"
                f"<i>Fix via ✏️ Edit before executing.</i>",
                parse_mode="HTML", reply_markup=deploy_kb(target_path.name))
            return
        installed = await asyncio.to_thread(auto_install_requirements, target_path) if missing else []
        note = f"\n📦 installed: <code>{', '.join(installed)}</code>" if installed else ""
        await msg.edit_text(
            f"📥 <b>DEPLOYED & READY:</b> <code>{target_path.name}</code>{note}",
            parse_mode="HTML", reply_markup=deploy_kb(target_path.name))

# ==================== CALLBACK HANDLER ====================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id
    ws = WORKSPACES_DIR / str(user_id)

    if data.startswith("exec_"):
        return await do_exec(query, context, data[len("exec_"):])

    if data.startswith("view_"):
        fname = data[len("view_"):]
        p = ws / fname
        if not p.exists():
            return await query.edit_message_text("❌ File not found.")
        content = p.read_text(encoding="utf-8", errors="replace")[:3500]
        return await query.edit_message_text(
            f"👁️ <b>{html.escape(fname)}</b>\n<pre>{html.escape(content)}</pre>",
            parse_mode="HTML")

    if data.startswith("del_"):
        fname = data[len("del_"):]
        p = ws / fname
        if p.exists():
            p.unlink()
            audit(user_id, "file.delete", fname)
            await query.edit_message_text(f"🗑️ Deleted <code>{html.escape(fname)}</code>", parse_mode="HTML")

    if data.startswith("edit_"):
        fname = data[len("edit_"):]
        p = ws / fname
        if not p.exists():
            return await query.edit_message_text("❌ File not found.")
        context.user_data["editing"] = {"file": fname, "path": str(p)}
        return await query.edit_message_text(
            f"✏️ <b>Editing</b> <code>{html.escape(fname)}</code>\n\n"
            f"<i>Send the full new file content in your next message.</i>\n"
            f"<i>Reply</i> <code>cancel</code> <i>to abort.</i>",
            parse_mode="HTML")

    if data.startswith("dry_"):
        return await do_dryrun(query, context, data[len("dry_"):])

    if data == "proc_kill":
        runner = ACTIVE_RUNNERS.get(chat_id)
        if runner:
            runner.stop()
            await query.edit_message_text("🛑 <b>Kill signal sent.</b>", parse_mode="HTML")
        else:
            await query.edit_message_text("💤 No active process.", parse_mode="HTML")

    elif data == "proc_restart":
        runner = ACTIVE_RUNNERS.get(chat_id)
        if runner and runner.proc and runner.proc.returncode is None:
            runner.restart_count = 0
            runner.backoff = 2
            runner._crash_repeat = 0
            runner._last_crash_sig = ""
            try: runner.proc.terminate()
            except Exception: pass
            await query.answer("♻️ Restarting...", show_alert=False)
        else:
            await query.answer("💤 No active process.", show_alert=True)

    elif data == "proc_log":
        await _send_logs(query, context, chat_id, user_id)

    elif data == "tools_logs":
        await _send_logs(query, context, chat_id, user_id)

    elif data == "tools_status":
        await _send_status(query, context, user_id, edit=True)

    elif data == "tools_audit":
        await _send_audit(query, context, user_id)

    elif data == "tools_runall":
        if user_id not in ADMIN_IDS:
            return await query.edit_message_text("⛔ Admin only.")
        if chat_id in ACTIVE_RUNNERS:
            return await query.edit_message_text("⚠️ Stop the current runner first.")
        files = sorted(f for f in ws.iterdir() if f.is_file() and f.suffix.lower() == ".py")
        if not files:
            return await query.edit_message_text("📭 No .py files.")
        await query.edit_message_text(f"▶️ Running {len(files)} scripts sequentially...")
        for f in files:
            ok, err = syntax_check(f)
            if not ok:
                await context.bot.send_message(
                    user_id,
                    f"⏭️ Skipping <code>{html.escape(f.name)}</code> (syntax error)",
                    parse_mode="HTML")
                continue
            msg = await context.bot.send_message(
                user_id, f"▶️ <b>Running</b> <code>{html.escape(f.name)}</code>...",
                parse_mode="HTML")
            try:
                env = {**os.environ, **USER_ENV.get(user_id, {})}
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", str(f), cwd=str(ws),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env=env)
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
                except asyncio.TimeoutError:
                    proc.kill()
                    await msg.edit_text(f"⏱️ <b>{html.escape(f.name)}</b> timed out (300s)")
                    continue
                head = ANSI_REGEX.sub('', out.decode(errors="replace"))[:1500]
                await msg.edit_text(
                    f"✅ <b>{html.escape(f.name)}</b> exit <code>{proc.returncode}</code>\n"
                    f"<pre>{html.escape(head or '(no output)')}</pre>",
                    parse_mode="HTML")
            except Exception as e:
                await msg.edit_text(
                    f"❌ <b>{html.escape(f.name)}</b>: {html.escape(str(e)[:300])}",
                    parse_mode="HTML")

    elif data == "env_add":
        context.user_data["awaiting"] = "env_add"
        await query.edit_message_text(
            "🔧 <b>Send</b> <code>KEY=VALUE</code>\n<i>Reply</i> <code>cancel</code> <i>to abort.</i>",
            parse_mode="HTML")

    elif data == "env_clear":
        USER_ENV.pop(user_id, None)
        await query.edit_message_text("🧹 Env vars cleared.")

    elif data.startswith("approve_") and user_id in ADMIN_IDS:
        tid = data.split("_")[1]
        db["users"][tid] = {"status": "approved"}; save_db(db)
        await query.edit_message_text(f"✅ Approved <code>{tid}</code>", parse_mode="HTML")
        try:
            await context.bot.send_message(int(tid), "🎉 <b>Access Granted!</b> Tap /start", parse_mode="HTML")
        except Exception: pass

    elif data.startswith("ban_") and user_id in ADMIN_IDS:
        tid = data.split("_")[1]
        db["users"][tid] = {"status": "banned"}
        if int(tid) not in db["banned"]: db["banned"].append(int(tid))
        save_db(db)
        await query.edit_message_text(f"🚫 Banned <code>{tid}</code>", parse_mode="HTML")

    elif data == "admin_backup" and user_id in ADMIN_IDS:
        if DATA_FILE.exists():
            with open(DATA_FILE, "rb") as f:
                await context.bot.send_document(user_id, document=InputFile(f, filename="jax_data.json"))

    elif data == "admin_audit" and user_id in ADMIN_IDS:
        await _send_audit(query, context, user_id)

    elif data == "admin_bc" and user_id in ADMIN_IDS:
        context.user_data["awaiting_broadcast"] = True
        await query.edit_message_text("📢 <i>Send the broadcast message now.</i>", parse_mode="HTML")

# ==================== START / BROADCAST / ERROR HANDLER ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        return await update.message.reply_text("⛔ <b>ACCESS DENIED: You are banned.</b>", parse_mode="HTML")

    if not is_approved(user.id):
        db["users"][str(user.id)] = {"status": "pending"}
        save_db(db)
        await update.message.reply_text("⏳ <b>Access request sent to Admin.</b>", parse_mode="HTML")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}"),
            InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{user.id}")]])
        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin,
                    f"🆕 <b>New User Request</b>\nID: <code>{user.id}</code>\nName: {user.full_name}",
                    reply_markup=kb, parse_mode="HTML")
            except Exception: pass
        return

    dash = (
        f"🔥 <b>JAX GOD ENGINE — ULTRA OP</b> 🔥\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user.mention_html()}\n"
        f"🔑 <b>Role:</b> <code>{'Administrator' if user.id in ADMIN_IDS else 'Authorized User'}</code>\n\n"
        f"📂 <i>Send a .py or .zip to deploy. Everything else lives on the buttons below.</i>")
    await update.message.reply_text(dash, parse_mode="HTML", reply_markup=main_menu(user.id))

async def bc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_broadcast"): return
    context.user_data["awaiting_broadcast"] = False
    text = update.message.text
    sent = 0
    for uid in db["users"].keys():
        try:
            await context.bot.send_message(int(uid), f"📢 <b>ADMIN BROADCAST:</b>\n\n{text}", parse_mode="HTML")
            sent += 1
        except Exception: pass
    await update.message.reply_text(
        f"✅ Broadcast sent to {sent} users.", reply_markup=main_menu(update.effective_user.id))

async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        logger.warning("⚠️ Telegram 409 — duplicate poller. "
                       "Only ONE instance per bot token. Stop extra Railway deployments.")
        return
    logger.error(f"Handler error: {type(err).__name__}: {err}")

# ==================== MAIN ====================
def main():
    Thread(target=run_flask, daemon=True).start()
    app_ = Application.builder().token(BOT_TOKEN).build()

    app_.add_handler(CommandHandler("start", start_cmd))
    app_.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    app_.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_IDS) & filters.Regex(r'.*'),
                       bc_handler),
        group=1)
    app_.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app_.add_handler(CallbackQueryHandler(callback_handler))
    app_.add_error_handler(global_error_handler)

    logger.info("🚀 JAX GOD ENGINE STARTED — BUTTON-DRIVEN ULTRA OP")
    app_.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    print("\033[92m[✓] Jax God Engine — button-driven ULTRA OP loaded.\033[0m")
    main()
