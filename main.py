# language: python, file: jax_god.py, target: Railway / VPS / Windows / Linux

import ast
import asyncio
import json
import logging
import os
import re
import sys
import time
import zipfile
import subprocess
import importlib.util
import shutil
import html
from pathlib import Path
from threading import Thread
from functools import wraps

import psutil
from dotenv import load_dotenv
from flask import Flask, jsonify

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ==================== CONFIG (RAILWAY READY) ====================
load_dotenv()

# Railway Environment Variables se BOT_TOKEN aur ADMIN_IDS uthayega
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

if not BOT_TOKEN:
    raise SystemExit("[-] BOT_TOKEN missing in environment variables")

# Railway apna PORT dynamically deta hai
PORT = int(os.getenv("PORT", "8080"))
MAX_ZIP_BYTES = 50 * 1024 * 1024  
MAX_RAM_MB_PER_PROCESS = 2048

BASE_DIR = Path(__file__).resolve().parent
WORKSPACES_DIR = BASE_DIR / "workspaces"
DATA_FILE = BASE_DIR / "jax_data.json"
WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("JAX_GOD")

ACTIVE_RUNNERS = {}
ANSI_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Mapping for mismatched pip package names
MODULE_MAPPER = {
    "user_agent": "user_agent", "fake_useragent": "fake-useragent", "bs4": "beautifulsoup4",
    "cv2": "opencv-python", "PIL": "Pillow", "telegram": "python-telegram-bot",
    "sklearn": "scikit-learn", "yaml": "pyyaml", "crypto": "pycryptodome",
    "Crypto": "pycryptodome", "fitz": "PyMuPDF", "dns": "dnspython", "docx": "python-docx",
    "pptx": "python-pptx", "serial": "pyserial", "dateutil": "python-dateutil",
    "jwt": "PyJWT", "websocket": "websocket-client", "requests": "requests"
}

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

# ==================== DEPENDENCY INSTALLER (AST) ====================
def auto_install_requirements(script_path):
    try:
        with open(script_path, "r", encoding="utf-8", errors="ignore") as f:
            code_content = f.read()

        tree = ast.parse(code_content, filename=str(script_path))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names: imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module: imports.add(node.module.split(".")[0])

        stdlib = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else {
            "os", "sys", "math", "json", "re", "ast", "subprocess", "time", "datetime",
            "random", "threading", "io", "socket", "urllib", "http", "collections",
            "itertools", "functools", "pathlib", "shutil", "logging", "asyncio"
        }

        for mod in imports:
            if mod not in stdlib and importlib.util.find_spec(mod) is None:
                pkg = MODULE_MAPPER.get(mod, mod)
                logger.info(f"Installing missing dependency: {pkg}")
                subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", pkg, "-q"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.error(f"Auto-install error: {e}")

# ==================== CREDENTIAL INJECTOR ====================
def inject_credentials(file_path, token, user_id):
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
        
        if re.search(r'(BOT_TOKEN|TOKEN|bot_token)\s*=\s*["\'].*?["\']', content):
            content = re.sub(r'(BOT_TOKEN|TOKEN|bot_token)\s*=\s*["\'].*?["\']', f'\\1 = "{token}"', content)
        else:
            content = f'BOT_TOKEN = "{token}"\n' + content

        if re.search(r'(USER_ID|CHAT_ID|ADMIN_ID|user_id|admin_id)\s*=\s*["\']?[0-9]*["\']?', content):
            content = re.sub(r'(USER_ID|CHAT_ID|ADMIN_ID|user_id|admin_id)\s*=\s*["\']?[0-9]*["\']?', f'\\1 = {user_id}', content)
        else:
            content = f'USER_ID = {user_id}\n' + content
            
        Path(file_path).write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        logger.error(f"Injection failed: {e}")
        return False

# ==================== FLASK KEEP-ALIVE (RAILWAY) ====================
app = Flask(__name__)

@app.route("/")
def health(): return jsonify(status="ok", service="jax-god-engine")

