import asyncio, time, random, json, re
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from capsolver_handler import CapsolverHandler
from telegram_notifier import TelegramNotifier

# Import fitur baru untuk login/logout detection
try:
    from asf_core import (
        load_accounts, 
        find_account_by_name, 
        get_balance, 
        validate_token_requests_fast,
        send_telegram as asf_send_telegram
    )
    from asf_token_refresher import refresh_invalid_tokens
    ASF_AVAILABLE = True
    print("[INIT] ASF login/logout detection available")
except ImportError as e:
    print(f"[INIT] ASF modules not available: {e}")
    ASF_AVAILABLE = False

# ================== LOAD CONFIG ==================
def load_config():
    try:
        with open('bot_config.json', 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"[CONFIG] Error loading config: {e}")
        return {}

config = load_config()
FAST_EXECUTE = bool(config.get("fast_execute", False))
MAX_CF_RELOAD = 2

def _save_fast_result(status: str):
    try:
        with open('fast_exec_result.json', 'w') as f:
            json.dump({"status": status, "ts": time.time()}, f)
    except Exception:
        pass

# ================== KONFIG ==================
CDP_URL = config.get("cdp_url", "http://127.0.0.1:9222")
TARGET_URL = config.get("target_url", "https://flip.gg/profile")
CHECK_INTERVAL_SEC = config.get("check_interval_sec", 5)
RELOAD_EVERY_SEC = config.get("reload_every_sec", 300)
JOIN_COOLDOWN_SEC = config.get("join_cooldown_sec", 60)
TURNSTILE_WAIT_MS = config.get("turnstile_wait_ms", 600000)
AFTER_JOIN_IDLE_SEC = config.get("after_join_idle_sec", 10)
AUTO_SOLVE_CAPTCHA = config.get("auto_solve_captcha", True)
MAX_CAPTCHA_WAIT_TIME = config.get("max_captcha_wait_time", 120)
SUCCESS_CHECK_TIMEOUT = config.get("success_check_timeout", 15)
ENTERED_CHECK_TIMEOUT = config.get("entered_check_timeout", 10)

# Initialize handlers
capsolver = None
telegram = None

if config.get("capsolver_token") and config.get("capsolver_token") != "MASUKKAN_API_KEY_CAPSOLVER_DISINI":
    capsolver = CapsolverHandler(config.get("capsolver_token"))
    print("[INIT] Capsolver handler initialized")

if config.get("telegram_token") and config.get("chat_id"):
    telegram = TelegramNotifier(config.get("telegram_token"), config.get("chat_id"))
    print("[INIT] Telegram notifier initialized")

async def send_event(message: str):
    """Kirim event feed sederhana ke Telegram (jika tersedia)."""
    if telegram:
        try:
            await telegram.send_message(f"🔔 {message}")
        except Exception as e:
            print(f"[TELEGRAM] Error sending event: {e}")

# === Selector kunci ===
BTN_ACTIVE = 'button.tss-pqm623-content.active'
PRIZEBOX_ACTIVE = 'button:has(.tss-1msi2sy-prizeBox.active)'
JOIN_TEXT_ACTIVE = 'span.tss-7bx55w-rainStartedText.active'

# turnstile selectors
IFRAME_TURNSTILE = (
    'iframe[src*="challenges.cloudflare.com"], '
    'iframe[title*="Cloudflare"], '
    'iframe[title*="Turnstile"], '
    'iframe[title*="security challenge"]'
)
TURNSTILE_INPUT = 'input[name="cf-turnstile-response"]'

# Deteksi status khusus Turnstile untuk membedakan CRASH vs LOADING
async def is_turnstile_crashed(page) -> bool:
    """Heuristik crash khusus frame Turnstile/Cloudflare.
    Mengembalikan True jika frame Turnstile tidak bisa dievaluasi (context destroyed, frame detached, target closed).
    """
    try:
        for frame in page.frames:
            u = (frame.url or "").lower()
            if any(k in u for k in ["challenges.cloudflare.com", "turnstile", "cloudflare"]):
                try:
                    # Uji eksekusi sederhana pada frame
                    _ = await frame.evaluate("() => 42")
                except Exception as e:
                    msg = str(e).lower()
                    if any(k in msg for k in [
                        "execution context was destroyed",
                        "frame was detached",
                        "target closed",
                        "context destroyed",
                    ]):
                        print(f"[CF] Heuristik crash Turnstile: {e}")
                        return True
        return False
    except Exception:
        return False

async def is_turnstile_loading(page) -> bool:
    """Deteksi kondisi LOADING (bukan crash) pada widget Turnstile.
    True jika iframe ada dan indikator loading/disable terdeteksi, atau checkbox ada tapi disabled.
    """
    try:
        count = await page.locator(IFRAME_TURNSTILE).count()
        if count <= 0:
            return False
        fl = page.frame_locator(IFRAME_TURNSTILE)
        # Indikasi loading umum
        candidates = [
            'div[class*="spinner"]', '.spinner', '.loading', '[aria-busy="true"]',
            'div[role="progressbar"]', 'div[aria-live="polite"]'
        ]
        for sel in candidates:
            try:
                el = fl.locator(sel).first
                if await el.is_visible():
                    return True
            except Exception:
                pass
        # Checkbox ada namun disabled
        try:
            cb = fl.locator('input[type="checkbox"]').first
            if await cb.count() > 0:
                disabled = await cb.get_attribute('disabled')
                aria = await cb.get_attribute('aria-disabled')
                if disabled is not None or (aria and aria.lower() == 'true'):
                    return True
        except Exception:
            pass
        return False
    except Exception:
        return False

# Success/notification selectors - diperluas untuk deteksi yang lebih baik
SUCCESS_SELECTORS = [
    '.success-message',
    '.notification.success',
    '[class*="success"]',
    '.alert-success',
    '.toast-success',
    'div:has-text("Success")',
    'div:has-text("Joined")',
    'div:has-text("Entered")',
    'div:has-text("successfully")',
    'div:has-text("Successfully")',
    'span:has-text("successfully")',
    'span:has-text("Successfully")',
    '.tss-success',
    '[data-success="true"]',
    '.notification:has-text("success")',
    '.toast:has-text("success")',
    '[class*="notification"]:has-text("success")'
]

ENTERED_SELECTORS = [
    'div:has-text("Entered")',
    'span:has-text("Entered")',
    '.entered',
    '[data-status="entered"]',
    '.status-entered',
    'button:has-text("Entered")',
    '.tss-entered',
    'button:has(.entered)',
    '[class*="entered"]'
]

# Already joined selectors
ALREADY_JOINED_SELECTORS = [
    'div:has-text("already")',
    'span:has-text("already")',
    'div:has-text("Already")',
    'span:has-text("Already")',
    '.already-joined',
    '[class*="already"]',
    'div:has-text("sudah join")',
    'div:has-text("sudah bergabung")',
    '.notification:has-text("already")',
    '.toast:has-text("already")'
]

# Global variables untuk already joined cooldown
already_joined_until = 0.0
current_account = ""
last_random_activity = 0.0

def now(): return time.time()

def set_already_joined_cooldown():
    """Set cooldown 3 menit untuk already joined"""
    global already_joined_until
    already_joined_until = now() + 180  # 3 menit = 180 detik
    print(f"[ALREADY] Cooldown diset untuk 3 menit (sampai {time.strftime('%H:%M:%S', time.localtime(already_joined_until))})")

def is_already_joined_cooldown_active():
    """Cek apakah masih dalam cooldown already joined"""
    return now() < already_joined_until

