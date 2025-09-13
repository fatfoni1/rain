import asyncio
import json
import os
import logging
import subprocess
import sys
import re
import time
from datetime import datetime
from typing import Optional, Tuple, List, Dict

import requests
import psutil
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

try:
    from gologin import GoLogin
    GOLOGIN_AVAILABLE = True
except ImportError:
    GOLOGIN_AVAILABLE = False

# ================== KONFIGURASI ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "bot_config.json")
START_BOT_SCRIPT = os.path.join(BASE_DIR, "watcher.py")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")

DEFAULT_CONFIG = {
    "telegram_token": "",
    "chat_id": "",
    "gologin_api_token": "",
    "gologin_profile_name": "",
    "gologin_profile_id": "",
    "capsolver_token": "",
    "cdp_url": "http://127.0.0.1:9222",
    "target_url": "https://flip.gg/profile",
    "check_interval_sec": 5,
    "reload_every_sec": 300,
    "join_cooldown_sec": 60,
    "turnstile_wait_ms": 600000,
    "after_join_idle_sec": 10,
    "headless": False
}

bot_process: Optional[subprocess.Popen] = None
config: Dict = {}
start_time: Optional[datetime] = None

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.request").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ================== KONFIGURASI MANAGEMENT ==================
def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            for key, value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = value
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            config = DEFAULT_CONFIG.copy()
    else:
        config = DEFAULT_CONFIG.copy()
    save_config()

def save_config():
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving config: {e}")

# ================== GOLOGIN UTILITIES ==================
API_BASE = "https://api.gologin.com"

def _api_headers() -> dict:
    token = config.get("gologin_api_token", "")
    return {"Authorization": f"Bearer {token}"} if token else {}