def run_flask():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    # 0.0.0.0 bind karna zaroori hai Railway ke liye
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ==================== UI KEYBOARDS ====================
def main_menu(user_id):
    kb = [
        ["🛑 Stop Active Script", "⚡ Running Tasks"],
        ["📁 File Manager", "🖥 System Stats"],
        ["📦 Pip Manager", "🧹 Clean Workspace"]
    ]
    if user_id in ADMIN_IDS: kb.append(["👑 Admin Panel"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, is_persistent=True)

def deploy_kb(filename):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Execute Script", callback_data=f"exec_{filename}")],
        [InlineKeyboardButton("👁️ View Source", callback_data=f"view_{filename}"),
         InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{filename}")]
    ])

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_conv")]])

# ==================== LIVE RUNNER (WITH PROMPT DETECTOR) ====================
class LiveRunner:
    def __init__(self, user_id, chat_id, script_path, workspace):
        self.user_id = user_id
        self.chat_id = chat_id
        self.script_path = script_path
        self.workspace = workspace
        self.proc = None
        self.logs = ""
        self.live_msg_id = None
        self.buffer = ""
        self.start_time = time.time()
        self.last_prompt = ""

    async def start(self, context: ContextTypes.DEFAULT_TYPE):
        env = os.environ.copy()
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "TERM": "xterm-256color"})

        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(self.script_path),
            cwd=str(self.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
            env=env
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛑 Kill Process", callback_data="proc_kill"),
             InlineKeyboardButton("📥 Download Logs", callback_data="proc_log")]
        ])
        
        header = (
            f"🚀 <b>LIVE SCRIPT EXECUTION</b>\n"
            f"📄 <b>File:</b> <code>{self.script_path.name}</code>\n"
            f"⚡ <b>PID:</b> <code>{self.proc.pid}</code>\n"
            f"⌨️ <i>Send text in chat to send input to terminal.</i>"
        )
        await context.bot.send_message(self.chat_id, header, parse_mode="HTML")
        live_msg = await context.bot.send_message(self.chat_id, "🖥 <b>Terminal:</b>\n<pre>Starting...</pre>", parse_mode="HTML", reply_markup=kb)
        self.live_msg_id = live_msg.message_id

        asyncio.create_task(self.stream_output(context))

    async def stream_output(self, context: ContextTypes.DEFAULT_TYPE):
        last_update = 0
        try:
            while True:
                try:
                    # 0.5 sec timeout to detect if script is waiting for input
                    chunk = await asyncio.wait_for(self.proc.stdout.read(1024), timeout=0.5)
                except asyncio.TimeoutError:
                    if self.buffer and not self.buffer.endswith('\n') and not self.buffer.endswith('\r'):
                        prompt_clean = self.buffer.strip().split('\n')[-1].strip()
                        if prompt_clean and prompt_clean != self.last_prompt and len(prompt_clean) < 150:
                            self.last_prompt = prompt_clean
                            await context.bot.send_message(
                                self.chat_id,
                                f"⚠️ <b>SCREEN INPUT REQUIRED</b>\n"
                                f"Terminal is asking for:\n<code>{html.escape(prompt_clean)}</code>\n\n"
                                f"<i>Please reply with the required information.</i>",
                                parse_mode="HTML"
                            )
                    continue

                if not chunk: break
                
                decoded = ANSI_REGEX.sub('', chunk.decode("utf-8", errors="replace"))
                self.logs += decoded
                self.buffer += decoded
                if len(self.buffer) > 35000: self.buffer = self.buffer[-35000:]

                now = asyncio.get_event_loop().time()
                if now - last_update > 2.0:
                    last_update = now
                    display = html.escape(self.buffer[-3500:].strip() or "Process active...")
                    try:
                        await context.bot.edit_message_text(
                            chat_id=self.chat_id, message_id=self.live_msg_id,
                            text=f"🖥 <b>Terminal:</b>\n<pre>{display}</pre>",
                            parse_mode="HTML"
                        )
                    except Exception: pass

            await self.proc.wait()
            final_text = html.escape(self.buffer[-3500:].strip() or "No output.")
            try:
                await context.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=self.live_msg_id,
                    text=f"🏁 <b>Execution Finished</b> (Exit: <code>{self.proc.returncode}</code>)\n<pre>{final_text}</pre>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download Logs", callback_data="proc_log")]])
                )
            except Exception: pass
        finally:
            if self.chat_id in ACTIVE_RUNNERS: del ACTIVE_RUNNERS[self.chat_id]

    async def send_input(self, text: str):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.write((text + "\n").encode("utf-8"))
                await self.proc.stdin.drain()
                self.last_prompt = "" # Reset so next prompt can be detected
            except Exception: pass