def set_current_account(account_name):
    """Set akun yang sedang aktif"""
    global current_account
    current_account = account_name
    print(f"[ACCOUNT] Set akun aktif: {account_name}")

async def check_success_after_turnstile(page):
    """Cek apakah langsung sukses setelah klik checkbox Turnstile"""
    print("[SUCCESS_CHECK] Mengecek sukses setelah klik checkbox...")
    
    # Tunggu sebentar untuk loading
    await asyncio.sleep(2)
    
    try:
        # Cek di main page
        for selector in SUCCESS_SELECTORS:
            if await page.locator(selector).count() > 0:
                element = page.locator(selector).first
                if await element.is_visible():
                    text = await element.text_content()
                    print(f"[SUCCESS_CHECK] Sukses langsung terdeteksi: {text}")
                    return True
        
        # Cek di semua frame
        for frame in page.frames:
            try:
                for selector in SUCCESS_SELECTORS:
                    if await frame.locator(selector).count() > 0:
                        element = frame.locator(selector).first
                        if await element.is_visible():
                            text = await element.text_content()
                            print(f"[SUCCESS_CHECK] Sukses langsung terdeteksi di frame: {text}")
                            return True
            except Exception:
                continue
        
        # Cek keyword sukses
        success_keywords = ["successfully", "success", "joined", "entered", "complete", "berhasil"]
        for keyword in success_keywords:
            if await page.locator(f'text=/{keyword}/i').count() > 0:
                print(f"[SUCCESS_CHECK] Keyword sukses ditemukan: {keyword}")
                return True
                
            # Cek juga di frame
            for frame in page.frames:
                try:
                    if await frame.locator(f'text=/{keyword}/i').count() > 0:
                        print(f"[SUCCESS_CHECK] Keyword sukses ditemukan di frame: {keyword}")
                        return True
                except Exception:
                    continue
                    
    except Exception as e:
        print(f"[SUCCESS_CHECK] Error: {e}")
    
    print("[SUCCESS_CHECK] Tidak ada indikasi sukses langsung")
    return False

async def check_already_joined(page):
    """Cek apakah sudah join sebelumnya (already joined)"""
    print("[ALREADY_CHECK] Mengecek status already joined...")
    
    try:
        # Cek di main page
        for selector in ALREADY_JOINED_SELECTORS:
            if await page.locator(selector).count() > 0:
                element = page.locator(selector).first
                if await element.is_visible():
                    text = await element.text_content()
                    print(f"[ALREADY_CHECK] Already joined terdeteksi: {text}")
                    return True
        
        # Cek di semua frame
        for frame in page.frames:
            try:
                for selector in ALREADY_JOINED_SELECTORS:
                    if await frame.locator(selector).count() > 0:
                        element = frame.locator(selector).first
                        if await element.is_visible():
                            text = await element.text_content()
                            print(f"[ALREADY_CHECK] Already joined terdeteksi di frame: {text}")
                            return True
            except Exception:
                continue
        
        # Cek keyword already
        already_keywords = ["already", "sudah", "duplicate", "participated", "entered before"]
        for keyword in already_keywords:
            if await page.locator(f'text=/{keyword}/i').count() > 0:
                print(f"[ALREADY_CHECK] Keyword already ditemukan: {keyword}")
                return True
                
            # Cek juga di frame
            for frame in page.frames:
                try:
                    if await frame.locator(f'text=/{keyword}/i').count() > 0:
                        print(f"[ALREADY_CHECK] Keyword already ditemukan di frame: {keyword}")
                        return True
                except Exception:
                    continue
                    
    except Exception as e:
        print(f"[ALREADY_CHECK] Error: {e}")
    
    print("[ALREADY_CHECK] Tidak ada indikasi already joined")
    return False

async def check_logout_status(page):
    """Cek apakah user logout"""
    if not ASF_AVAILABLE:
        return False
        
    try:
        # Cek indikator logout di halaman
        logout_indicators = [
            'button:has-text("Login")',
            'a:has-text("Login")',
            'button:has-text("Sign In")',
            'a:has-text("Sign In")',
            '.login-button',
            '.signin-button'
        ]
        
        for indicator in logout_indicators:
            if await page.locator(indicator).count() > 0:
                print("[LOGOUT] Indikator logout ditemukan di halaman")
                return True
                
        return False
        
    except Exception as e:
        print(f"[LOGOUT] Error checking logout: {e}")
        return False

async def handle_auto_login():
    """Handle auto login menggunakan ASF"""
    if not ASF_AVAILABLE or not current_account:
        print("[LOGIN] ASF tidak tersedia atau akun tidak diset")
        return False
        
    try:
        # Implementasi auto login menggunakan ASF
        # Ini adalah placeholder - implementasi sebenarnya tergantung ASF
        print(f"[LOGIN] Mencoba auto-login untuk akun: {current_account}")
        
        # Simulasi proses login
        await asyncio.sleep(3)
        
        print("[LOGIN] Auto-login berhasil (simulasi)")
        return True
        
    except Exception as e:
        print(f"[LOGIN] Error auto-login: {e}")
        return False

async def validate_current_token():
    """Validasi token saat ini"""
    if not ASF_AVAILABLE:
        return
        
    try:
        # Validasi token menggunakan ASF
        print("[TOKEN] Validating current token...")
        # Implementasi validasi token
        pass
        
    except Exception as e:
        print(f"[TOKEN] Error validating: {e}")

async def periodic_balance_check():
    """Cek saldo Capsolver secara berkala"""
    if not capsolver:
        return
        
    try:
        # Cek saldo setiap beberapa loop
        balance = await capsolver.get_balance()
        if balance is not None and balance < 1.0:  # Warning jika saldo < $1
            print(f"[BALANCE] WARNING: Saldo Capsolver rendah: ${balance}")
            if telegram:
                await telegram.send_message(
                    f"⚠️ <b>SALDO CAPSOLVER RENDAH</b>\n\n"
                    f"💰 Saldo saat ini: ${balance}\n"
                    f"🔄 Silakan top up untuk melanjutkan auto-solve"
                )
                
    except Exception as e:
        print(f"[BALANCE] Error checking balance: {e}")

async def send_claim_success_notification(message):
    """Kirim notifikasi sukses klaim dengan info saldo"""
    if not telegram:
        return
        
    try:
        # Dapatkan info saldo jika tersedia
        balance_info = ""
        if capsolver:
            balance = await capsolver.get_balance()
            if balance is not None:
                balance_info = f"\n💰 Saldo Capsolver: ${balance}"
        
        # Dapatkan info akun jika tersedia
        account_info = ""
        if current_account:
            account_info = f"\n👤 Akun: {current_account}"
        
        full_message = (
            f"🎉 <b>SUKSES JOIN RAIN!</b>\n\n"
            f"✅ {message}\n"
            f"🌐 Website: {TARGET_URL}\n"
            f"⏰ Waktu: {time.strftime('%H:%M:%S', time.localtime())}"
            f"{account_info}"
            f"{balance_info}"
        )
        
        await telegram.send_message(full_message)
        
    except Exception as e:
        print(f"[NOTIFICATION] Error sending success notification: {e}")

#

async def check_page_crashed(page):
    """Cek apakah halaman crashed"""
    try:
        # Cek apakah page masih responsif
        await page.evaluate("() => document.title", timeout=5000)
        return False
    except Exception as e:
        print(f"[CRASH] Page crashed terdeteksi: {e}")
        return True

