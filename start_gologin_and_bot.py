import os
import sys
import logging

# ================== SETUP LOGGING (PALING ATAS) ==================
# Konfigurasi ini harus dijalankan sebelum import lainnya untuk memastikan
# semua logger dari pustaka pihak ketiga dapat di-override.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# Bungkam semua logger yang "cerewet" dari pustaka telegram
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.request").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

import requests
import subprocess
import json
import asyncio
import psutil

# ================== SETUP LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[
        logging.StreamHandler() # Tampilkan log ke konsol
    ]
)

try:
    from gologin import GoLogin
except ImportError:
    print("[GoLogin] ERROR: Pustaka 'gologin' tidak ditemukan.")
    print("[GoLogin] Silakan install dengan menjalankan: pip install gologin requests")
    sys.exit(1)

# ================== KONFIGURASI DARI FILE ==================
# Menggunakan path dinamis agar bisa dijalankan dari mana saja
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_CONFIG_PATH = os.path.join(BASE_DIR, "bot_config.json")

def load_bot_config():
    """Memuat konfigurasi dari file JSON."""
    try:
        with open(BOT_CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"[Config] ERROR: Gagal memuat {BOT_CONFIG_PATH}: {e}")
        sys.exit(1)

config = load_bot_config()



# 3. Path ke skrip bot utama Anda
BOT_SCRIPT_PATH = os.path.join(BASE_DIR, "bot_cdp.py")
# =================================================

def get_profile_id(token, profile_name):
    """Mendapatkan ID profil berdasarkan namanya."""
    logging.info(f"[GoLogin] Mencari ID untuk profil: {profile_name}")
    url = "https://api.gologin.com/browser/v2"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        profiles = response.json().get("profiles", [])
        for profile in profiles:
            if profile.get("name").lower() == profile_name.lower():
                profile_id = profile.get("id")
                logging.info(f"[GoLogin] Profil ditemukan. ID: {profile_id}")
                return profile_id
        logging.error(f"[GoLogin] ERROR: Profil dengan nama '{profile_name}' tidak ditemukan.")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"[GoLogin] ERROR: Gagal menghubungi API GoLogin: {e}")
        return None

def start_gologin_profile(token, profile_id):
    """Menjalankan profil GoLogin dan mendapatkan port CDP."""
    logging.info("[GoLogin] Meminta GoLogin untuk menjalankan profil...")
    try:
        # Inisialisasi GoLogin dengan token Anda
        gl = GoLogin({
            "token": token,
            "profile_id": profile_id,
            # "start_url": "https://flip.gg/" # URL bisa diset di sini jika perlu
        })

        # Pustaka akan mencari port secara otomatis
        logging.info("[GoLogin] Menjalankan profil menggunakan pustaka resmi...")
        debugger_address = gl.start()

        # Ekstrak port dari alamat debugger
        # Contoh: 127.0.0.1:54321
        cdp_port = debugger_address.split(":")[-1]
        logging.info(f"[GoLogin] Profil berhasil dijalankan. Alamat CDP: {debugger_address}")
        logging.info(f"[GoLogin] Port CDP yang diekstrak: {cdp_port}")
        return cdp_port

    except Exception as e:
        logging.error(f"[GoLogin] ERROR: Gagal menjalankan profil via pustaka: {e}")
        logging.error("[GoLogin] Pastikan aplikasi GoLogin sedang berjalan di RDP Anda.")
        return None

def update_bot_config(port):
    """Update cdp_url di bot_config.json."""
    logging.info(f"[Config] Mengupdate {BOT_CONFIG_PATH} dengan port {port}...")
    with open(BOT_CONFIG_PATH, 'r+') as f:
        config_data = json.load(f)
        config_data['cdp_url'] = f"http://127.0.0.1:{port}"
        f.seek(0)
        json.dump(config_data, f, indent=2)
        f.truncate()
    logging.info("[Config] File konfigurasi berhasil diupdate.")


def _kill_processes_for_port_by_net(port: int, grace_seconds: float = 2.0) -> tuple[bool, str]:
    """Bunuh proses yang menggunakan port tertentu via net_connections sebagai fallback."""
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
        victims = {pid for pid in victims if pid and pid > 0}
        if not victims:
            return False, f"Tidak ada proses di port {port}"
        for pid in victims:
            try:
                psutil.Process(pid).terminate()
            except psutil.NoSuchProcess:
                pass
        import time
        t0 = time.time()
        while time.time() - t0 < grace_seconds:
            alive = [pid for pid in victims if psutil.pid_exists(pid)]
            if not alive:
                return True, f"Terminated PID(s): {', '.join(map(str, victims))}"
            time.sleep(0.2)
        for pid in list(victims):
            if psutil.pid_exists(pid):
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass
        return True, f"Killed PID(s): {', '.join(map(str, victims))}"
    except Exception as e:
        return False, f"Kill by net error: {e}"


def stop_gologin_profile(token: str, profile_id: str, port: str | None) -> tuple[bool, str]:
    """Hentikan profil GoLogin via SDK. Fallback: kill proses di port CDP jika perlu."""
    # 1) via SDK
    try:
        gl = GoLogin({"token": token, "profile_id": profile_id})
        gl.stop()
        return True, "Profil dihentikan via SDK"
    except Exception as e:
        logging.error(f"[GoLogin] stop error: {e}")
    # 2) fallback via net
    try:
        p = int(str(port)) if port else None
    except Exception:
        p = None
    if p:
        ok, msg = _kill_processes_for_port_by_net(p)
        return ok, (msg if ok else f"Fallback berhenti gagal: {msg}")
    return False, "Tidak ada port untuk fallback kill"

if __name__ == "__main__":
    gologin_api_token = config.get("gologin_api_token")
    gologin_profile_name = config.get("gologin_profile_name")

    if not gologin_api_token or not gologin_profile_name:
        logging.error("[Config] ERROR: 'gologin_api_token' atau 'gologin_profile_name' tidak ditemukan di bot_config.json")
        sys.exit(1)

    profile_id = get_profile_id(gologin_api_token, gologin_profile_name)
    if profile_id:
        port = start_gologin_profile(gologin_api_token, profile_id)
        if port:
            update_bot_config(port)
            logging.info("\n[Bot] Menjalankan skrip bot utama...")
            subprocess.run([sys.executable, BOT_SCRIPT_PATH])
            logging.info("\n[Bot] Skrip bot telah selesai.")
            # Hentikan profil segera setelah bot selesai
            logging.info("[GoLogin] Menghentikan profil...")
            ok, msg = stop_gologin_profile(gologin_api_token, profile_id, port)
            logging.info(("✅ " if ok else "❌ ") + msg)