# ==================== CONVERSATIONS (INJECTION) ====================
WAIT_TOKEN, WAIT_UID = range(2)

async def exec_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    filename = query.data.replace("exec_", "")
    context.user_data["exec_file"] = filename

    await query.edit_message_text(
        "🔑 <b>Step 1/2:</b> Send me the <code>BOT_TOKEN</code> you want to inject into the script.\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode="HTML"
    )
    return WAIT_TOKEN

async def save_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["exec_token"] = update.message.text.strip()
    await update.message.reply_text(
        "👤 <b>Step 2/2:</b> Send me the <code>USER_ID</code> / <code>CHAT_ID</code> to inject."
    )
    return WAIT_UID

async def save_uid_and_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid_val = update.message.text.strip()
    filename = context.user_data.get("exec_file")
    token = context.user_data.get("exec_token")
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    ws_path = WORKSPACES_DIR / str(user_id)
    script_path = ws_path / filename

    if not script_path.exists():
        await update.message.reply_text("❌ File not found. It may have been deleted.")
        return ConversationHandler.END

    await update.message.reply_text("💉 <i>Injecting credentials into script...</i>", parse_mode="HTML")
    inject_credentials(script_path, token, uid_val)

    if chat_id in ACTIVE_RUNNERS:
        await update.message.reply_text("⚠️ A script is already running. Stop it first.", reply_markup=main_menu(user_id))
        return ConversationHandler.END

    await update.message.reply_text("🚀 <i>Starting execution...</i>", parse_mode="HTML")
    runner = LiveRunner(user_id, chat_id, script_path, ws_path)
    ACTIVE_RUNNERS[chat_id] = runner
    await runner.start(context)

    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Execution cancelled.")
    else:
        await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu(update.effective_user.id))
    return ConversationHandler.END