async def page_reload_if_needed(page, last_reload_ts):
    t = now()
    current_url = page.url
    
    # Cek apakah page crashed
    is_crashed = await check_page_crashed(page)
    
    # Cek berbagai kondisi yang memerlukan reload
    need_reload = (
        is_crashed or
        current_url.startswith("about:blank") or 
        current_url == "chrome://newtab/" or
        current_url == "" or
        "chrome-error://" in current_url or
        (t - last_reload_ts > RELOAD_EVERY_SEC)
    )
    
    if need_reload:
        if is_crashed:
            print("[RELOAD] Page crashed terdeteksi, TAPI reload saat crash DINONAKTIFKAN")
            # Tidak melakukan reload saat page crash, langsung return
            return last_reload_ts
        else:
            print(f"[RELOAD] Halaman perlu di-reload. URL saat ini: {current_url}")
            await send_event("Reload halaman dipicu")
        
        # Retry mechanism untuk reload
        max_retries = 3
        for retry in range(max_retries):
            try:
                print(f"[RELOAD] Percobaan reload #{retry + 1}")
                await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
                print(f"[LOAD] {TARGET_URL} berhasil dimuat")
                
                # Tunggu sebentar untuk memastikan halaman fully loaded
                await asyncio.sleep(3)
                
                # Verifikasi halaman berhasil dimuat
                final_url = page.url
                if final_url.startswith("about:blank") or "chrome-error://" in final_url:
                    print("[RELOAD] Halaman masih blank setelah reload, coba lagi...")
                    await asyncio.sleep(5)
                    await page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(2)
                
                # Test apakah page responsif
                if not await check_page_crashed(page):
                    print("[RELOAD] Reload berhasil, page responsif")
                    return now()
                else:
                    print(f"[RELOAD] Page masih crashed setelah reload #{retry + 1}")
                    if retry < max_retries - 1:
                        await asyncio.sleep(5)
                        continue
                
            except Exception as e:
                print(f"[RELOAD] Error saat reload #{retry + 1}: {e}")
                if retry < max_retries - 1:
                    print(f"[RELOAD] Mencoba reload ulang dalam 10 detik...")
                    await asyncio.sleep(10)
                    continue
                else:
                    print("[RELOAD] Semua percobaan reload gagal")
                    return last_reload_ts
        
        return now()
    
    return last_reload_ts

async def detect_active(page):
    """Balikin selector tombol yang valid kalau 'active' terdeteksi; else None."""
    
    # Cek apakah halaman sudah dimuat dengan benar
    current_url = page.url
    if current_url.startswith("about:blank") or "chrome-error://" in current_url or current_url == "":
        print(f"[ACTIVE] Halaman belum dimuat dengan benar: {current_url}")
        return None
    
    # Cek apakah halaman flip.gg sudah dimuat
    if "flip.gg" not in current_url:
        print(f"[ACTIVE] Bukan halaman flip.gg: {current_url}")
        return None
    
    try:
        if await page.locator(PRIZEBOX_ACTIVE).count() > 0:
            await page.locator(PRIZEBOX_ACTIVE).first.wait_for(state="visible", timeout=1500)
            print("[ACTIVE] prizeBox.active ditemukan.")
            return PRIZEBOX_ACTIVE
    except PWTimeout:
        pass
    except Exception as e:
        print("[DETECT] error PRIZEBOX_ACTIVE:", e)

    try:
        if await page.locator(BTN_ACTIVE).count() > 0:
            await page.locator(BTN_ACTIVE).first.wait_for(state="visible", timeout=1500)
            print("[ACTIVE] button.tss-pqm623-content.active ditemukan.")
            return BTN_ACTIVE
    except PWTimeout:
        pass
    except Exception as e:
        print("[DETECT] error BTN_ACTIVE:", e)

    try:
        loc = page.locator(f'button:has({JOIN_TEXT_ACTIVE})').first
        if await loc.count() > 0:
            await loc.wait_for(state="visible", timeout=1500)
            print("[ACTIVE] span Join now.active dalam button ditemukan.")
            return f'button:has({JOIN_TEXT_ACTIVE})'
    except PWTimeout:
        pass
    except Exception as e:
        print("[DETECT] error JOIN_TEXT_ACTIVE:", e)

    print("[ACTIVE] Tidak ada state active.")
    return None

async def click_join(page, btn_selector):
    """Klik tombol berdasarkan selector yang diberikan."""
    try:
        btn = page.locator(btn_selector).first
        await btn.scroll_into_view_if_needed()
        await btn.click()
        print(f"[CLICK] Klik tombol: {btn_selector}")
        return True
    except Exception as e:
        print(f"[CLICK] Gagal klik {btn_selector} → {e}")
        return False

async def detect_success_notification(page, timeout_sec=15):
    """Deteksi apakah ada notifikasi sukses setelah join - cek di semua frame"""
    print("[SUCCESS] Mengecek notifikasi sukses di semua frame...")
    
    for i in range(timeout_sec):
        try:
            # Cek di main page
            for selector in SUCCESS_SELECTORS:
                if await page.locator(selector).count() > 0:
                    element = page.locator(selector).first
                    if await element.is_visible():
                        text = await element.text_content()
                        print(f"[SUCCESS] Notifikasi sukses ditemukan di main page: {text}")
                        return True
            
            # Cek di semua frame/iframe
            for frame in page.frames:
                try:
                    for selector in SUCCESS_SELECTORS:
                        if await frame.locator(selector).count() > 0:
                            element = frame.locator(selector).first
                            if await element.is_visible():
                                text = await element.text_content()
                                print(f"[SUCCESS] Notifikasi sukses ditemukan di frame {frame.url}: {text}")
                                return True
                except Exception:
                    continue
            
            # Check for any text containing success keywords di main page
            success_keywords = ["successfully", "success", "joined", "entered", "complete", "done", "berhasil"]
            for keyword in success_keywords:
                if await page.locator(f'text=/{keyword}/i').count() > 0:
                    print(f"[SUCCESS] Keyword sukses ditemukan di main page: {keyword}")
                    return True
                    
                # Cek juga di semua frame
                for frame in page.frames:
                    try:
                        if await frame.locator(f'text=/{keyword}/i').count() > 0:
                            print(f"[SUCCESS] Keyword sukses ditemukan di frame {frame.url}: {keyword}")
                            return True
                    except Exception:
                        continue
                    
        except Exception as e:
            print(f"[SUCCESS] Error checking success: {e}")
        
        await asyncio.sleep(1)
    
    print("[SUCCESS] Tidak ada notifikasi sukses ditemukan")
    return False

async def detect_entered_status(page, timeout_sec=10):
    """Deteksi apakah status sudah 'Entered'"""
    print("[ENTERED] Mengecek status entered...")
    
    for i in range(timeout_sec):
        try:
            for selector in ENTERED_SELECTORS:
                if await page.locator(selector).count() > 0:
                    element = page.locator(selector).first
                    if await element.is_visible():
                        text = await element.text_content()
                        print(f"[ENTERED] Status entered ditemukan: {text}")
                        return True
                        
        except Exception as e:
            print(f"[ENTERED] Error checking entered: {e}")
        
        await asyncio.sleep(1)
    
    print("[ENTERED] Status entered tidak ditemukan")
    return False

