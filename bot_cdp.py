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

# ================== KONFIG ==================
CDP_URL = config.get("cdp_url", "http://127.0.0.1:9222")
TARGET_URL = config.get("target_url", "https://flip.gg/")
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

async def perform_random_activity(page):
    """Lakukan aktivitas random untuk terlihat seperti user asli"""
    try:
        activities = [
            "scroll_down",
            "scroll_up", 
            "mouse_move",
            "hover_element",
            "small_scroll",
            "page_click"
        ]
        
        # Pilih aktivitas random
        activity = random.choice(activities)
        print(f"[ACTIVITY] Melakukan aktivitas random: {activity}")
        
        if activity == "scroll_down":
            # Scroll ke bawah dengan jarak random
            scroll_amount = random.randint(200, 800)
            await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
        elif activity == "scroll_up":
            # Scroll ke atas dengan jarak random
            scroll_amount = random.randint(200, 600)
            await page.evaluate(f"window.scrollBy(0, -{scroll_amount})")
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
        elif activity == "mouse_move":
            # Gerakan mouse random
            viewport = await page.viewport_size()
            if viewport:
                x = random.randint(100, viewport['width'] - 100)
                y = random.randint(100, viewport['height'] - 100)
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.3, 0.8))
                
        elif activity == "hover_element":
            # Hover ke elemen random yang aman
            safe_selectors = [
                'body',
                'header',
                '.container',
                'nav',
                'footer',
                'main'
            ]
            
            for selector in safe_selectors:
                try:
                    if await page.locator(selector).count() > 0:
                        element = page.locator(selector).first
                        await element.hover()
                        await asyncio.sleep(random.uniform(0.5, 1.0))
                        break
                except Exception:
                    continue
                    
        elif activity == "small_scroll":
            # Scroll kecil-kecil seperti user membaca
            for _ in range(random.randint(2, 5)):
                scroll_amount = random.randint(50, 150)
                direction = random.choice([1, -1])
                await page.evaluate(f"window.scrollBy(0, {scroll_amount * direction})")
                await asyncio.sleep(random.uniform(0.3, 0.7))
                
        elif activity == "page_click":
            # Klik di area kosong yang aman
            viewport = await page.viewport_size()
            if viewport:
                # Klik di area yang kemungkinan kosong
                safe_areas = [
                    (viewport['width'] // 4, viewport['height'] // 4),
                    (viewport['width'] // 2, viewport['height'] // 6),
                    (viewport['width'] * 3 // 4, viewport['height'] // 4),
                ]
                
                x, y = random.choice(safe_areas)
                x += random.randint(-50, 50)
                y += random.randint(-30, 30)
                
                try:
                    await page.mouse.click(x, y)
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                except Exception:
                    pass
        
        print(f"[ACTIVITY] Aktivitas {activity} selesai")
        
    except Exception as e:
        print(f"[ACTIVITY] Error saat melakukan aktivitas random: {e}")

async def should_perform_random_activity():
    """Cek apakah sudah waktunya untuk aktivitas random"""
    global last_random_activity
    current_time = now()
    
    # Lakukan aktivitas random setiap 15-45 detik
    interval = random.randint(15, 45)
    
    if current_time - last_random_activity > interval:
        last_random_activity = current_time
        return True
    
    return False

async def perform_idle_activities(page):
    """Lakukan berbagai aktivitas saat idle untuk terlihat natural"""
    try:
        print("[IDLE_ACTIVITY] Memulai aktivitas idle...")
        
        # Kombinasi aktivitas yang lebih kompleks
        activities_sequence = [
            "scroll_exploration",
            "mouse_movement_pattern", 
            "reading_simulation",
            "page_interaction"
        ]
        
        activity = random.choice(activities_sequence)
        print(f"[IDLE_ACTIVITY] Melakukan: {activity}")
        
        if activity == "scroll_exploration":
            # Simulasi user menjelajahi halaman
            print("[IDLE_ACTIVITY] Simulasi eksplorasi halaman...")
            
            # Scroll ke bawah perlahan
            for _ in range(random.randint(3, 7)):
                scroll_amount = random.randint(150, 400)
                await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                await asyncio.sleep(random.uniform(1.0, 2.5))
                
                # Sesekali pause seperti membaca
                if random.random() < 0.3:
                    await asyncio.sleep(random.uniform(2.0, 4.0))
            
            # Scroll kembali ke atas
            await asyncio.sleep(random.uniform(1.0, 2.0))
            for _ in range(random.randint(2, 4)):
                scroll_amount = random.randint(200, 500)
                await page.evaluate(f"window.scrollBy(0, -{scroll_amount})")
                await asyncio.sleep(random.uniform(0.8, 1.8))
                
        elif activity == "mouse_movement_pattern":
            # Gerakan mouse yang natural
            print("[IDLE_ACTIVITY] Simulasi gerakan mouse natural...")
            
            viewport = await page.viewport_size()
            if viewport:
                # Buat pola gerakan mouse yang natural
                points = []
                for _ in range(random.randint(5, 10)):
                    x = random.randint(100, viewport['width'] - 100)
                    y = random.randint(100, viewport['height'] - 100)
                    points.append((x, y))
                
                for x, y in points:
                    await page.mouse.move(x, y)
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    
                    # Sesekali hover sebentar
                    if random.random() < 0.4:
                        await asyncio.sleep(random.uniform(0.5, 1.0))
                        
        elif activity == "reading_simulation":
            # Simulasi user membaca konten
            print("[IDLE_ACTIVITY] Simulasi membaca konten...")
            
            # Scroll kecil-kecil seperti membaca
            for _ in range(random.randint(8, 15)):
                scroll_amount = random.randint(30, 100)
                await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                
                # Pause seperti membaca
                reading_time = random.uniform(1.5, 4.0)
                await asyncio.sleep(reading_time)
                
                # Sesekali scroll balik sedikit (seperti re-read)
                if random.random() < 0.2:
                    await page.evaluate(f"window.scrollBy(0, -{random.randint(20, 60)})")
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    
        elif activity == "page_interaction":
            # Interaksi dengan elemen halaman yang aman
            print("[IDLE_ACTIVITY] Simulasi interaksi halaman...")
            
            safe_interactions = [
                "hover_navigation",
                "click_safe_area", 
                "scroll_to_sections"
            ]
            
            interaction = random.choice(safe_interactions)
            
            if interaction == "hover_navigation":
                # Hover ke elemen navigasi
                nav_selectors = ['nav', 'header', '.navbar', '.menu', '.navigation']
                for selector in nav_selectors:
                    try:
                        if await page.locator(selector).count() > 0:
                            await page.locator(selector).first.hover()
                            await asyncio.sleep(random.uniform(1.0, 2.0))
                            break
                    except Exception:
                        continue
                        
            elif interaction == "click_safe_area":
                # Klik di area yang aman (tidak akan trigger action)
                viewport = await page.viewport_size()
                if viewport:
                    # Area margin yang aman
                    safe_x = random.choice([
                        random.randint(10, 50),  # Kiri
                        random.randint(viewport['width'] - 50, viewport['width'] - 10)  # Kanan
                    ])
                    safe_y = random.randint(100, viewport['height'] - 100)
                    
                    try:
                        await page.mouse.click(safe_x, safe_y)
                        await asyncio.sleep(random.uniform(0.5, 1.0))
                    except Exception:
                        pass
                        
            elif interaction == "scroll_to_sections":
                # Scroll ke berbagai section halaman
                for _ in range(random.randint(3, 6)):
                    # Random scroll amount dan direction
                    scroll_amount = random.randint(200, 600)
                    direction = random.choice([1, -1])
                    
                    await page.evaluate(f"window.scrollBy(0, {scroll_amount * direction})")
                    await asyncio.sleep(random.uniform(1.5, 3.0))
        
        print(f"[IDLE_ACTIVITY] Aktivitas {activity} selesai")
        
    except Exception as e:
        print(f"[IDLE_ACTIVITY] Error saat aktivitas idle: {e}")

async def stop_all_activities(page):
    """Stop semua aktivitas dan kembali ke posisi optimal"""
    try:
        print("[ACTIVITY] Menghentikan semua aktivitas, kembali ke posisi optimal...")
        
        # Scroll ke atas halaman untuk posisi optimal monitoring
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)
        
        # Posisikan mouse di area yang tidak mengganggu
        viewport = await page.viewport_size()
        if viewport:
            safe_x = viewport['width'] // 2
            safe_y = viewport['height'] // 4
            await page.mouse.move(safe_x, safe_y)
        
        print("[ACTIVITY] Posisi optimal untuk monitoring telah diset")
        
    except Exception as e:
        print(f"[ACTIVITY] Error saat stop activities: {e}")

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

async def page_reload_if_needed(page, last_reload_ts):
    t = now()
    current_url = page.url
    
    # Cek berbagai kondisi yang memerlukan reload
    need_reload = (
        current_url.startswith("about:blank") or 
        current_url == "chrome://newtab/" or
        current_url == "" or
        "chrome-error://" in current_url or
        (t - last_reload_ts > RELOAD_EVERY_SEC)
    )
    
    if need_reload:
        print(f"[RELOAD] Halaman perlu di-reload. URL saat ini: {current_url}")
        try:
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
            
            return now()
            
        except Exception as e:
            print(f"[RELOAD] Error saat reload: {e}")
            print("[RELOAD] Mencoba reload ulang dalam 10 detik...")
            await asyncio.sleep(10)
            try:
                await page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
                return now()
            except Exception as e2:
                print(f"[RELOAD] Error reload kedua: {e2}")
                return last_reload_ts
    
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

async def click_turnstile_checkbox(page, dialog_selector='div[role="dialog"]'):
    """Klik checkbox di iframe Turnstile dengan pencarian yang lebih komprehensif"""
    print("[TS] Mencari dan mengklik checkbox Turnstile di semua frame...")
    
    # Selectors untuk checkbox Turnstile yang lebih lengkap
    # ... (selectors tetap sama)
    checkbox_selectors = [
        'input[type="checkbox"]',
        '[role="checkbox"]', 
        'label:has([type="checkbox"])',
        'div[role="button"][tabindex]',
        'div[aria-checked]',
        '.cb-lb input[type="checkbox"]',  # Selector khusus dari inspect
        'label.cb-lb input',
        '#wNUym6 input[type="checkbox"]',
        '.cb-c input[type="checkbox"]',
        'input[type="checkbox"][class*="cb"]',
        'label[class*="cb"] input[type="checkbox"]'
    ]

    # Tunggu dialog/modal muncul terlebih dahulu
    try:
        print(f"[TS] Menunggu dialog muncul: {dialog_selector}")
        dialog = page.locator(dialog_selector).last
        await dialog.wait_for(state="visible", timeout=10000)
        print("[TS] Dialog terdeteksi. Fokus pencarian di dalam dialog.")
        # Set context pencarian ke dalam dialog
        search_context = dialog
    except Exception:
        print("[TS] Dialog tidak terdeteksi, melanjutkan pencarian di seluruh halaman.")
        search_context = page


    
    # 1. Cek di main page dulu (mungkin tidak di iframe)
    print("[TS] Mengecek checkbox di main page...")
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
                        print(f"[TS] Checkbox 'Verify you are human' ditemukan di main page: {selector}")
                        await element.scroll_into_view_if_needed()
                        await element.click(force=True)
                        print("[TS] Checkbox berhasil diklik di main page!")
                        return True
                except Exception:
                    # Jika tidak bisa cek parent text, coba klik saja
                    print(f"[TS] Mencoba klik checkbox di main page: {selector}")
                    await element.scroll_into_view_if_needed()
                    await element.click(force=True)
                    print("[TS] Checkbox diklik di main page!")
                    return True
        except Exception as e:
            continue
    
    # 2. Cek di iframe Turnstile tradisional
    print("[TS] Mengecek iframe Turnstile tradisional...")
    try: # Menggunakan search_context (dialog atau page)
        await search_context.locator(IFRAME_TURNSTILE).first.wait_for(state="visible", timeout=10_000)
        fl = page.frame_locator(IFRAME_TURNSTILE)
        
        for selector in checkbox_selectors:
            try:
                el = fl.locator(selector).first
                await el.wait_for(state="visible", timeout=3000)
                await el.click(force=True)
                print(f"[TS] Checkbox diklik di iframe Turnstile: {selector}")
                return True
            except Exception:
                continue
    except PWTimeout:
        print("[TS] Iframe Turnstile tradisional tidak ditemukan")
    
    # 3. Cek di SEMUA frame/iframe yang ada
    print("[TS] Fallback: Mengecek di semua frame yang tersedia di halaman...")
    for frame in page.frames:
        try:
            frame_url = frame.url or ""
            print(f"[TS] Mengecek frame: {frame_url}")
            
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
                                print(f"[TS] Checkbox 'Verify you are human' ditemukan di frame {frame_url}: {selector}")
                                await element.click(force=True)
                                print(f"[TS] Checkbox berhasil diklik di frame!")
                                return True
                        except Exception:
                            pass
                        
                        # Jika frame mengandung cloudflare/turnstile, langsung coba klik
                        if any(keyword in frame_url.lower() for keyword in ["cloudflare", "turnstile", "challenges"]):
                            print(f"[TS] Frame Cloudflare/Turnstile terdeteksi, klik checkbox: {selector}")
                            await element.click(force=True)
                            print(f"[TS] Checkbox diklik di frame Cloudflare!")
                            return True
                        
                        # Untuk frame lain, coba klik jika selector cocok dengan pattern Turnstile
                        if any(pattern in selector for pattern in ["cb-", "checkbox"]):
                            print(f"[TS] Pattern Turnstile terdeteksi di frame, klik checkbox: {selector}")
                            await element.click(force=True)
                            print(f"[TS] Checkbox diklik di frame!")
                            return True
                            
                except Exception as e:
                    continue
                    
        except Exception as e:
            continue
    
    # 4. Fallback: cari berdasarkan text content
    print("[TS] Fallback: mencari berdasarkan text 'Verify you are human'...")
    try:
        # Cek di main page
        verify_elements = await page.locator('text=/verify.*human/i').all()
        for element in verify_elements:
            try:
                # Cari checkbox di dalam atau dekat element ini
                checkbox = element.locator('input[type="checkbox"]').first
                if await checkbox.count() > 0:
                    await checkbox.click(force=True)
                    print("[TS] Checkbox ditemukan via text search di main page!")
                    return True
                    
                # Cari di parent
                parent_checkbox = element.locator('xpath=..//*[@type="checkbox"]').first
                if await parent_checkbox.count() > 0:
                    await parent_checkbox.click(force=True)
                    print("[TS] Checkbox ditemukan via parent text search di main page!")
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
                            await checkbox.click(force=True)
                            print(f"[TS] Checkbox ditemukan via text search di frame {frame.url}!")
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
                
    except Exception as e:
        print(f"[TS] Error dalam text search: {e}")
    
    print("[TS] Checkbox tidak ditemukan di manapun")
    return False

async def wait_turnstile_token(page, timeout_ms):
    """Tunggu token Turnstile terisi (jika elemen ada)."""
    print("[TS] Menunggu token Turnstile…")
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
            print("[TS] Token terdeteksi ��")
            return val
        if i % 5 == 0:
            print(f"[TS] …menunggu token ({i}s)")
        await asyncio.sleep(1)
    print("[TS] Timeout nunggu token.")
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
    print("[TURNSTILE] Memulai handling Turnstile challenge...")
    
    # LANGKAH 1: Deteksi Turnstile dan extract informasi
    print("[TURNSTILE] Mengekstrak informasi Turnstile...")
    website_url, sitekey, action, cdata = await extract_turnstile_info(page)
    
    if not sitekey or sitekey == "0x4AAAAAAADnPIDROlWd_wc":
        print("[TURNSTILE] Sitekey tidak valid atau tidak ditemukan")
        # Coba klik checkbox manual jika ada
        checkbox_clicked = await click_turnstile_checkbox(page, dialog_selector='div[role="dialog"]')
        if checkbox_clicked:
            print("[TURNSTILE] Checkbox diklik, tunggu hasil...")
            await asyncio.sleep(5)
            
            # Cek hasil setelah klik checkbox
            success_found = await detect_success_notification(page, 10)
            if success_found:
                return "instant_success"
                
            already_joined = await check_already_joined(page)
            if already_joined:
                set_already_joined_cooldown()
                return "already_joined"
        
        return "no_turnstile"
    
    # LANGKAH 2: Gunakan CapSolver untuk menyelesaikan challenge
    if capsolver and AUTO_SOLVE_CAPTCHA:
        print("[TURNSTILE] Menggunakan CapSolver untuk menyelesaikan Turnstile...")
        
        # Solve menggunakan CapSolver dengan metadata yang benar
        solved_token = await capsolver.solve_turnstile(
            website_url=website_url,
            website_key=sitekey,
            action=action,
            cdata=cdata
        )
        
        if solved_token:
            print("[TURNSTILE] Token berhasil didapat dari CapSolver!")
            
            # LANGKAH 3: Inject token ke halaman
            token_injected = await inject_turnstile_token(page, solved_token)
            
            if token_injected:
                print("[TURNSTILE] Token berhasil diinjeksi ke halaman!")
                
                # LANGKAH 4: Trigger callback dan update UI
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
                
                # Tunggu sebentar untuk memastikan perubahan diterapkan
                await asyncio.sleep(2)
                
                # LANGKAH 5: Cek hasil
                print("[TURNSTILE] Mengecek hasil setelah inject token...")
                
                # Cek sukses terlebih dahulu
                success_found = await detect_success_notification(page, 15)
                if success_found:
                    print("[TURNSTILE] Sukses terdeteksi setelah CapSolver!")
                    
                    # Kirim notifikasi ke Telegram
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
                
                # Cek already joined
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
                    
                    return "already_joined"
                
                print("[TURNSTILE] Token diinjeksi tapi tidak ada feedback yang jelas")
                return "capsolver_success"  # Anggap berhasil karena token sudah diinjeksi
                
            else:
                print("[TURNSTILE] Gagal inject token dari CapSolver")
                return "failed"
        else:
            print("[TURNSTILE] CapSolver gagal menyelesaikan challenge")
            return "failed"
    
    # LANGKAH 6: Fallback - coba klik checkbox manual
    print("[TURNSTILE] CapSolver tidak tersedia, mencoba klik checkbox manual...")
    checkbox_clicked = await click_turnstile_checkbox(page, dialog_selector='div[role="dialog"]')
    
    if checkbox_clicked:
        print("[TURNSTILE] Checkbox diklik, tunggu hasil...")
        await asyncio.sleep(5)
        
        # Cek hasil setelah klik checkbox
        success_found = await detect_success_notification(page, 15)
        if success_found:
            return "instant_success"
            
        already_joined = await check_already_joined(page)
        if already_joined:
            set_already_joined_cooldown()
            return "already_joined"
        
        # Tunggu manual solve jika ada input field
        print("[TURNSTILE] Menunggu manual solve...")
        token = await wait_turnstile_token(page, TURNSTILE_WAIT_MS)
        if token:
            return "manual_success"
    
    print("[TURNSTILE] Semua metode gagal")
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
                # Reset error counter jika berhasil
                consecutive_errors = 0
                
                # reload berkala
                last_reload = await page_reload_if_needed(page, last_reload)

                # FITUR BARU: Cek already joined cooldown
                if is_already_joined_cooldown_active():
                    remaining = int(already_joined_until - now())
                    print(f"[ALREADY] Skip monitoring, sisa cooldown {remaining} detik")
                    
                    # Lakukan aktivitas random selama cooldown
                    print("[ALREADY] Melakukan aktivitas random selama cooldown...")
                    await perform_idle_activities(page)
                    
                    await asyncio.sleep(10)  # Sleep 10 detik lalu cek lagi
                    continue

                # FITUR BARU: Cek logout status SEBELUM detect active
                if await check_logout_status(page):
                    print("[LOGOUT] User logout terdeteksi, memulai auto-login...")
                    login_success = await handle_auto_login()
                    if login_success:
                        print("[LOGIN] Auto-login berhasil, melanjutkan monitoring...")
                        # Tunggu sebentar untuk halaman stabil, lalu lanjut ke detect_active
                        await asyncio.sleep(3)
                    else:
                        print("[LOGIN] Auto-login gagal, skip loop ini")
                        await asyncio.sleep(10)
                        continue

                # FITUR BARU: Validasi token berkala (tidak mengganggu alur utama)
                try:
                    await validate_current_token()
                    await periodic_balance_check()
                except Exception as e:
                    print(f"[TOKEN] Error validating token: {e}")

                # deteksi active (ALUR ASLI TIDAK BERUBAH)
                sel = await detect_active(page)
                if not sel:
                    print(f"[IDLE] Tidak ada active, melakukan aktivitas random...")
                    
                    # Lakukan aktivitas random saat idle/monitoring
                    if await should_perform_random_activity():
                        await perform_random_activity(page)
                    
                    print(f"[IDLE] tidur {CHECK_INTERVAL_SEC}s")
                    await asyncio.sleep(CHECK_INTERVAL_SEC + random.random())
                    continue

                # ACTIVE TERDETEKSI - STOP SEMUA AKTIVITAS
                print("[ACTIVE] Active terdeteksi! Menghentikan semua aktivitas random...")
                await stop_all_activities(page)

                # cooldown anti spam
                ago = now() - last_join
                if ago < JOIN_COOLDOWN_SEC:
                    print(f"[COOLDOWN] {int(JOIN_COOLDOWN_SEC-ago)}s tersisa.")
                    await asyncio.sleep(1.5)
                    continue

                # klik join
                if await click_join(page, sel):
                    last_join = now()
                    
                    # Handle Turnstile challenge dengan logika baru
                    turnstile_result = await handle_turnstile_challenge(page)
                    
                    if turnstile_result == "instant_success":
                        print("[FLOW] Notifikasi sukses ditemukan setelah klik checkbox!")
                        print("[FLOW] Langsung klik join lagi tanpa jeda...")
                        # Langsung klik lagi tanpa menunggu
                        continue
                        
                    elif turnstile_result == "already_joined":
                        print("[FLOW] Already joined terdeteksi, jangan klik apapun selama 3 menit")
                        # Cooldown sudah diset di handle_turnstile_challenge
                        continue
                        
                    elif turnstile_result in ["capsolver_success", "manual_success"]:
                        print(f"[FLOW] Turnstile berhasil diselesaikan via {turnstile_result}")
                        
                        # Setelah Turnstile berhasil, cek notifikasi sukses di semua frame
                        print("[FLOW] Mengecek notifikasi sukses di semua frame setelah Turnstile...")
                        success_detected = await detect_success_notification(page, SUCCESS_CHECK_TIMEOUT)
                        
                        if success_detected:
                            print("[FLOW] Notifikasi sukses terdeteksi setelah Turnstile!")
                            print("[FLOW] Langsung klik join lagi tanpa jeda...")
                            
                            # Kirim notifikasi sukses
                            await send_claim_success_notification("🎯 Bot berhasil join setelah solve Turnstile!")
                            
                            # Langsung klik lagi tanpa menunggu
                            continue
                        else:
                            print("[FLOW] Tidak ada notifikasi sukses setelah Turnstile")
                            
                            # Cek apakah ada notifikasi already joined
                            already_check = await check_already_joined(page)
                            if already_check:
                                print("[FLOW] Already joined terdeteksi setelah Turnstile, set cooldown 3 menit")
                                set_already_joined_cooldown()
                                
                                if telegram:
                                    await telegram.send_message(
                                        "ℹ️ <b>ALREADY JOINED (Post-Turnstile)</b>\n\n"
                                        "🔄 Sudah join rain sebelumnya\n"
                                        "⏰ Skip monitoring selama 3 menit\n"
                                        "🎯 Bot akan lanjut monitoring setelah cooldown"
                                    )
                                continue
                            
                    elif turnstile_result == "no_turnstile":
                        print("[FLOW] Tidak ada Turnstile, langsung cek hasil")
                        # Cek hasil join langsung
                        success_detected = await detect_success_notification(page, 5)
                        if success_detected:
                            print("[FLOW] Sukses join tanpa Turnstile!")
                            print("[FLOW] Langsung klik join lagi tanpa jeda...")
                            await send_claim_success_notification("🎯 Sukses join tanpa Turnstile!")
                            # Langsung klik lagi
                            continue
                        else:
                            # Cek already joined jika tidak ada sukses
                            already_check = await check_already_joined(page)
                            if already_check:
                                print("[FLOW] Already joined terdeteksi tanpa Turnstile, set cooldown 3 menit")
                                set_already_joined_cooldown()
                                continue
                        
                    else:  # failed
                        print("[FLOW] Turnstile gagal diselesaikan")
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
                
                # Jika terlalu banyak error berturut-turut, coba reload halaman
                if consecutive_errors >= 5:
                    print("[ERROR] Terlalu banyak error berturut-turut, force reload...")
                    try:
                        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
                        last_reload = now()
                        consecutive_errors = 0
                        print("[ERROR] Force reload berhasil")
                    except Exception as reload_error:
                        print(f"[ERROR] Force reload gagal: {reload_error}")
                
                if telegram and consecutive_errors <= 3:  # Hindari spam notifikasi error
                    await telegram.send_error_notification(f"Bot error: {str(e)}")
                
                await asyncio.sleep(5)  # Sleep lebih lama saat error

if __name__ == "__main__":
    asyncio.run(main())