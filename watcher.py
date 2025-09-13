import asyncio
import json
import os
import sys
from typing import Optional, Tuple

import aiohttp
from playwright.async_api import async_playwright

from telegram_notifier import TelegramNotifier
from asf_core import validate_token_requests_fast
from asf_token_refresher import refresh_invalid_tokens

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'bot_config.json')
AKUN_PATH = os.path.join(os.path.dirname(__file__), 'akun.txt')
STATE_PATH = os.path.join(os.path.dirname(__file__), 'state.json')
TARGET_URL_DEFAULT = 'https://flip.gg/profile'

# Selectors (sinkron dengan dokumentasi rain.txt)
PRIZEBOX_ACTIVE = 'button:has(.tss-1msi2sy-prizeBox.active)'
BTN_ACTIVE = 'button.tss-pqm623-content.active'
JOIN_TEXT_ACTIVE = 'span.tss-7bx55w-rainStartedText.active'

CHECK_INTERVAL_SEC = 5  # loop cek active per 5 detik


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WATCHER] Gagal simpan config: {e}")


def get_token_from_file(path: str) -> Optional[str]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for raw in f:
                line = (raw or '').strip()
                if not line or line.startswith('#'):
                    continue
                # File Anda saat ini berisi token murni tanpa '='
                # Ambil baris apa adanya sebagai token
                return line
    except FileNotFoundError:
        print(f"[WATCHER] akun.txt tidak ditemukan: {path}")
    except Exception as e:
        print(f"[WATCHER] Error baca akun.txt: {e}")
    return None


def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


async def api_get_user_state(token: str) -> Optional[dict]:
    """Ambil data user via API langsung untuk wallet/WNFW. Return dict JSON atau None."""
    url = 'https://api.flip.gg/api/user'
    headers = {'x-auth-token': token}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    print(f"[WATCHER] /api/user status: {resp.status}")
                    return None
    except Exception as e:
        print(f"[WATCHER] API error: {e}")
        return None


async def validate_or_refresh_token(token: Optional[str], max_retries: int = 2) -> Optional[str]:
    """Validasi token cepat, jika invalid coba refresh via refresher dan baca ulang akun.txt. Batasi percobaan."""
    for attempt in range(max_retries + 1):
        if not token:
            print("[WATCHER] Token kosong.")
        else:
            ok, msg = validate_token_requests_fast(token)
            print(f"[WATCHER] Validasi token: {msg}")
            if ok:
                return token
        # Invalid → coba refresh (kecuali sudah di percobaan terakhir)
        if attempt < max_retries:
            print("[WATCHER] Token invalid, mulai refresh via asf_token_refresher...")
            try:
                await refresh_invalid_tokens(headless=True)
            except Exception as e:
                print(f"[WATCHER] refresh_invalid_tokens error: {e}")
            token = get_token_from_file(AKUN_PATH)
        else:
            break
    return None


async def send_telegram_if_available(cfg: dict, text: str) -> None:
    tok = (cfg.get('telegram_token') or '').strip()
    chat = (cfg.get('chat_id') or '').strip()
    if not tok or not chat:
        print(f"[WATCHER] Telegram tidak dikonfigurasi. Pesan: {text}")
        return
    try:
        notifier = TelegramNotifier(tok, chat)
        await notifier.send_message(text)
    except Exception as e:
        print(f"[WATCHER] Gagal kirim Telegram: {e}")


def set_fast_execute(cfg: dict, value: bool = True) -> None:
    try:
        current = bool(cfg.get('fast_execute', False))
        if current != value:
            cfg['fast_execute'] = value
            save_config(cfg)
            print(f"[WATCHER] fast_execute diset ke {value}")
    except Exception as e:
        print(f"[WATCHER] Gagal set fast_execute: {e}")


async def inject_token_init_script(context, token: str) -> None:
    # Injeksi token HANYA saat origin flip.gg
    script = f"""
        (() => {{
            try {{
                if (location && location.hostname && location.hostname.endsWith('flip.gg')) {{
                    localStorage.removeItem('token');
                    localStorage.setItem('token', '{token}');
                    try {{ sessionStorage.setItem('token', '{token}'); }} catch (e) {{}}
                }}
            }} catch (e) {{}}
        }})()
    """
    await context.add_init_script(script)


async def capture_wallet_and_wnfw(token: str) -> Tuple[Optional[float], Optional[float]]:
    data = await api_get_user_state(token)
    if not data or not isinstance(data, dict) or 'user' not in data:
        return None, None
    user = data.get('user') or {}
    wallet = safe_float(user.get('wallet'))
    wnfw = safe_float(user.get('wagerNeededForWithdraw'))
    return wallet, wnfw


def load_snapshot() -> Tuple[Optional[float], Optional[float]]:
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                s = json.load(f)
                return safe_float(s.get('wallet')), safe_float(s.get('wnfw'))
    except Exception:
        pass
    return None, None


def save_snapshot(wallet: Optional[float], wnfw: Optional[float]) -> None:
    try:
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump({'wallet': wallet, 'wnfw': wnfw}, f, indent=2)
    except Exception as e:
        print(f"[WATCHER] Gagal simpan snapshot: {e}")


def read_fast_result() -> Optional[str]:
    path = os.path.join(os.path.dirname(__file__), 'fast_exec_result.json')
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # bersihkan agar tidak terbaca ulang di iterasi berikutnya
            try:
                os.remove(path)
            except Exception:
                pass
            return str(data.get('status') or '').strip().lower() or None
    except Exception:
        pass
    return None