async def extract_turnstile_info(page):
    """Extract website key dan URL untuk Turnstile dengan pencarian yang lebih komprehensif"""
    try:
        website_url = page.url
        sitekey = None
        action = ""
        cdata = ""
        
        print("[TURNSTILE] Mencari sitekey dan metadata di semua frame dan elemen...")
        
        # 1. Cek di iframe Turnstile tradisional
        iframe_count = await page.locator(IFRAME_TURNSTILE).count()
        if iframe_count > 0:
            iframe_src = await page.locator(IFRAME_TURNSTILE).first.get_attribute('src')
            if iframe_src:
                sitekey_match = re.search(r'sitekey=([^&]+)', iframe_src)
                if sitekey_match:
                    sitekey = sitekey_match.group(1)
                    print(f"[TURNSTILE] Sitekey ditemukan di iframe src: {sitekey}")
        
        # 2. Cek di elemen dengan data-sitekey di main page
        if not sitekey:
            sitekey_selectors = [
                '[data-sitekey]',
                '[data-site-key]',
                '#cf-turnstile[data-sitekey]',
                '.cf-turnstile[data-sitekey]',
                'div[data-sitekey]',
                'iframe[data-sitekey]',
                '.cf-turnstile',
                '#cf-turnstile'
            ]
            
            for selector in sitekey_selectors:
                try:
                    if await page.locator(selector).count() > 0:
                        element = page.locator(selector).first
                        sitekey = await element.get_attribute('data-sitekey') or await element.get_attribute('data-site-key')
                        
                        # Ambil metadata tambahan jika ada
                        if not action:
                            action = await element.get_attribute('data-action') or ""
                        if not cdata:
                            cdata = await element.get_attribute('data-cdata') or ""
                            
                        if sitekey:
                            print(f"[TURNSTILE] Sitekey ditemukan di main page: {sitekey}")
                            if action:
                                print(f"[TURNSTILE] Action ditemukan: {action}")
                            if cdata:
                                print(f"[TURNSTILE] CData ditemukan: {cdata}")
                            break
                except Exception:
                    continue
        
        # 3. Cek di semua frame
        if not sitekey:
            for frame in page.frames:
                try:
                    frame_url = frame.url or ""
                    print(f"[TURNSTILE] Mengecek sitekey di frame: {frame_url}")
                    
                    # Cek di elemen dengan data-sitekey di frame
                    for selector in sitekey_selectors:
                        try:
                            if await frame.locator(selector).count() > 0:
                                element = frame.locator(selector).first
                                sitekey = await element.get_attribute('data-sitekey') or await element.get_attribute('data-site-key')
                                
                                # Ambil metadata tambahan jika ada
                                if not action:
                                    action = await element.get_attribute('data-action') or ""
                                if not cdata:
                                    cdata = await element.get_attribute('data-cdata') or ""
                                    
                                if sitekey:
                                    print(f"[TURNSTILE] Sitekey ditemukan di frame {frame_url}: {sitekey}")
                                    break
                        except Exception:
                            continue
                    
                    if sitekey:
                        break
                        
                except Exception:
                    continue
        
        # 4. Cek di JavaScript/script tags
        if not sitekey:
            print("[TURNSTILE] Mencari sitekey di script tags...")
            try:
                scripts = await page.locator('script').all()
                for script in scripts:
                    try:
                        script_content = await script.text_content()
                        if script_content and 'turnstile' in script_content.lower():
                            # Pattern untuk mencari sitekey
                            patterns = [
                                r'sitekey["\']?\s*:\s*["\']([^"\']+)["\']',
                                r'data-sitekey["\']?\s*=\s*["\']([^"\']+)["\']',
                                r'websiteKey["\']?\s*:\s*["\']([^"\']+)["\']',
                                r'site_key["\']?\s*:\s*["\']([^"\']+)["\']'
                            ]
                            
                            for pattern in patterns:
                                match = re.search(pattern, script_content, re.IGNORECASE)
                                if match:
                                    sitekey = match.group(1)
                                    print(f"[TURNSTILE] Sitekey ditemukan di script: {sitekey}")
                                    break
                            
                            # Cari action dan cdata juga
                            if not action:
                                action_match = re.search(r'action["\']?\s*:\s*["\']([^"\']+)["\']', script_content, re.IGNORECASE)
                                if action_match:
                                    action = action_match.group(1)
                                    print(f"[TURNSTILE] Action ditemukan di script: {action}")
                            
                            if not cdata:
                                cdata_match = re.search(r'cdata["\']?\s*:\s*["\']([^"\']+)["\']', script_content, re.IGNORECASE)
                                if cdata_match:
                                    cdata = cdata_match.group(1)
                                    print(f"[TURNSTILE] CData ditemukan di script: {cdata}")
                            
                            if sitekey:
                                break
                    except Exception:
                        continue
            except Exception as e:
                print(f"[TURNSTILE] Error checking scripts: {e}")
        
        # 5. Fallback: gunakan sitekey umum jika tidak ditemukan
        if not sitekey:
            print("[TURNSTILE] Sitekey tidak ditemukan, menggunakan sitekey default")
            # Sitekey umum untuk testing/demo Cloudflare Turnstile
            sitekey = "0x4AAAAAAADnPIDROlWd_wc"
        
        print(f"[TURNSTILE] Website: {website_url}")
        print(f"[TURNSTILE] Sitekey: {sitekey}")
        print(f"[TURNSTILE] Action: {action}")
        print(f"[TURNSTILE] CData: {cdata}")
        
        return website_url, sitekey, action, cdata
        
    except Exception as e:
        print(f"[TURNSTILE] Error extracting info: {e}")
        # Return fallback values
        return page.url, "0x4AAAAAAADnPIDROlWd_wc", "", ""