# ==================== HANDLERS ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        return await update.message.reply_text("⛔ <b>ACCESS DENIED: You are banned.</b>", parse_mode="HTML")

    if not is_approved(user.id):
        db["users"][str(user.id)] = {"status": "pending"}
        save_db(db)
        await update.message.reply_text("⏳ <b>Access request sent to Admin.</b>", parse_mode="HTML")
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}"), InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{user.id}")]])
        for admin in ADMIN_IDS:
            try: await context.bot.send_message(admin, f"🆕 <b>New User Request</b>\nID: <code>{user.id}</code>\nName: {user.full_name}", reply_markup=kb, parse_mode="HTML")
            except Exception: pass
        return

    dash = (
        f"🔥 <b>JAX GOD ENGINE ONLINE</b> 🔥\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user.mention_html()}\n"
        f"🔑 <b>Role:</b> <code>{'Administrator' if user.id in ADMIN_IDS else 'Authorized User'}</code>\n\n"
        f"📂 <i>Send any .py or .zip file to execute.</i>"
    )
    await update.message.reply_text(dash, parse_mode="HTML", reply_markup=main_menu(user.id))

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id) or is_banned(user_id): return

    doc = update.message.document
    if doc.file_size > MAX_ZIP_BYTES: return await update.message.reply_text("❌ Size Limit Exceeded (50MB).")

    ws_path = WORKSPACES_DIR / str(user_id)
    ws_path.mkdir(parents=True, exist_ok=True)
    clean_name = os.path.basename(doc.file_name).replace(" ", "_")
    target_path = ws_path / clean_name

    msg = await update.message.reply_text("📥 <i>Downloading...</i>", parse_mode="HTML")
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(target_path)

    if target_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(target_path, 'r') as zf: zf.extractall(ws_path)
        os.remove(target_path)
        py_files = list(ws_path.glob("*.py"))
        if not py_files:
            await msg.edit_text("❌ No .py file found in ZIP.", parse_mode="HTML")
            return
        target_path = py_files[0]
        await msg.edit_text(f"📦 <b>EXTRACTED & DEPLOYED:</b> <code>{target_path.name}</code>", parse_mode="HTML", reply_markup=deploy_kb(target_path.name))
    else:
        await msg.edit_text(f"📥 <b>DEPLOYED:</b> <code>{target_path.name}</code>\n🔍 <i>Checking dependencies...</i>", parse_mode="HTML")
        auto_install_requirements(target_path)
        await msg.edit_text(f"📥 <b>DEPLOYED & READY:</b> <code>{target_path.name}</code>", parse_mode="HTML", reply_markup=deploy_kb(target_path.name))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    if not is_approved(user_id) or is_banned(user_id): return

    # Send input to running process if active
    if chat_id in ACTIVE_RUNNERS and text not in ["🛑 Stop Active Script", "⚡ Running Tasks", "📁 File Manager", "🖥 System Stats", "📦 Pip Manager", "🧹 Clean Workspace", "👑 Admin Panel"]:
        await ACTIVE_RUNNERS[chat_id].send_input(text)
        await update.message.reply_text(f"⌨️ Sent to terminal: <code>{html.escape(text)}</code>", parse_mode="HTML")
        return

    if text == "🛑 Stop Active Script":
        if chat_id in ACTIVE_RUNNERS:
            proc = ACTIVE_RUNNERS[chat_id].proc
            if proc and proc.returncode is None: proc.terminate()
            del ACTIVE_RUNNERS[chat_id]
            await update.message.reply_text("🛑 <b>Process Terminated.</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("💤 No active scripts.", parse_mode="HTML")

    elif text == "⚡ Running Tasks":
        if chat_id in ACTIVE_RUNNERS:
            runner = ACTIVE_RUNNERS[chat_id]
            await update.message.reply_text(f"⚡ Running: <code>{runner.script_path.name}</code>\nPID: <code>{runner.proc.pid}</code>", parse_mode="HTML")
        else:
            await update.message.reply_text("💤 Engine Idle.", parse_mode="HTML")

    elif text == "📁 File Manager":
        ws_path = WORKSPACES_DIR / str(user_id)
        files = [f for f in ws_path.iterdir() if f.is_file() and f.name != "jax_data.json"]
        if not files: return await update.message.reply_text("📭 Workspace Empty.")
        for f in files:
            kb = [[InlineKeyboardButton("🚀 Execute", callback_data=f"exec_{f.name}"), InlineKeyboardButton("👁️ View", callback_data=f"view_{f.name}"), InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{f.name}")]]
            await update.message.reply_text(f"📄 <code>{f.name}</code> ({(f.stat().st_size/1024):.1f} KB)", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif text == "🖥 System Stats":
        cpu = psutil.cpu_percent(interval=0.2)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        await update.message.reply_text(
            f"🖥 <b>SERVER DASHBOARD</b>\n"
            f"⚙️ <b>CPU Load:</b> <code>{cpu}%</code>\n"
            f"🧠 <b>RAM:</b> <code>{ram.percent}%</code> ({round(ram.used/(1024**3), 2)}GB / {round(ram.total/(1024**3), 2)}GB)\n"
            f"💾 <b>Disk:</b> <code>{disk.percent}%</code>\n"
            f"🔥 <b>Active Tasks:</b> <code>{len(ACTIVE_RUNNERS)}</code>",
            parse_mode="HTML"
        )

    elif text == "📦 Pip Manager":
        await update.message.reply_text("📦 <i>Use command:</i> <code>/pip install library_name</code>", parse_mode="HTML")

    elif text == "🧹 Clean Workspace":
        ws_path = WORKSPACES_DIR / str(user_id)
        for f in ws_path.iterdir():
            if f.name.endswith(".log") or f.name == "__pycache__": shutil.rmtree(f, ignore_errors=True)
        await update.message.reply_text("🧹 <b>Workspace Cleaned.</b>", parse_mode="HTML")

    elif text == "👑 Admin Panel" and user_id in ADMIN_IDS:
        users_cnt = len(db["users"])
        banned_cnt = len(db.get("banned", []))
        kb = [[InlineKeyboardButton("📢 Broadcast", callback_data="admin_bc")], [InlineKeyboardButton("💾 Backup DB", callback_data="admin_backup")]]
        await update.message.reply_text(
            f"👑 <b>ADMIN DASHBOARD</b>\n"
            f"👥 <b>Total Users:</b> <code>{users_cnt}</code>\n"
            f"🚫 <b>Banned:</b> <code>{banned_cnt}</code>\n"
            f"🔥 <b>Active:</b> <code>{len(ACTIVE_RUNNERS)}</code>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb)
        )

async def pip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0] != "install" or len(context.args) < 2:
        return await update.message.reply_text("⚠️ Syntax: <code>/pip install package_name</code>", parse_mode="HTML")
    pkg = context.args[1]
    msg = await update.message.reply_text(f"⚙️ Installing <code>{pkg}</code>...", parse_mode="HTML")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await msg.edit_text(f"✅ Installed <code>{pkg}</code>!", parse_mode="HTML")
    except Exception:
        await msg.edit_text(f"❌ Failed to install <code>{pkg}</code>.", parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    ws_path = WORKSPACES_DIR / str(user_id)

    if data.startswith("view_"):
        fname = data.split("_", 1)[1]
        filepath = ws_path / fname
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8", errors="replace")[:3500]
            await context.bot.send_message(user_id, f"👁️ <pre>{html.escape(content)}</pre>", parse_mode="HTML")

    elif data.startswith("del_"):
        fname = data.split("_", 1)[1]
        filepath = ws_path / fname
        if filepath.exists():
            filepath.unlink()
            await query.edit_message_text(f"🗑️ Deleted <code>{fname}</code>", parse_mode="HTML")

    elif data == "proc_kill":
        chat_id = query.message.chat_id
        if chat_id in ACTIVE_RUNNERS:
            proc = ACTIVE_RUNNERS[chat_id].proc
            if proc and proc.returncode is None: proc.terminate()
            del ACTIVE_RUNNERS[chat_id]
            await query.edit_message_text("🛑 <b>Process Killed.</b>", parse_mode="HTML")

    elif data == "proc_log":
        chat_id = query.message.chat_id
        runner = ACTIVE_RUNNERS.get(chat_id)
        logs = runner.logs if runner else "No logs found."
        log_file = BASE_DIR / f"log_{user_id}.txt"
        log_file.write_text(logs, encoding="utf-8")
        with open(log_file, "rb") as f:
            await context.bot.send_document(user_id, document=InputFile(f, filename="terminal_logs.txt"))
        log_file.unlink()

    elif data.startswith("approve_") and user_id in ADMIN_IDS:
        tid = data.split("_")[1]
        db["users"][tid] = {"status": "approved"}; save_db(db)
        await query.edit_message_text(f"✅ Approved <code>{tid}</code>", parse_mode="HTML")
        try: await context.bot.send_message(int(tid), "🎉 <b>Access Granted!</b> Send /start", parse_mode="HTML")
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

    elif data == "admin_bc" and user_id in ADMIN_IDS:
        context.user_data["awaiting_broadcast"] = True
        await query.edit_message_text("📢 <i>Send the broadcast message now.</i>", parse_mode="HTML")

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
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")

# ==================== MAIN ====================
def main():
    # Flask server background thread me chalega
    Thread(target=run_flask, daemon=True).start()
    
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation Handler for Credential Injection
    exec_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(exec_entry, pattern="^exec_")],
        states={
            WAIT_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_token)],
            WAIT_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_uid_and_run)]
        },
        fallbacks=[CommandHandler("cancel", cancel_conv), CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("pip", pip_command))
    app.add_handler(exec_conv)
    
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    # Text handlers (handles menu buttons AND broadcast AND stdin to process)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_IDS) & filters.Regex(r'.*'), bc_handler), group=1) 
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("🚀 JAX GOD ENGINE STARTED ON RAILWAY")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    print("\033[92m[✓] Jax God Engine Loaded Successfully!\033[0m")
    main()