async def run_executor(cfg: dict) -> int:
    """Jalankan start_gologin_and_bot.py dan tunggu selesai. Return exit code."""
    try:
        # Set fast_execute = True agar bot_cdp menjalankan mode cepat
        set_fast_execute(cfg, True)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            os.path.join(os.path.dirname(__file__), 'start_gologin_and_bot.py'),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if stdout:
            print(stdout.decode(errors='ignore'))
        if stderr:
            print(stderr.decode(errors='ignore'))
        return proc.returncode or 0
    except Exception as e:
        print(f"[WATCHER] Gagal jalankan executor: {e}")
        return 1


async def watcher_main():
    cfg = load_config()
    target_url = cfg.get('target_url') or TARGET_URL_DEFAULT

    while True:  # nonstop loop
        # 1) Ambil token & validasi/recovery
        token = get_token_from_file(AKUN_PATH)
        token = await validate_or_refresh_token(token, max_retries=2)
        if not token:
            await send_telegram_if_available(cfg, '❌ Token tidak valid dan gagal refresh. Watcher berhenti sementara.')
            await asyncio.sleep(15)
            continue

        # 2) Ambil saldo & WNFW (awal)
        wallet, wnfw = await capture_wallet_and_wnfw(token)
        if wallet is not None and wnfw is not None:
            await send_telegram_if_available(cfg, f"📊 Saldo awal: {wallet:.8f} | WNFW: {wnfw:.8f}")
            save_snapshot(wallet, wnfw)
        else:
            await send_telegram_if_available(cfg, 'ℹ️ Gagal mengambil saldo awal/wnfw dari /api/user, lanjut cek active.')

        # 3) Launch Playwright biasa (injeksi JWT + cek active)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False)
            context = await browser.new_context()
            await inject_token_init_script(context, token)
            page = await context.new_page()
            try:
                await page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
                await send_telegram_if_available(cfg, f"🔎 Watcher aktif (non-headless). Memantau tombol active setiap {CHECK_INTERVAL_SEC}s")
            except Exception as e:
                print(f"[WATCHER] Gagal buka target: {e}")

            # 4) Loop cek active per 5 detik
            active_found = False
            while True:
                try:
                    # urutan deteksi selector
                    if await page.locator(PRIZEBOX_ACTIVE).count() > 0:
                        active_found = True
                    elif await page.locator(BTN_ACTIVE).count() > 0:
                        active_found = True
                    else:
                        loc = page.locator(f'button:has({JOIN_TEXT_ACTIVE})')
                        if await loc.count() > 0:
                            active_found = True
                except Exception:
                    active_found = False

                if active_found:
                    print('[WATCHER] Active terdeteksi. Menjalankan executor GoLogin...')
                    await send_telegram_if_available(cfg, '🚦 Active terdeteksi. Menjalankan eksekusi GoLogin...')
                    break
                await asyncio.sleep(CHECK_INTERVAL_SEC)

            # Tutup watcher browser sebelum jalankan executor
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

        # 5) Jalankan Executor (GoLogin fast-exec) dan tunggu selesai
        rc = await run_executor(cfg)
        print(f"[WATCHER] Executor selesai dengan kode: {rc}")

        # 6) Ambil saldo & WNFW (pasca klaim) dan bandingkan
        new_wallet, new_wnfw = await capture_wallet_and_wnfw(token)
        old_wallet, old_wnfw = load_snapshot()
        if new_wallet is not None and new_wnfw is not None:
            if old_wallet is not None and old_wnfw is not None:
                d_wallet = (new_wallet - old_wallet) if (isinstance(new_wallet, float) and isinstance(old_wallet, float)) else 0.0
                d_wnfw = (new_wnfw - old_wnfw) if (isinstance(new_wnfw, float) and isinstance(old_wnfw, float)) else 0.0
                sign_wallet = '+' if d_wallet >= 0 else ''
                sign_wnfw = '+' if d_wnfw >= 0 else ''
                await send_telegram_if_available(
                    cfg,
                    f"📈 Saldo: {new_wallet:.8f} ({sign_wallet}{d_wallet:.8f}) | WNFW: {new_wnfw:.8f} ({sign_wnfw}{d_wnfw:.8f})"
                )
            else:
                await send_telegram_if_available(cfg, f"📈 Saldo: {new_wallet:.8f} | WNFW: {new_wnfw:.8f}")
            save_snapshot(new_wallet, new_wnfw)
        else:
            await send_telegram_if_available(cfg, 'ℹ️ Gagal mengambil saldo/wnfw pasca klaim.')

        # 7) Baca hasil eksekusi cepat; jika success/already → jeda 3 menit sebelum cek active lagi
        result = read_fast_result()
        if result == 'success':
            await send_telegram_if_available(cfg, '⏳ Jeda 1 menit sebelum cek active lagi (post-success).')
            await asyncio.sleep(60)

        # 8) Kembali ke awal loop (nonstop)
        print('[WATCHER] Kembali ke mode pemantauan (loop nonstop).')
        await asyncio.sleep(2)


if __name__ == '__main__':
    try:
        asyncio.run(watcher_main())
    except KeyboardInterrupt:
        print('\n[WATCHER] Dihentikan oleh user')
    except Exception as e:
        print(f"[WATCHER] Fatal error: {e}")
