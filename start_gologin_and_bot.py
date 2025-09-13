import requests
import subprocess
import os
import sys
import json
import asyncio

try:
    from gologin import GoLogin
except ImportError:
    print("[GoLogin] ERROR: Pustaka 'gologin' tidak ditemukan.")
    print("[GoLogin] Silakan install dengan menjalankan: pip install gologin")
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
        print(f"[Config] ERROR: Gagal memuat {BOT_CONFIG_PATH}: {e}")
        sys.exit(1)

config = load_bot_config()

# 3. Path ke skrip bot utama Anda
BOT_SCRIPT_PATH = os.path.join(BASE_DIR, "bot_cdp.py")
# =================================================

def get_profile_id(token, profile_name):
    """Mendapatkan ID profil berdasarkan namanya."""
    print(f"[GoLogin] Mencari ID untuk profil: {profile_name}")
    url = "https://api.gologin.com/browser/v2"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        profiles = response.json().get("profiles", [])
        for profile in profiles:
            if profile.get("name").lower() == profile_name.lower():
                profile_id = profile.get("id")
                print(f"[GoLogin] Profil ditemukan. ID: {profile_id}")
                return profile_id
        print(f"[GoLogin] ERROR: Profil dengan nama '{profile_name}' tidak ditemukan.")
        return None
    except requests.exceptions.RequestException as e:
        print(f"[GoLogin] ERROR: Gagal menghubungi API GoLogin: {e}")
        return None

def start_gologin_profile(token, profile_id):
    """Menjalankan profil GoLogin dan mendapatkan port CDP."""
    print("[GoLogin] Meminta GoLogin untuk menjalankan profil...")
    try:
        # Inisialisasi GoLogin dengan token Anda
        gl = GoLogin({
            "token": token,
            "profile_id": profile_id,
        })

        # Pustaka akan mencari port secara otomatis
        print("[GoLogin] Menjalankan profil menggunakan pustaka resmi...")
        debugger_address = gl.start()

        # Ekstrak port dari alamat debugger
        # Contoh: 127.0.0.1:54321
        cdp_port = debugger_address.split(":")[-1]
        print(f"[GoLogin] Profil berhasil dijalankan. Alamat CDP: {debugger_address}")
        print(f"[GoLogin] Port CDP yang diekstrak: {cdp_port}")
        return cdp_port

    except Exception as e:
        print(f"[GoLogin] ERROR: Gagal menjalankan profil via pustaka: {e}")
        print("[GoLogin] Pastikan aplikasi GoLogin sedang berjalan di RDP Anda.")
        return None

def update_bot_config(port):
    """Update cdp_url di bot_config.json."""
    print(f"[Config] Mengupdate {BOT_CONFIG_PATH} dengan port {port}...")
    with open(BOT_CONFIG_PATH, 'r+') as f:
        config_data = json.load(f)
        config_data['cdp_url'] = f"http://127.0.0.1:{port}"
        f.seek(0)
        json.dump(config_data, f, indent=2)
        f.truncate()
    print("[Config] File konfigurasi berhasil diupdate.")

if __name__ == "__main__":
    gologin_api_token = config.get("gologin_api_token")
    gologin_profile_name = config.get("gologin_profile_name")

    if not gologin_api_token or not gologin_profile_name:
        print("[Config] ERROR: 'gologin_api_token' atau 'gologin_profile_name' tidak ditemukan di bot_config.json")
        sys.exit(1)

    profile_id = get_profile_id(gologin_api_token, gologin_profile_name)
    if profile_id:
        port = start_gologin_profile(gologin_api_token, profile_id)
        if port:
            update_bot_config(port)
            print("\n[Bot] Menjalankan skrip bot utama...")
            subprocess.run([sys.executable, BOT_SCRIPT_PATH])
            print("\n[Bot] Skrip bot telah selesai.")