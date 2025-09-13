import asyncio
import json
import os
import logging
import subprocess
import sys
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ================== KONFIGURASI ==================
# Menggunakan path dinamis agar bisa dijalankan dari mana saja
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "bot_config.json")
START_BOT_SCRIPT = os.path.join(BASE_DIR, "start_gologin_and_bot.py")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")

# Default config
DEFAULT_CONFIG = {
    "telegram_token": "",
    "chat_id": "",
    "gologin_api_token": "",
    "gologin_profile_name": "",
    "capsolver_token": "",
    "cdp_url": "http://127.0.0.1:9222",
    "target_url": "https://flip.gg/",
    "check_interval_sec": 5,
    "reload_every_sec": 300,
    "join_cooldown_sec": 60,
    "turnstile_wait_ms": 600000,
    "after_join_idle_sec": 10
}

# Variabel global
bot_process = None  # Menyimpan proses bot yang berjalan
config = {}
start_time = None # Waktu bot dimulai
MAX_LOGS = 15     # Maksimal log yang ditampilkan di status

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================== KONFIGURASI MANAGEMENT ==================
def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # Pastikan semua key ada
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

async def get_capsolver_balance():
    """Mendapatkan saldo Capsolver melalui API."""
    capsolver_token = config.get("capsolver_token")
    if not capsolver_token or capsolver_token == "MASUKKAN_API_KEY_CAPSOLVER_DISINI":
        return "Token Capsolver belum diatur."

    try:
        url = "https://api.capsolver.com/getBalance"
        headers = {"Content-Type": "application/json"}
        data = {"clientKey": capsolver_token}
        
        # Menggunakan httpx untuk request async
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

# ================== TELEGRAM BOT HANDLERS ==================
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan menu utama"""
    keyboard = [
        [InlineKeyboardButton("🚀 Start Bot", callback_data="start_bot")],
        [InlineKeyboardButton("🛑 Stop Bot", callback_data="stop_bot")],
        [InlineKeyboardButton("📊 Status", callback_data="status")],
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

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu settings"""
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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk inline keyboard buttons"""
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
                
            bot_process = subprocess.Popen([sys.executable, START_BOT_SCRIPT])
            start_time = datetime.now()
            keyboard = [[InlineKeyboardButton("⬅️ Kembali ke Menu", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "✅ Bot telah dimulai!\n\n"
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
    
    elif query.data == "status":
        is_running = bot_process and bot_process.poll() is None
        status = "🟢 Berjalan" if is_running else "🔴 Berhenti"
        
        uptime = "N/A"
        if is_running and start_time:
            delta = datetime.now() - start_time
            uptime = str(delta).split('.')[0]

        # Dapatkan saldo capsolver
        capsolver_balance = await get_capsolver_balance()

        # Membaca log dari file bot.log
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                logs = f.readlines()
            log_text = "".join(logs[-MAX_LOGS:])
        except FileNotFoundError:
            log_text = "File log belum dibuat."
        except Exception as e:
            log_text = f"Gagal membaca log: {e}"
        
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh Log", callback_data="status")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (f"📊 *Status Bot*\n\n"
                f"Status: {status}\n"
                f"Uptime: {uptime}\n\n"
                f"💰 Saldo Capsolver: `{capsolver_balance}`\n\n"
                f"📝 *Log Aktivitas Terbaru:*\n"
                f"```\n{log_text}\n```")
        
        await query.edit_message_text(
            text, 
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    elif query.data == "settings":
        await settings_menu(update, context)
    
    elif query.data == "main_menu":
        await start_command(update, context)
    
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

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk text messages"""
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
        
        # Kembali ke settings menu
        # Perlu memanggil ulang karena message handler tidak punya query
        await update.message.reply_text("Kembali ke menu pengaturan...")
        await settings_menu(update, None)

    else:
        # Abaikan pesan lain jika tidak sedang mengedit
        # Tampilkan menu utama untuk semua pesan lainnya
        await show_main_menu(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk error"""
    if "Message is not modified" not in str(context.error):
        logger.error(f"Update {update} caused error {context.error}")

def main():
    """Main function"""
    load_config()
    
    if not config.get('telegram_token'):
        print("❌ Token Telegram belum diset!")
        print("Silakan edit file bot_config.json dan masukkan token Telegram Anda")
        return
    
    # Setup Telegram bot
    app = Application.builder().token(config['telegram_token']).build()
    
    # Add handlers
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)
    
    print("🤖 Telegram Bot Controller dimulai...")
    print(f"📋 Config file: {CONFIG_FILE}")
    print("💡 Gunakan /start di Telegram untuk mengontrol bot")
    
    # Run bot polling
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