def _http_get(url: str, params: dict = None, timeout: int = 25):
    try:
        resp = requests.get(url, headers=_api_headers(), params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp
    except Exception as e:
        logger.error(f"[GoLogin] HTTP GET error: {e}")
        return None

def _resolve_profile_by_name(name: str) -> Tuple[Optional[str], str]:
    """
    Cari ID profil dari nama (case-insensitive) via /browser/v2.
    Prioritas exact match, fallback contains.
    """
    if not name:
        return None, "Nama profil kosong."
    resp = _http_get(f"{API_BASE}/browser/v2")
    if not resp:
        return None, "Gagal memanggil API GoLogin."
    data = resp.json()
    profiles: List[dict] = data.get("profiles", []) if isinstance(data, dict) else []
    if not profiles:
        return None, "Daftar profil kosong."

    exact = next((p for p in profiles if str(p.get("name","")).lower() == name.lower()), None)
    if exact:
        return exact.get("id"), f"Profil ditemukan (exact): {name}"

    part = next((p for p in profiles if name.lower() in str(p.get("name","")).lower()), None)
    if part:
        return part.get("id"), f"Profil ditemukan (contains): {part.get('name')}"
    return None, f"Profil '{name}' tidak ditemukan."

def _list_profiles(page: int = 1, limit: int = 30) -> Tuple[List[dict], str]:
    resp = _http_get(f"{API_BASE}/browser/v2", params={"page": page, "limit": limit})
    if not resp:
        return [], "Gagal memanggil API GoLogin."
    data = resp.json()
    profiles: List[dict] = data.get("profiles", []) if isinstance(data, dict) else []
    return profiles, f"Total: {len(profiles)}"

def _devtools_alive(cdp_url: str) -> bool:
    """Cek CDP: GET {cdp_url}/json/version"""
    try:
        probe = cdp_url.rstrip("/") + "/json/version"
        r = requests.get(probe, timeout=3)
        return r.ok
    except Exception:
        return False

def _extract_port_from_cdp(cdp_url: str) -> Optional[int]:
    """Ambil port dari http://127.0.0.1:<port>"""
    if not cdp_url:
        return None
    m = re.search(r":(\d+)$", cdp_url.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

def _kill_processes_for_port_by_cmdline(port: int, grace_seconds: float = 2.0) -> Tuple[bool, str]:
    """
    Bunuh proses yang diluncurkan dengan argumen '--remote-debugging-port=<port>'.
    Biasanya proses 'orbita.exe' atau 'chrome.exe'.
    """
    try:
        victims = set()
        for p in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
                if f"--remote-debugging-port={port}" in cmd:
                    victims.add(p.pid)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue

        if not victims:
            return False, f"Tidak ada proses dengan arg --remote-debugging-port={port}"

        for pid in victims:
            try:
                psutil.Process(pid).terminate()
            except psutil.NoSuchProcess:
                pass

        t0 = time.time()
        while time.time() - t0 < grace_seconds:
            alive = [pid for pid in victims if psutil.pid_exists(pid)]
            if not alive:
                return True, f"Terminated PID(s): {', '.join(map(str, victims))}"
            time.sleep(0.2)

        # paksa kill
        for pid in list(victims):
            if psutil.pid_exists(pid):
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass
        return True, f"Killed PID(s): {', '.join(map(str, victims))}"
    except Exception as e:
        return False, f"Kill by cmdline error: {e}"

def _kill_processes_for_port_by_net(port: int, grace_seconds: float = 2.0) -> Tuple[bool, str]:
    """
    Bunuh proses yang terhubung/listen pada TCP port lewat net_connections().
    Ini pengganti connections() yang deprecated.
    """
    try:
        victims = set()
        for p in psutil.process_iter(attrs=["pid", "name"]):
            try:
                for c in p.net_connections(kind="inet"):
                    if c.laddr and c.laddr.port == port:
                        victims.add(p.pid)
                    elif c.raddr and c.raddr.port == port:
                        victims.add(p.pid)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue

        # filter noise pid=0 (System Idle Process) atau pid negatif
        victims = {pid for pid in victims if pid and pid > 0}

        if not victims:
            return False, f"Tidak ada proses di port {port}"

        for pid in victims:
            try:
                psutil.Process(pid).terminate()
            except psutil.NoSuchProcess:
                pass

        t0 = time.time()
        while time.time() - t0 < grace_seconds:
            alive = [pid for pid in victims if psutil.pid_exists(pid)]
            if not alive:
                return True, f"Terminated PID(s): {', '.join(map(str, victims))}"
            time.sleep(0.2)

        # force kill
        for pid in list(victims):
            if psutil.pid_exists(pid):
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass
        return True, f"Killed PID(s): {', '.join(map(str, victims))}"
    except Exception as e:
        return False, f"Kill by net error: {e}"

def _cleanup_locked_account_web_data_from_error(err: Exception) -> Optional[List[str]]:
    """
    Jika error mengandung path '...\\Default\\Account Web Data', coba hapus file itu
    dan 'Account Web Data-journal'. Return list path yang dihapus, kalau ada.
    """
    msg = str(err)
    m = re.search(r"([A-Za-z]:\\\\[^\"']*Default\\\\Account Web Data)", msg)
    if not m:
        return None
    base = m.group(1)
    deleted = []
    for path in [base, base + "-journal"]:
        try:
            if os.path.exists(path):
                os.remove(path)
                deleted.append(path)
        except Exception:
            pass
    return deleted or None

def _start_gologin_profile(token: str, profile_id: str, headless: Optional[bool] = None) -> Tuple[bool, str]:
    """
    Start profil memakai SDK resmi, dapat debugger address (ip:port),
    set config['cdp_url'] dan simpan.
    Auto-cleanup 'Account Web Data' jika ke-lock di Windows.
    """
    if not GOLOGIN_AVAILABLE:
        return False, "Pustaka 'gologin' tidak terpasang. jalankan: pip install gologin"
    if not token:
        return False, "API token GoLogin belum di-set."
    if not profile_id:
        return False, "Profile ID kosong."

    try:
        opts = {"token": token, "profile_id": profile_id}
        if headless is True:
            opts["headless"] = True
        gl = GoLogin(opts)
        debugger_address = gl.start()  # contoh: "127.0.0.1:54321"
        if not debugger_address or ":" not in debugger_address:
            return False, f"Gagal start profil: debugger address invalid ({debugger_address})"

        host, port = debugger_address.split(":", 1)
        new_cdp = f"http://{host}:{port}"
        config["cdp_url"] = new_cdp
        save_config()
        logger.info(f"[GoLogin] Profil start OK. CDP: {new_cdp}")
        return True, f"Profil dijalankan. CDP: {new_cdp}"
    except Exception as e:
        deleted = _cleanup_locked_account_web_data_from_error(e)
        if deleted:
            logger.warning(f"[GoLogin] Menghapus file lock: {', '.join(deleted)} lalu retry start...")
            try:
                opts = {"token": token, "profile_id": profile_id}
                if headless is True:
                    opts["headless"] = True
                gl = GoLogin(opts)
                debugger_address = gl.start()
                if not debugger_address or ":" not in debugger_address:
                    return False, f"Gagal start profil (retry): debugger address invalid ({debugger_address})"
                host, port = debugger_address.split(":", 1)
                new_cdp = f"http://{host}:{port}"
                config["cdp_url"] = new_cdp
                save_config()
                logger.info(f"[GoLogin] Profil start OK (retry). CDP: {new_cdp}")
                return True, f"Profil dijalankan (retry). CDP: {new_cdp}"
            except Exception as e2:
                logger.error(f"[GoLogin] start error (retry): {e2}")
                return False, f"Gagal start profil: {e2}"
        logger.error(f"[GoLogin] start error: {e}")
        return False, f"Gagal start profil: {e}"

def _stop_gologin_profile(token: str, profile_id: str) -> Tuple[bool, str]:
    """
    Stop profil via SDK. Jika gagal (pid=0, dll.), fallback:
    1) kill berdasarkan arg '--remote-debugging-port=<port>'
    2) kill berdasarkan koneksi net (net_connections)
    """
    if not token:
        return False, "API token GoLogin belum di-set."
    if not profile_id:
        return False, "Profile ID kosong."

    # 1) Coba stop via SDK
    if GOLOGIN_AVAILABLE:
        try:
            gl = GoLogin({"token": token, "profile_id": profile_id})
            gl.stop()
            return True, "Profil dihentikan."
        except Exception as e:
            logger.error(f"[GoLogin] stop error: {e}")

    # Ambil port dari CDP untuk fallback
    cdp = config.get("cdp_url", "")
    port = _extract_port_from_cdp(cdp)
    if not port:
        return False, "Gagal stop: port CDP tidak diketahui."

    # 2) Kill via cmdline --remote-debugging-port
    ok1, msg1 = _kill_processes_for_port_by_cmdline(port)
    if ok1:
        return True, f"Profil dihentikan via cmdline. {msg1}"

    # 3) Kill via net_connections
    ok2, msg2 = _kill_processes_for_port_by_net(port)
    if ok2:
        return True, f"Profil dihentikan via net. {msg2}"

    return False, f"Gagal stop via SDK/cmdline/net. {msg1 if msg1 else msg2}"

def _status_gologin_profile() -> str:
    """
    Status sederhana: tampilkan nama/id aktif + status CDP URL.
    """
    name = config.get("gologin_profile_name", "") or "N/A"
    pid = config.get("gologin_profile_id", "") or "N/A"
    cdp = config.get("cdp_url", "")
    alive = "UP" if (cdp and _devtools_alive(cdp)) else "DOWN"
    return f"Profil: {name}\nID: {pid}\nCDP: {cdp}\nDevTools: {alive}"

# =============== MENU UTAMA =================
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan menu utama"""
    keyboard = [
        [InlineKeyboardButton("🚀 Start Bot", callback_data="start_bot")],
        [InlineKeyboardButton("🛑 Stop Bot", callback_data="stop_bot")],
        [InlineKeyboardButton("ℹ️ Info", callback_data="info")],
        [InlineKeyboardButton("🧰 GoLogin", callback_data="gologin_menu")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    is_running = bot_process and bot_process.poll() is None
    status = "🟢 Berjalan" if is_running else "🔴 Berhenti"
    text = (
        "🤖 *Bot Controller*\n\n"
        f"Status saat ini: {status}\n\n"
        "Pilih aksi yang ingin dilakukan:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk command /start"""
    await show_main_menu(update, context)

# =============== SUBMENU DLL =================
async def gologin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("▶️ Start Profile", callback_data="gologin_start_profile"),
         InlineKeyboardButton("🔎 Status", callback_data="gologin_status")],
        [InlineKeyboardButton("⏹ Stop Profile", callback_data="gologin_stop_profile"),
         InlineKeyboardButton("🔁 Restart", callback_data="gologin_restart_profile")],
        [InlineKeyboardButton("📜 Daftar Profil", callback_data="gologin_list_profiles")],
        [InlineKeyboardButton("✅ Pilih Profil Aktif", callback_data="edit_gologin_profile_name")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    profile_id = config.get('gologin_profile_id', 'N/A')
    text = (
        "🧰 GoLogin\n\n"
        f"Profil Aktif (nama): `{config.get('gologin_profile_name','N/A')}`\n"
        f"Profil Aktif (ID): `{profile_id}`\n"
        f"CDP URL: `{config.get('cdp_url','N/A')}`\n"
        f"API Token: `...{config.get('gologin_api_token','')[-5:]}`\n\n"
        "Pilih aksi untuk mengelola profil GoLogin."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔑 Ganti API GoLogin", callback_data="edit_gologin_api_token")],
        [InlineKeyboardButton("👤 Ganti Profil GoLogin", callback_data="edit_gologin_profile_name")],
        [InlineKeyboardButton("🔐 Ganti API CapSolver", callback_data="edit_capsolver_token")],
        [InlineKeyboardButton("⚙️ Ganti Token Telegram", callback_data="edit_telegram_token")],
        [InlineKeyboardButton("🎯 Edit Target URL", callback_data="edit_target_url")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "⚙️ *Settings*\n\n"
        f"🔑 API GoLogin: `...{config.get('gologin_api_token', '')[-5:]}`\n"
        f"👤 Profil GoLogin: `{config.get('gologin_profile_name', 'Not Set')}`\n"
        f"🔐 API CapSolver: `...{config.get('capsolver_token', '')[-5:]}`\n"
        f"🎯 Target URL: `{config.get('target_url', 'Not Set')}`\n"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

# ================== BUTTON HANDLER ==================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_process, start_time
    query = update.callback_query
    await query.answer()

    if query.data == "start_bot":
        is_running = bot_process and bot_process.poll() is None
        if is_running:
            keyboard = [[InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "⚠️ Bot sudah berjalan!\n\n"
                "Status: 🟢 Berjalan\n"
                "Profil: " + config.get('gologin_profile_name', 'N/A'),
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            if not config.get('telegram_token') or not config.get('chat_id'):
                keyboard = [[InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    "❌ Harap set Token Telegram dan Chat ID terlebih dahulu!\n\nGunakan menu Settings untuk mengatur konfigurasi.", 
                    reply_markup=reply_markup
                )
                return
            # Tampilkan opsi mode eksekusi
            keyboard = [
                [InlineKeyboardButton("👻 Headless", callback_data="start_bot_headless"),
                 InlineKeyboardButton("🖥 Visible", callback_data="start_bot_visible")],
                [InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="main_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "Pilih mode eksekusi bot:",
                reply_markup=reply_markup
            )

    elif query.data == "start_bot_headless":
        # Simpan mode headless dan mulai bot (watcher)
        config['headless'] = True
        save_config()
        if bot_process and bot_process.poll() is None:
            bot_process.terminate()
            await asyncio.sleep(0.5)
        bot_process = subprocess.Popen([sys.executable, START_BOT_SCRIPT])
        start_time = datetime.now()
        keyboard = [[InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "✅ Bot telah dimulai (mode: Headless)!\n\n"
            "Status: 🟢 Berjalan\n"
            "Target: " + config['target_url'] + "\n"
            "Profil: " + config.get('gologin_profile_name', 'N/A'),
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    elif query.data == "start_bot_visible":
        # Simpan mode visible dan mulai bot (watcher)
        config['headless'] = False
        save_config()
        if bot_process and bot_process.poll() is None:
            bot_process.terminate()
            await asyncio.sleep(0.5)
        bot_process = subprocess.Popen([sys.executable, START_BOT_SCRIPT])
        start_time = datetime.now()
        keyboard = [[InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "✅ Bot telah dimulai (mode: Visible)!\n\n"
            "Status: 🟢 Berjalan\n"
            "Target: " + config['target_url'] + "\n"
            "Profil: " + config.get('gologin_profile_name', 'N/A'),
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    elif query.data == "stop_bot":
        is_running = bot_process and bot_process.poll() is None
        if is_running:
            bot_process.terminate()
            bot_process = None
            keyboard = [[InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "🤖 *Bot Controller*\n\n"
                "🛑 Bot CDP telah dihentikan!\n\n"
                "Status: 🔴 Berhenti\n"
                "Target: " + config['target_url'] + "\n"
                "Profil: " + config.get('gologin_profile_name', 'N/A'),
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            keyboard = [[InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "🤖 *Bot Controller*\n\n"
                "⚠️ Bot tidak sedang berjalan!\n\n"
                "Status: 🔴 Berhenti\n"
                "Bot CDP sudah dalam keadaan berhenti.", 
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    elif query.data == "info":
        is_running = bot_process and bot_process.poll() is None
        status = "🟢 Berjalan" if is_running else "🔴 Berhenti"
        uptime = "N/A"
        if is_running and start_time:
            delta = datetime.now() - start_time
            uptime = str(delta).split('.')[0]
        capsolver_balance = await get_capsolver_balance()
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="info")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (
            f"ℹ️ Informasi Bot\n\n"
            f"Status: {status}\n"
            f"Uptime: {uptime}\n"
            f"Profil GoLogin: {config.get('gologin_profile_name', 'N/A')}\n"
            f"Target: {config.get('target_url', 'N/A')}\n"
            f"Saldo CapSolver: `{capsolver_balance}`"
        )
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    elif query.data == "gologin_menu":
        await gologin_menu(update, context)

    elif query.data == "settings":
        await settings_menu(update, context)

    elif query.data == "main_menu":
        await show_main_menu(update, context)

    # ====== Aksi GoLogin ======
    elif query.data == "gologin_status":
        status_txt = _status_gologin_profile()
        keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
        await query.edit_message_text(f"📡 Status GoLogin\n\n`{status_txt}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "gologin_start_profile":
        token = config.get('gologin_api_token','')
        # Tentukan profile_id: pakai yang tersimpan, fallback resolve by name
        profile_id = config.get('gologin_profile_id','')
        if not profile_id:
            name = config.get('gologin_profile_name','')
            pid, msg = _resolve_profile_by_name(name)
            if not pid:
                keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
                await query.edit_message_text(f"❌ {msg}", reply_markup=InlineKeyboardMarkup(keyboard))
                return
            profile_id = pid
            config["gologin_profile_id"] = profile_id
            save_config()
        # Tampilkan pilihan mode start profil
        keyboard = [
            [InlineKeyboardButton("👻 Headless", callback_data="gologin_start_profile_headless"),
             InlineKeyboardButton("🖥 Visible", callback_data="gologin_start_profile_visible")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]
        ]
        await query.edit_message_text("Pilih mode untuk menjalankan profil GoLogin:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "gologin_start_profile_headless":
        token = config.get('gologin_api_token','')
        profile_id = config.get('gologin_profile_id','')
        if not profile_id:
            name = config.get('gologin_profile_name','')
            pid, msg = _resolve_profile_by_name(name)
            if not pid:
                keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
                await query.edit_message_text(f"❌ {msg}", reply_markup=InlineKeyboardMarkup(keyboard))
                return
            profile_id = pid
            config["gologin_profile_id"] = profile_id
            save_config()
        # simpan mode headless
        config['headless'] = True
        save_config()
        ok, msg = _start_gologin_profile(token, profile_id, headless=True)
        keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
        await query.edit_message_text(("✅ " if ok else "❌ ") + msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "gologin_start_profile_visible":
        token = config.get('gologin_api_token','')
        profile_id = config.get('gologin_profile_id','')
        if not profile_id:
            name = config.get('gologin_profile_name','')
            pid, msg = _resolve_profile_by_name(name)
            if not pid:
                keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
                await query.edit_message_text(f"❌ {msg}", reply_markup=InlineKeyboardMarkup(keyboard))
                return
            profile_id = pid
            config["gologin_profile_id"] = profile_id
            save_config()
        # simpan mode visible
        config['headless'] = False
        save_config()
        ok, msg = _start_gologin_profile(token, profile_id, headless=False)
        keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
        await query.edit_message_text(("✅ " if ok else "❌ ") + msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "gologin_stop_profile":
        token = config.get('gologin_api_token','')
        profile_id = config.get('gologin_profile_id','')
        if not profile_id:
            name = config.get('gologin_profile_name','')
            pid, _ = _resolve_profile_by_name(name)
            profile_id = pid or ""
        ok, msg = _stop_gologin_profile(token, profile_id)
        keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
        await query.edit_message_text(("✅ " if ok else "❌ ") + msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "gologin_restart_profile":
        token = config.get('gologin_api_token','')
        profile_id = config.get('gologin_profile_id','')
        if not profile_id:
            name = config.get('gologin_profile_name','')
            pid, _ = _resolve_profile_by_name(name)
            profile_id = pid or ""
            config["gologin_profile_id"] = profile_id
            save_config()
        ok1, msg1 = _stop_gologin_profile(token, profile_id)
        await asyncio.sleep(1.0)
        ok2, msg2 = _start_gologin_profile(token, profile_id)
        ok = ok1 and ok2
        keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
        await query.edit_message_text(("✅ " if ok else "❌ ") + f"Stop: {msg1}\nStart: {msg2}", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "gologin_list_profiles":
        profiles, meta = _list_profiles(page=1, limit=30)
        if not profiles:
            keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
            await query.edit_message_text("❌ Gagal mengambil daftar profil.", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        rows: List[List[InlineKeyboardButton]] = []
        for p in profiles[:10]:
            pid = p.get("id","")
            pname = p.get("name","(no-name)")
            rows.append([InlineKeyboardButton(f"✅ {pname}", callback_data=f"gologin_choose_profile:{pid}:{pname}")])
        rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")])
        await query.edit_message_text(
            f"📜 Daftar Profil (max 10)\n{meta}\n\nPilih salah satu untuk dijadikan aktif:",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    elif query.data.startswith("gologin_choose_profile:"):
        # format: gologin_choose_profile:<id>:<name>
        try:
            _, pid, pname = query.data.split(":", 2)
        except ValueError:
            keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
            await query.edit_message_text("❌ Format pemilihan profil tidak valid.", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        config["gologin_profile_id"] = pid
        config["gologin_profile_name"] = pname
        save_config()
        keyboard = [[InlineKeyboardButton("⬅️ Kembali", callback_data="gologin_menu")]]
        await query.edit_message_text(f"✅ Profil aktif di-set ke:\nNama: `{pname}`\nID: `{pid}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    # ====== Handler untuk edit_* field (Settings) ======
    elif query.data.startswith("edit_"):
        context.user_data['editing'] = query.data.replace("edit_", "")
        field_names = {
            'gologin_api_token': 'API Token GoLogin',
            'gologin_profile_name': 'Nama Profil GoLogin',
            'capsolver_token': 'API Token CapSolver',
            'telegram_token': 'Token Telegram',
            'target_url': 'Target URL',
        }
        field_name = field_names.get(context.user_data['editing'], 'Field')
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="settings")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"✏️ Masukkan {field_name} baru:\n\nKetik nilai baru dan kirim, atau klik Batal untuk kembali.",
            reply_markup=reply_markup
        )

# ================== GET CAPSOLVER BALANCE ==================
async def get_capsolver_balance():
    capsolver_token = config.get("capsolver_token")
    if not capsolver_token or capsolver_token == "MASUKKAN_API_KEY_CAPSOLVER_DISINI":
        return "Token Capsolver belum diatur."
    try:
        url = "https://api.capsolver.com/getBalance"
        headers = {"Content-Type": "application/json"}
        data = {"clientKey": capsolver_token}
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            result = response.json()
            if result.get("errorId") == 0:
                return f"${result.get('balance', 0):.4f}"
            else:
                return f"Error: {result.get('errorDescription', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Gagal mendapatkan saldo Capsolver: {e}")
        return "Gagal mengambil saldo."

# ================== MESSAGE HANDLER ==================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'editing' in context.user_data:
        field = context.user_data['editing']
        value = update.message.text.strip()
        config[field] = value
        save_config()
        field_names = {
            'gologin_api_token': 'API Token GoLogin',
            'gologin_profile_name': 'Nama Profil GoLogin',
            'capsolver_token': 'API Token CapSolver',
            'telegram_token': 'Token Telegram',
            'target_url': 'Target URL',
        }
        field_name = field_names.get(field, 'Field')
        await update.message.reply_text(f"✅ {field_name} berhasil diupdate!")
        del context.user_data['editing']
        await update.message.reply_text("Kembali ke menu pengaturan...")
        await settings_menu(update, None)
        if field == 'telegram_token':
            await update.message.reply_text("Token Telegram diubah. Controller akan direstart dalam 3 detik...")
            await asyncio.sleep(3)
            os.execv(sys.executable, ['python'] + sys.argv)
    else:
        await show_main_menu(update, context)

# ================== ERROR HANDLER ==================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "Message is not modified" not in str(context.error):
        logger.error(f"Update {update} caused error {context.error}")

# ================== ENTRY POINT ==================
def main():
    load_config()
    if not config.get('telegram_token'):
        print("❌ Token Telegram belum diset!")
        print("Silakan edit file bot_config.json dan masukkan token Telegram Anda")
        return
    app = Application.builder().token(config['telegram_token']).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)
    print("🤖 Telegram Bot Controller dimulai...")
    print(f"📋 Config file: {CONFIG_FILE}")
    print("💡 Gunakan /start di Telegram untuk mengontrol bot")
    app.run_polling()

if __name__ == "__main__":
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except ImportError:
        pass
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Bot dihentikan oleh user")
    except Exception as e:
        print(f"💥 Fatal error: {e}")