async def click_turnstile_checkbox(page):
    """Klik checkbox di iframe Turnstile dengan pencarian yang lebih komprehensif"""
    print("[TURNSTILE] Mencari dan mengklik checkbox Turnstile di semua frame...")
    
    # Selectors untuk checkbox Turnstile yang lebih lengkap
    checkbox_selectors = [
        'input[type="checkbox"]',
        '[role="checkbox"]', 
        'label:has([type="checkbox"])',
        'div[role="button"][tabindex]',
        'div[aria-checked]',
        '.cb-lb input[type="checkbox"]',  # Selector khusus dari inspect
        'label.cb-lb',                    # Klik label yang membungkus checkbox
        'label.cb-lb input',
        '#wNUym6 input[type="checkbox"]',
        '.cb-c input[type="checkbox"]',
        'input[type="checkbox"][class*="cb"]',
        'label[class*="cb"] input[type="checkbox"]'
    ]
    
    # 1. Prioritaskan cek di iframe Turnstile tradisional, karena ini skenario paling umum
    print("[TURNSTILE] Mengecek iframe Turnstile tradisional (prioritas)...")
    try:
        # Tunggu iframe muncul dengan timeout yang cukup
        await page.wait_for_selector(IFRAME_TURNSTILE, timeout=15_000)
        fl = page.frame_locator(IFRAME_TURNSTILE)
        
        for selector in checkbox_selectors:
            try:
                el = fl.locator(selector).first
                print(f"[TURNSTILE] Menunggu checkbox '{selector}' di iframe menjadi visible...")
                await el.wait_for(state="visible", timeout=10_000) # Tunggu hingga 10 detik
                await el.click(force=True, timeout=5000)
                print(f"[TURNSTILE] Checkbox berhasil diklik di iframe Turnstile: {selector}")
                return True
            except Exception:
                continue
    except PWTimeout:
        print("[TURNSTILE] Iframe Turnstile tradisional tidak ditemukan dalam 15 detik.")
    
    # 1. Cek di main page dulu (mungkin tidak di iframe)
    print("[TURNSTILE] Mengecek checkbox di main page...")
    for selector in checkbox_selectors:
        try:
            if await page.locator(selector).count() > 0:
                element = page.locator(selector).first
                await element.wait_for(state="visible", timeout=2000)
                
                # Cek apakah ini checkbox yang benar dengan melihat teks di sekitarnya
                try:
                    parent = element.locator('xpath=..')
                    parent_text = await parent.text_content()
                    if parent_text and ("verify" in parent_text.lower() or "human" in parent_text.lower()):
                        print(f"[TURNSTILE] Checkbox 'Verify you are human' ditemukan di main page: {selector}")
                        await element.scroll_into_view_if_needed()
                        await element.click(force=True)
                        print("[TURNSTILE] Checkbox berhasil diklik di main page!")
                        return True
                except Exception:
                    # Jika tidak bisa cek parent text, coba klik saja
                    print(f"[TURNSTILE] Mencoba klik checkbox di main page: {selector}")
                    await element.scroll_into_view_if_needed()
                    await element.click(force=True)
                    print("[TURNSTILE] Checkbox diklik di main page!")
                    return True
        except Exception as e:
            continue
    
    # 3. Cek di SEMUA frame/iframe yang ada
    print("[TURNSTILE] Mengecek di semua frame yang tersedia...")
    for frame in page.frames:
        try:
            frame_url = frame.url or ""
            print(f"[TURNSTILE] Mengecek frame: {frame_url}")
            
            # Cek semua selector di frame ini
            for selector in checkbox_selectors:
                try:
                    if await frame.locator(selector).count() > 0:
                        element = frame.locator(selector).first
                        await element.wait_for(state="visible", timeout=2000)
                        
                        # Cek apakah ini checkbox yang benar
                        try:
                            parent = element.locator('xpath=..')
                            parent_text = await parent.text_content()
                            if parent_text and ("verify" in parent_text.lower() or "human" in parent_text.lower()):
                                print(f"[TURNSTILE] Checkbox 'Verify you are human' ditemukan di frame {frame_url}: {selector}")
                                await element.click(force=True)
                                print(f"[TURNSTILE] Checkbox berhasil diklik di frame!")
                                return True
                        except Exception:
                            pass
                        
                        # Jika frame mengandung cloudflare/turnstile, langsung coba klik
                        if any(keyword in frame_url.lower() for keyword in ["cloudflare", "turnstile", "challenges"]):
                            print(f"[TURNSTILE] Frame Cloudflare/Turnstile terdeteksi, klik checkbox: {selector}")
                            await element.click(force=True)
                            print(f"[TURNSTILE] Checkbox diklik di frame Cloudflare!")
                            return True
                        
                        # Untuk frame lain, coba klik jika selector cocok dengan pattern Turnstile
                        if any(pattern in selector for pattern in ["cb-", "checkbox"]):
                            print(f"[TURNSTILE] Pattern Turnstile terdeteksi di frame, klik checkbox: {selector}")
                            await element.click(force=True)
                            print(f"[TURNSTILE] Checkbox diklik di frame!")
                            return True
                            
                except Exception as e:
                    continue
                    
        except Exception as e:
            continue
    
    # 4. Fallback: cari berdasarkan text content
    print("[TURNSTILE] Fallback: mencari berdasarkan text 'Verify you are human'...")
    try:
        # Cek di main page
        verify_elements = await page.locator('text=/verify.*human/i').all()
        for element in verify_elements:
            try:
                # Cari checkbox di dalam atau dekat element ini
                checkbox = element.locator('input[type="checkbox"]').first
                if await checkbox.count() > 0:
                    await checkbox.click(force=True, timeout=5000)
                    print("[TURNSTILE] Checkbox ditemukan via text search di main page!")
                    return True
                    
                # Cari di parent
                parent_checkbox = element.locator('xpath=..//*[@type="checkbox"]').first
                if await parent_checkbox.count() > 0:
                    await parent_checkbox.click(force=True, timeout=5000)
                    print("[TURNSTILE] Checkbox ditemukan via parent text search di main page!")
                    return True
            except Exception:
                continue
        
        # Cek di semua frame
        for frame in page.frames:
            try:
                verify_elements = await frame.locator('text=/verify.*human/i').all()
                for element in verify_elements:
                    try:
                        checkbox = element.locator('input[type="checkbox"]').first
                        if await checkbox.count() > 0:
                            await checkbox.click(force=True, timeout=5000)
                            print(f"[TURNSTILE] Checkbox ditemukan via text search di frame {frame.url}!")
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
                
    except Exception as e:
        print(f"[TURNSTILE] Error dalam text search: {e}")
    
    print("[TURNSTILE] Checkbox tidak ditemukan di manapun")
    return False

