import os
import requests
from typing import List, Dict, Tuple
from asf_http_manager import http_manager

# ========== KONFIGURASI ==========
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "7275352971")
TG_TOKEN = os.environ.get("TG_TOKEN", "8397820382:AAHfWyFfDhp26UH4asildxt5wGQ2jZcIiFM")

# File paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(BASE_DIR, "akun.txt")
SEED_FILE = os.path.join(BASE_DIR, "seed.txt")

# ========== Helper Functions ==========
def safe_float(value, default=0.0):
    """Konversi nilai ke float dengan aman"""
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = ''.join(c for c in value if c.isdigit() or c in '.-')
            if not cleaned or cleaned in ('-', '.', '-.'):
                return default
            return float(cleaned)
        return default
    except (ValueError, TypeError, AttributeError):
        return default

def safe_string(value, default="N/A"):
    """Konversi nilai ke string dengan aman"""
    try:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip() if value.strip() else default
        return str(value) if value != "" else default
    except (TypeError, AttributeError):
        return default

# ========== File Management ==========
def load_accounts() -> List[Dict[str, str]]:
    """Load akun dari akun.txt"""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    
    try:
        accounts = []
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                
                if "=" not in line:
                    continue
                
                if "|" in line:
                    id_name, token = line.split("=", 1)
                    if "|" in id_name:
                        acc_id, name = id_name.split("|", 1)
                        accounts.append({
                            "id": acc_id.strip(), 
                            "name": name.strip(), 
                            "token": token.strip()
                        })
                    else:
                        name = id_name.strip()
                        accounts.append({
                            "id": str(line_num), 
                            "name": name, 
                            "token": token.strip()
                        })
                else:
                    name, token = line.split("=", 1)
                    accounts.append({
                        "id": str(line_num), 
                        "name": name.strip(), 
                        "token": token.strip()
                    })
        
        # Sort by ID
        accounts.sort(key=lambda x: int(x.get("id", "999")))
        return accounts
    except Exception as e:
        print(f"❌ Error loading akun.txt: {e}")
        return []

def save_accounts(accounts: List[Dict[str, str]]):
    """Save akun ke akun.txt"""
    try:
        # Ensure IDs
        for i, acc in enumerate(accounts):
            if "id" not in acc or not acc["id"]:
                acc["id"] = str(i + 1)
        
        # Sort by ID
        accounts.sort(key=lambda x: int(x.get("id", "999")))
        
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            f.write("# Format: ID|Nama=Token\n")
            f.write("# ID menentukan urutan akun\n\n")
            for acc in accounts:
                f.write(f"{acc['id']}|{acc['name']}={acc['token']}\n")
        
        print(f"✅ Saved {len(accounts)} accounts to akun.txt")
    except Exception as e:
        print(f"❌ Error saving akun.txt: {e}")

def load_seed_phrases() -> Dict[str, str]:
    """Load seed phrases dari seed.txt"""
    if not os.path.exists(SEED_FILE):
        return {}
    
    try:
        result = {}
        with open(SEED_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                
                name, seed = line.split("=", 1)
                name = name.strip()
                seed = seed.strip()
                if name and seed:
                    result[name] = seed
        
        return result
    except Exception as e:
        print(f"❌ Error loading seed.txt: {e}")
        return {}

def save_seed_phrases(seeds: Dict[str, str]):
    """Save seed phrases ke seed.txt"""
    try:
        with open(SEED_FILE, "w", encoding="utf-8") as f:
            f.write("# Format: Nama=PrivateKey\n\n")
            for name, seed in seeds.items():
                name = (name or "").strip()
                seed = (seed or "").strip()
                if name and seed:
                    f.write(f"{name}={seed}\n")
        
        print(f"✅ Saved {len(seeds)} seed phrases to seed.txt")
    except Exception as e:
        print(f"❌ Error saving seed.txt: {e}")

# ========== API Functions ==========
def get_profile(token: str) -> Dict:
    """Mendapatkan profil user"""
    state = http_manager.check_user_state(token)
    if not state.token_valid:
        return {}
    
    return {
        "wallet": state.wallet,
        "wager": state.wager,
        "wagerNeededForWithdraw": state.wagerNeededForWithdraw
    }

def get_balance(token: str) -> float:
    """Mendapatkan saldo user"""
    state = http_manager.check_user_state(token)
    return state.wallet

def get_vip(token: str) -> Dict:
    """Mendapatkan data VIP user"""
    state = http_manager.check_user_state(token, need_vip=True)
    if not state.token_valid or state.vip_level is None:
        return {}
    
    return {
        "currentLevel": {"level": state.vip_level, "name": state.vip_name},
        "nextLevel": {"wagerNeeded": state.vip_next_wager}
    }

def validate_token_requests_fast(token: str) -> Tuple[bool, str]:
    """Validasi token secara efisien"""
    if not token or not isinstance(token, str) or len(token.strip()) < 10:
        return False, "Token kosong/terlalu pendek"
    
    state = http_manager.check_user_state(token)
    if state.token_valid:
        return True, f"Token valid (source: {state.source})"
    else:
        return False, state.error or "Token tidak valid"

# ========== Telegram Functions ==========
async def send_telegram(msg: str, context=None, disable_notif: bool = False):
    """Kirim pesan ke Telegram"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": TG_CHAT_ID,
                    "text": str(msg),
                    "disable_notification": disable_notif,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
    except Exception as e:
        print(f"❌ Error sending Telegram message: {e}")

# ========== Account Management ==========
def find_account_by_name(name: str) -> Dict[str, str]:
    """Cari akun berdasarkan nama"""
    accounts = load_accounts()
    for acc in accounts:
        if acc.get("name", "").strip().lower() == name.strip().lower():
            return acc
    return {}

def update_account_token(name: str, new_token: str) -> bool:
    """Update token untuk akun tertentu"""
    try:
        accounts = load_accounts()
        updated = False
        
        for acc in accounts:
            if acc.get("name", "").strip().lower() == name.strip().lower():
                acc["token"] = new_token
                updated = True
                break
        
        if not updated:
            # Tambah akun baru jika tidak ditemukan
            new_id = str(len(accounts) + 1)
            accounts.append({"id": new_id, "name": name, "token": new_token})
            updated = True
        
        if updated:
            save_accounts(accounts)
            return True
        
        return False
    except Exception as e:
        print(f"❌ Error updating account token: {e}")
        return False