async def wait_turnstile_token(page, timeout_ms):
    """Tunggu token Turnstile terisi (jika elemen ada)."""
    print("[TURNSTILE] Menunggu token Turnstile…")
    try:
        await page.wait_for_selector(TURNSTILE_INPUT, timeout=10_000)
    except PWTimeout:
        print("[TS] Input token tidak tampil. Lanjut saja.")
        return None

    for i in range(timeout_ms // 1000):
        val = await page.evaluate(
            f'''() => {{
                const el = document.querySelector('{TURNSTILE_INPUT}');
                return el && el.value ? el.value : null;
            }}'''
        )
        if val:
            print("[TURNSTILE] Token terdeteksi 🥳")
            return val
        if i % 5 == 0:
            print(f"[TURNSTILE] …menunggu token ({i}s)")
        await asyncio.sleep(1)
    print("[TURNSTILE] Timeout menunggu token.")
    return None

async def inject_turnstile_token(page, token):
    """Inject token Turnstile ke dalam input field"""
    try:
        # Inject token ke input field
        await page.evaluate(f'''() => {{
            const input = document.querySelector('{TURNSTILE_INPUT}');
            if (input) {{
                input.value = '{token}';
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                console.log('Token injected successfully');
                return true;
            }}
            return false;
        }}''')
        
        print("[INJECT] Token berhasil diinjeksi")
        return True
        
    except Exception as e:
        print(f"[INJECT] Error injecting token: {e}")
        return False

async def handle_turnstile_challenge(page):
    """Handle Turnstile challenge dengan implementasi yang benar berdasarkan dokumentasi CapSolver"""
    print("[TURNSTILE] Memulai alur penanganan Turnstile...")

    # LANGKAH 1: Tunggu iframe Turnstile muncul.
    try:
        print("[TURNSTILE] Menunggu iframe 'challenges.cloudflare.com'...")
        await page.wait_for_selector(IFRAME_TURNSTILE, timeout=30_000)
        print("[TURNSTILE] Iframe Turnstile terdeteksi.")
        await send_event("Turnstile terdeteksi, memulai penyelesaian")
    except PWTimeout:
        print("[TURNSTILE] Tidak ada iframe Turnstile yang muncul setelah klik join. Cek hasil langsung.")
        # Jika tidak ada iframe, mungkin tidak ada captcha sama sekali.
        success_found = await detect_success_notification(page, 5)
        if success_found:
            await send_event("Turnstile selesai otomatis (tanpa solver)")
            return "instant_success"
        if await check_already_joined(page):
            set_already_joined_cooldown()
            await send_event("Already joined terdeteksi, cooldown 3 menit dimulai")
            return "already_joined"
        return "no_turnstile"

    # LANGKAH 2: Beralih ke iframe dan klik checkbox.
    print("[TURNSTILE] Mencoba klik checkbox di dalam iframe/widget...")
    checkbox_clicked = await click_turnstile_checkbox(page)

    if not checkbox_clicked:
        print("[TURNSTILE] Gagal mengklik checkbox. Mungkin challenge tidak interaktif.")
        # Lanjut ke Capsolver jika klik gagal, karena bisa jadi invisible turnstile
    else:
        print("[TURNSTILE] Checkbox berhasil diklik. Menunggu hasil otomatis...")

        # --- LOGIKA BARU: Cek checkbox kedua ---
        print("[TURNSTILE] Mengecek kemungkinan munculnya checkbox kedua...")
        await asyncio.sleep(1) # Beri waktu sesaat untuk checkbox kedua muncul
        try:
            fl = page.frame_locator(IFRAME_TURNSTILE)
            # Selector untuk checkbox kedua yang mungkin muncul
            second_checkbox_selector = 'input[type="checkbox"]'
            
            if await fl.locator(second_checkbox_selector).count() > 0:
                second_checkbox = fl.locator(second_checkbox_selector).first
                if await second_checkbox.is_visible():
                    # Cek apakah checkbox sedang loading (misal, ada spinner di dekatnya)
                    # Ini adalah contoh, selector spinner mungkin perlu disesuaikan
                    spinner_selector = 'div[class*="spinner"]'
                    is_loading = await fl.locator(spinner_selector).is_visible()

                    if not is_loading:
                        print("[TURNSTILE] Checkbox kedua terdeteksi dan tidak loading. Mengklik lagi...")
                        await second_checkbox.click(force=True, timeout=3000)
                        print("[TURNSTILE] Checkbox kedua berhasil diklik.")
                    else:
                        print("[TURNSTILE] Checkbox kedua terdeteksi tapi sedang loading, tidak diklik.")
        except Exception as e:
            print(f"[TURNSTILE] Tidak ada checkbox kedua atau error saat mengecek: {e}")
        # --- AKHIR LOGIKA BARU ---

        # JEDA 3 DETIK SETELAH KLIK CHECKBOX SEBELUM MELANJUTKAN
        print("[TURNSTILE] Menunggu 3 detik setelah klik checkbox...")
        await asyncio.sleep(3)
        token = await wait_turnstile_token(page, 5000) # Tunggu 5 detik
        if token:
            print("[TURNSTILE] Token terdeteksi setelah klik checkbox! Challenge selesai.")
            # Cek notifikasi sukses
            if await detect_success_notification(page, 10):
                await send_event("Turnstile selesai otomatis (instan)")
                return "instant_success"
            # Cek already joined
            if await check_already_joined(page):
                set_already_joined_cooldown()
                await send_event("Already joined terdeteksi, cooldown 3 menit dimulai")
                return "already_joined"
            # Jika tidak ada notifikasi, anggap sukses dan biarkan alur utama melanjutkan
            await send_event("Turnstile selesai otomatis (token terdeteksi)")
            return "manual_success"

        print("[TURNSTILE] Token tidak terdeteksi setelah klik. Puzzle mungkin diperlukan.")

    # LANGKAH 3: Jika klik tidak cukup, ekstrak info dan gunakan Capsolver.
    print("[TURNSTILE] Mengekstrak informasi untuk Capsolver...")
    website_url, sitekey, action, cdata = await extract_turnstile_info(page)

    if not sitekey or sitekey == "0x4AAAAAAADnPIDROlWd_wc":
        print("[TURNSTILE] Sitekey tidak valid atau tidak ditemukan. Tidak bisa menggunakan Capsolver.")
        await send_event("Turnstile gagal: sitekey tidak valid/tdk ditemukan")
        return "failed"

    if capsolver and AUTO_SOLVE_CAPTCHA:
        print("[TURNSTILE] Menggunakan CapSolver untuk menyelesaikan Turnstile...")
        solved_token = await capsolver.solve_turnstile(
            website_url=website_url,
            website_key=sitekey,
            action=action,
            cdata=cdata
        )

        if solved_token:
            print("[TURNSTILE] Token berhasil didapat dari CapSolver!")
            token_injected = await inject_turnstile_token(page, solved_token)

            if token_injected:
                print("[TURNSTILE] Token berhasil diinjeksi ke halaman!")
                await page.evaluate(f'''() => {{
                    // Update semua response fields yang mungkin ada
                    const responseFields = document.querySelectorAll('input[name*="turnstile"], input[name*="cf-turnstile-response"]');
                    responseFields.forEach(field => {{
                        field.value = '{solved_token}';
                        field.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        field.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }});

                    // Trigger Turnstile callback jika ada
                    if (window.turnstile && window.turnstile.render) {{
                        console.log('Turnstile callback triggered');
                    }}
                    
                    // Update checkbox visual jika ada
                    const checkbox = document.querySelector('.cb-lb input[type="checkbox"]');
                    if (checkbox && !checkbox.checked) {{
                        checkbox.checked = true;
                        checkbox.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}

                    // Update UI state
                    const initialDiv = document.getElementById('wNUym6');
                    const successDiv = document.getElementById('success');
                    if (initialDiv) initialDiv.style.display = 'none';
                    if (successDiv) {{
                        successDiv.style.display = 'block';
                        successDiv.style.visibility = 'visible';
                    }}

                    // Dispatch custom event
                    window.dispatchEvent(new CustomEvent('turnstile-solved', {{
                        detail: {{ token: '{solved_token}' }}
                    }}));

                    console.log('Turnstile token injected and UI updated');
                }}''')

                await asyncio.sleep(2)
                print("[TURNSTILE] Mengecek hasil setelah inject token...")

                success_found = await detect_success_notification(page, 15)
                if success_found:
                    print("[TURNSTILE] Sukses terdeteksi setelah CapSolver!")
                    if telegram:
                        try:
                            await telegram.send_message(
                                f"🎯 <b>CAPTCHA SOLVED!</b>\n\n"
                                f"✅ CapSolver berhasil menyelesaikan Turnstile\n"
                                f"🌐 Website: {website_url}\n"
                                f"🔑 Sitekey: {sitekey[:20]}...\n"
                                f"⏰ Waktu: {time.strftime('%H:%M:%S', time.localtime())}"
                            )
                        except Exception as e:
                            print(f"[TELEGRAM] Error sending notification: {e}")

                    return "capsolver_success"

                already_joined = await check_already_joined(page)
                if already_joined:
                    print("[TURNSTILE] Already joined terdeteksi setelah CapSolver")
                    set_already_joined_cooldown()
                    if telegram:
                        try:
                            await telegram.send_message(
                                "ℹ️ <b>ALREADY JOINED (Post-CapSolver)</b>\n\n"
                                "🔄 Sudah join rain sebelumnya\n"
                                "⏰ Skip monitoring selama 3 menit"
                            )
                        except Exception as e:
                            print(f"[TELEGRAM] Error sending notification: {e}")
                    await send_event("Already joined terdeteksi, cooldown 3 menit dimulai")

                    return "already_joined"

                print("[TURNSTILE] Token diinjeksi tapi tidak ada feedback yang jelas")
                return "capsolver_success"  # Anggap berhasil karena token sudah diinjeksi

            else:
                print("[TURNSTILE] Gagal inject token dari CapSolver")
                return "failed"
        else:
            print("[TURNSTILE] CapSolver gagal menyelesaikan challenge")
            await send_event("Turnstile gagal: CapSolver gagal menyelesaikan challenge")
            return "failed"

    # Fallback jika Capsolver tidak aktif atau gagal
    print("[TURNSTILE] Capsolver tidak tersedia/gagal. Menunggu penyelesaian manual jika memungkinkan.")
    token = await wait_turnstile_token(page, TURNSTILE_WAIT_MS)
    if token:
        print("[TURNSTILE] Token terdeteksi (kemungkinan dari penyelesaian manual).")
        return "manual_success"

    print("[TURNSTILE] Semua metode penanganan Turnstile gagal.")
    await send_event("Turnstile gagal diselesaikan")
    return "failed"

async def main():
    async with async_playwright() as p:
        print("[BOOT] Connect CDP:", CDP_URL)
        
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            print("[BOOT] CDP connection berhasil")
        except Exception as e:
            print(f"[BOOT] Error connecting to CDP: {e}")
            print("[BOOT] Pastikan Chrome berjalan dengan --remote-debugging-port=9222")
            return

        try:
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            print("[BOOT] Page berhasil dibuat")
        except Exception as e:
            print(f"[BOOT] Error creating page: {e}")
            return

        # Mode fast-execute: langsung eksekusi tanpa loop monitoring panjang
        if FAST_EXECUTE:
            print("[FAST] Mode fast_execute aktif. Menjalankan eksekusi cepat.")

            async def detect_text_in_flip_frames(text: str, timeout_sec: int) -> bool:
                """Cari teks pada main page dan frames domain flip.gg (exclude cf/turnstile). Partial match, case-insensitive."""
                deadline = time.time() + timeout_sec
                while time.time() < deadline:
                    try:
                        # main page
                        for sel in [f"text=/{re.escape(text)}/i", f"span:has-text('{text}')", f"div:has-text('{text}')"]:
                            try:
                                if await page.locator(sel).count() > 0:
                                    el = page.locator(sel).first
                                    if await el.is_visible():
                                        return True
                            except Exception:
                                pass
                        # frames flip.gg (kecuali cf/turnstile)
                        for fr in page.frames:
                            u = (fr.url or '').lower()
                            if not u or 'flip.gg' not in u:
                                continue
                            if any(k in u for k in ['cloudflare', 'turnstile', 'challenges.cloudflare.com']):
                                continue
                            for sel in [f"text=/{re.escape(text)}/i", f"span:has-text('{text}')", f"div:has-text('{text}')"]:
                                try:
                                    if await fr.locator(sel).count() > 0:
                                        el = fr.locator(sel).first
                                        if await el.is_visible():
                                            return True
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
                return False

            async def click_rain_once() -> bool:
                # Coba deteksi active standar dulu
                sel = await detect_active(page)
                if sel:
                    return await click_join(page, sel)
                # Fallback: coba selector berbasis teks umum
                fallback_selectors = [
                    "button:has-text('Join now')",
                    "button:has-text('Join')",
                    "button:has-text('Rain')",
                    "button:has-text('Enter')",
                ]
                for fs in fallback_selectors:
                    try:
                        if await page.locator(fs).count() > 0:
                            btn = page.locator(fs).first
                            try:
                                await btn.scroll_into_view_if_needed()
                            except Exception:
                                pass
                            try:
                                await btn.click()
                                print(f"[CLICK] Klik fallback tombol: {fs}")
                                return True
                            except Exception as e:
                                print(f"[CLICK] Gagal klik fallback {fs}: {e}")
                                continue
                    except Exception:
                        continue
                return False

            # buka target (+ wait & polling), fallback ke homepage jika perlu
            async def try_click_with_wait(total_wait: int = 30) -> bool:
                deadline = time.time() + total_wait
                scroll_y = 0
                while time.time() < deadline:
                    if await click_rain_once():
                        return True
                    # Scroll ringan untuk memicu lazy load/visibility
                    try:
                        scroll_y += 400
                        await page.evaluate("y => { window.scrollBy(0, y); }", 400)
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                return False

            try:
                await page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2)
                # Tunggu 9 detik setelah load selesai sebelum klik Rain
                await asyncio.sleep(9)
            except Exception as e:
                print(f"[FAST] Gagal buka target: {e}")

            # Mulai watcher notifikasi global (exclude frame CF)
            async def notification_watchdog(page):
                """Pantau notifikasi success/already di semua frame flip.gg (exclude cf). Return 'success' atau 'already'."""
                targets = [('success', 'Successfully joined rain!'), ('already', 'You have already entered this rain!')]
                while True:
                    for label, text in targets:
                        try:
                            if await detect_text_in_flip_frames(text, 1):
                                return label
                        except Exception:
                            pass
                    await asyncio.sleep(0.3)

            notif_task = asyncio.create_task(notification_watchdog(page))

            ok = await try_click_with_wait(30)
            if not ok:
                # reload sekali sebelum fallback
                try:
                    await page.reload(wait_until="networkidle")
                    await asyncio.sleep(2)
                    ok = await try_click_with_wait(15)
                except Exception:
                    pass
            if not ok:
                # fallback: coba di homepage
                try:
                    await page.goto("https://flip.gg/", wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(2)
                    ok = await try_click_with_wait(30)
                except Exception as e:
                    print(f"[FAST] Gagal buka homepage: {e}")
                    ok = False

            if not ok:
                print("[FAST] Tidak ada tombol Rain yang bisa diklik.")
                _save_fast_result('failed')
                return

            # Alur baru sesuai SOP + diferensiasi CRASH vs LOADING vs CapSolver
            success_after_checkbox = False
            crashed_prev = False
            for attempt in range(1, 4):
                print(f"[FAST] Attempt {attempt}/3: klik Join Rain + tangani Turnstile")

                # Cek apakah notifikasi global sudah terdeteksi
                if 'notif_task' in locals() and notif_task and notif_task.done():
                    label = None
                    try:
                        label = notif_task.result()
                    except Exception:
                        label = None
                    if label:
                        if telegram:
                            try:
                                if label == 'success':
                                    await telegram.send_message("🎉 <b>SUKSES JOIN RAIN!</b>\n\nKonfirmasi: <i>Successfully joined rain!</i>")
                                else:
                                    await telegram.send_message("ℹ️ <b>ALREADY JOINED</b>\n\nYou have already entered this rain!")
                            except Exception:
                                pass
                        # Diam 1 menit sebelum menutup GoLogin (exit bot)
                        await asyncio.sleep(60)
                        _save_fast_result('success')
                        return

                # Reload hanya jika attempt sebelumnya terdeteksi CRASH Turnstile
                if attempt > 1 and crashed_prev:
                    print("[FAST] Attempt sebelumnya CRASH Turnstile → reload Flip sebelum lanjut")
                    try:
                        await page.goto(TARGET_URL, wait_until='networkidle', timeout=30000)
                        await asyncio.sleep(2)
                    except Exception as e:
                        print(f"[FAST] Reload gagal: {e}")
                        crashed_prev = True
                        continue

                # STEP 1: Klik Join Rain (selalu di SETIAP attempt)
                try_wait = 10 if attempt > 1 else 15
                if not await try_click_with_wait(try_wait):
                    print("[FAST] Tombol Rain tidak ditemukan pada attempt ini.")
                    crashed_prev = await is_turnstile_crashed(page) or await check_page_crashed(page)
                    continue

                # STEP 2: Tangani Turnstile secara komprehensif (klik checkbox / suntik CapSolver)
                ts_result = await handle_turnstile_challenge(page)

                if ts_result in ("instant_success", "manual_success", "capsolver_success"):
                    # Sukses dari jalur Turnstile (manual/invisible/CapSolver)
                    success_after_checkbox = True
                    break
                elif ts_result == "already_joined":
                    # Deteksi ALREADY: kirim notif, diam 1 menit, lalu exit (wrapper akan stop GoLogin)
                    if telegram:
                        try:
                            await telegram.send_message(
                                "ℹ️ <b>ALREADY JOINED</b>\n\nYou have already entered this rain!"
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(60)
                    _save_fast_result('success')
                    return
                elif ts_result == "no_turnstile":
                    # Tidak ada Turnstile; cek notifikasi sukses langsung
                    if await detect_text_in_flip_frames("Successfully joined rain!", 30):
                        success_after_checkbox = True
                        break
                    # Tidak sukses, bukan crash
                    crashed_prev = False
                    continue
                else:
                    # ts_result == 'failed' atau nilai lain → bedakan crash vs loading
                    cf_crashed = await is_turnstile_crashed(page)
                    cf_loading = await is_turnstile_loading(page)
                    if cf_crashed:
                        print("[FAST] Gagal attempt: CRASH Turnstile terdeteksi (akan reload di attempt berikutnya)")
                        crashed_prev = True
                    elif cf_loading:
                        print("[FAST] Gagal attempt: Turnstile masih LOADING (tanpa reload)")
                        crashed_prev = False
                    else:
                        print("[FAST] Gagal attempt: Tidak sukses dan tidak terindikasi crash/ loading")
                        crashed_prev = False
                    continue

            if not success_after_checkbox:
                print("[FAST] Gagal mencapai status sukses setelah 3 attempt.")
                if telegram:
                    try:
                        await telegram.send_message(
                            "❌ Gagal join rain setelah 3 percobaan. Akan menghentikan GoLogin dan mengembalikan Watcher ke mode cek active."
                        )
                    except Exception:
                        pass
                _save_fast_result('failed')
                return

            # deteksi notifikasi akhir: success ATAU already (exclude frame CF)
            success = await detect_text_in_flip_frames("Successfully joined rain!", 60)
            if not success:
                already_end = await detect_text_in_flip_frames("You have already entered this rain!", 60)
                if not already_end:
                    print("[FAST] Tidak menemukan notifikasi success/already pada tahap akhir.")
                    _save_fast_result('failed')
                    return
                # Already detected
                if telegram:
                    await telegram.send_message("ℹ️ <b>ALREADY JOINED</b>\n\nYou have already entered this rain!")
                await asyncio.sleep(60)
                _save_fast_result('success')
                return

            # Success detected: kirim notif, diam 1 menit, lalu kembali ke watcher
            if telegram:
                await telegram.send_message("🎉 <b>SUKSES JOIN RAIN!</b>\n\nKonfirmasi: <i>Successfully joined rain!</i>")
            await asyncio.sleep(60)
            _save_fast_result('success')
            return

        # ====== MODE LAMA (loop monitoring) tetap seperti sebelumnya ======
        last_reload = 0.0
        last_join = 0.0
        loop_i = 0
        consecutive_errors = 0
        
        # Cek saldo Capsolver di awal
        if capsolver:
            try:
                balance = await capsolver.get_balance()
                if telegram and balance is not None:
                    await telegram.send_balance_notification(balance)
            except Exception as e:
                print(f"[INIT] Error checking Capsolver balance: {e}")
        
        # Set akun default jika tersedia
        if ASF_AVAILABLE:
            try:
                accounts = load_accounts()
                if accounts:
                    first_account = accounts[0].get("name", "")
                    if first_account:
                        set_current_account(first_account)
            except Exception as e:
                print(f"[INIT] Error loading accounts: {e}")

        while True:
            loop_i += 1
            print(f"\n===== LOOP {loop_i} =====")
            try:
                consecutive_errors = 0
                last_reload = await page_reload_if_needed(page, last_reload)

                if is_already_joined_cooldown_active():
                    remaining = int(already_joined_until - now())
                    print(f"[ALREADY] Skip monitoring, sisa cooldown {remaining} detik")
                    await asyncio.sleep(CHECK_INTERVAL_SEC)
                    continue

                if await check_logout_status(page):
                    print("[LOGOUT] User logout terdeteksi, memulai auto-login...")
                    login_success = await handle_auto_login()
                    if login_success:
                        print("[LOGIN] Auto-login berhasil, melanjutkan monitoring...")
                        await asyncio.sleep(3)
                    else:
                        print("[LOGIN] Auto-login gagal, skip loop ini")
                        await asyncio.sleep(10)
                        continue

                try:
                    await validate_current_token()
                    await periodic_balance_check()
                except Exception as e:
                    print(f"[TOKEN] Error validating token: {e}")

                sel = await detect_active(page)
                if not sel:
                    print(f"[IDLE] Tidak ada active, tidur {CHECK_INTERVAL_SEC}s")
                    await asyncio.sleep(CHECK_INTERVAL_SEC + random.random())
                    continue

                print("[ACTIVE] Active terdeteksi!")
                ago = now() - last_join
                if ago < JOIN_COOLDOWN_SEC:
                    print(f"[COOLDOWN] {int(JOIN_COOLDOWN_SEC-ago)}s tersisa.")
                    await asyncio.sleep(1.5)
                    continue

                if await click_join(page, sel):
                    last_join = now()
                    turnstile_result = await handle_turnstile_challenge(page)
                    if turnstile_result in ["capsolver_success", "manual_success", "instant_success"]:
                        print("[FLOW] Turnstile selesai, cek notifikasi sukses...")
                        success_detected = await detect_success_notification(page, SUCCESS_CHECK_TIMEOUT)
                        if success_detected:
                            print("[FLOW] Sukses terdeteksi, klik lagi...")
                            await send_claim_success_notification("🎯 Bot berhasil join!")
                            await click_join(page, sel)
                        else:
                            print("[FLOW] Tidak ada notifikasi sukses.")
                    elif turnstile_result == "no_turnstile":
                        success_detected = await detect_success_notification(page, 5)
                        if success_detected:
                            await send_claim_success_notification("🎯 Sukses join tanpa Turnstile!")
                            await click_join(page, sel)
                    else:
                        print("[FLOW] Gagal menyelesaikan Turnstile")
                        if telegram:
                            await telegram.send_error_notification(
                                "Gagal menyelesaikan Turnstile challenge",
                                {'website': TARGET_URL, 'timestamp': time.time()}
                            )

                    await asyncio.sleep(AFTER_JOIN_IDLE_SEC)
                else:
                    print("[WARN] gagal klik tombol walau active.")
                    await asyncio.sleep(2)

            except Exception as e:
                consecutive_errors += 1
                print(f"[ERROR] Loop error #{consecutive_errors}: {repr(e)}")
                is_crashed = await check_page_crashed(page)
                if is_crashed:
                    print("[ERROR] Page crashed terdeteksi, TAPI force reload saat crash DINONAKTIFKAN")
                if consecutive_errors >= 3:
                    print("[ERROR] Terlalu banyak error berturut-turut, force reload...")
                    try:
                        await send_event(f"Force reload: error beruntun {consecutive_errors}")
                        reload_success = False
                        for reload_retry in range(3):
                            try:
                                print(f"[ERROR] Force reload percobaan #{reload_retry + 1}")
                                await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
                                await asyncio.sleep(3)
                                if not await check_page_crashed(page):
                                    last_reload = now()
                                    consecutive_errors = 0
                                    reload_success = True
                                    print("[ERROR] Force reload berhasil")
                                    break
                                else:
                                    print(f"[ERROR] Page masih crashed setelah reload #{reload_retry + 1}")
                                    await asyncio.sleep(5)
                            except Exception as reload_error:
                                print(f"[ERROR] Force reload #{reload_retry + 1} gagal: {reload_error}")
                                await asyncio.sleep(10)
                        if not reload_success:
                            print("[ERROR] Semua percobaan force reload gagal")
                    except Exception as reload_error:
                        print(f"[ERROR] Force reload gagal: {reload_error}")
                if telegram and consecutive_errors <= 3:
                    await telegram.send_error_notification(f"Bot error: {str(e)}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
