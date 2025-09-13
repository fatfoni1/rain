import time
import threading
import logging
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Konfigurasi Logging Minimalis
logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')

def _log_warn(message):
    logging.warning(message)

def _log_info(message):
    logging.info(message)

# ==============================================================================
# 1. Konfigurasi & Dataclasses
# ==============================================================================

API_HOST = "api.flip.gg"
BASE_URL = f"https://{API_HOST}"

# TTL Cache (dalam detik)
USER_CACHE_TTL = 7
VIP_CACHE_TTL = 30
NEGATIVE_CACHE_TTL = 15 # Untuk status 401/403
STALE_CACHE_TTL = 60 # Toleransi cache lama saat API gagal

@dataclass
class UserState:
    """Dataclass untuk menyimpan hasil gabungan dari status user."""
    token_valid: bool = False
    wallet: float = 0.0
    wager: float = 0.0
    wagerNeededForWithdraw: float = 0.0
    vip_level: Optional[int] = None
    vip_name: Optional[str] = None
    vip_next_wager: Optional[float] = None
    stale: bool = False
    vip_stale: bool = False
    source: str = "fallback"
    error: Optional[str] = None

@dataclass
class CacheEntry:
    """Struktur data untuk item di dalam cache."""
    data: Dict[str, Any]
    expiry: float

# ==============================================================================
# 2. Singleton HttpManager
# ==============================================================================

class HttpManager:
    """
    Mengelola sesi HTTP, rate limiting, caching, dan request coalescing untuk API.
    Didesain sebagai singleton untuk memastikan state (session, cache, dll.) terpusat.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        
        with self._lock:
            if self._initialized:
                return
            
            # 1. Session Pooling + Retry
            self._session = self._create_session()
            
            # 2. Cache (In-memory)
            self._cache: Dict[str, Dict[str, CacheEntry]] = {}
            self._cache_lock = threading.Lock()
            
            # 3. In-Flight Coalescing
            self._in_flight: Dict[Tuple[str, str], threading.Event] = {}
            self._in_flight_lock = threading.Lock()
            
            # 4. Rate Limiter (Token Bucket)
            self._rate_limiter = self._TokenBucket(capacity=3, refill_rate=3)
            
            # 5. Statistik
            self.stats = {"api_ok": 0, "cache_hit": 0, "rate_limited": 0, "server_error": 0, "auth_error": 0}
            
            self._initialized = True

    class _TokenBucket:
        """Implementasi sederhana dari token bucket algorithm."""
        
        def __init__(self, capacity: int, refill_rate: float):
            self.capacity = capacity
            self._tokens = float(capacity)
            self._refill_rate = refill_rate
            self._last_refill = time.monotonic()
            self._lock = threading.Lock()

        def _refill(self):
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self._refill_rate)
            self._last_refill = now

        def consume(self) -> None:
            with self._lock:
                self._refill()
                while self._tokens < 1:
                    # Tunggu jika bucket kosong
                    wait_time = (1 - self._tokens) / self._refill_rate
                    time.sleep(wait_time + 0.05)  # Jitter kecil
                    self._refill()
                self._tokens -= 1

    def _create_session(self) -> requests.Session:
        """Membuat session requests dengan HTTPAdapter untuk retry otomatis."""
        session = requests.Session()
        session.headers.update({
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0"
        })
        
        retries = Retry(
            total=3,
            backoff_factor=0.4,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        return session

    def _get_from_cache(self, token: str, endpoint: str) -> Optional[Dict[str, Any]]:
        with self._cache_lock:
            if token in self._cache and endpoint in self._cache[token]:
                entry = self._cache[token][endpoint]
                if time.monotonic() < entry.expiry:
                    self.stats["cache_hit"] += 1
                    return entry.data
        return None

    def _set_in_cache(self, token: str, endpoint: str, data: Dict[str, Any], ttl: int):
        with self._cache_lock:
            if token not in self._cache:
                self._cache[token] = {}
            self._cache[token][endpoint] = CacheEntry(data=data, expiry=time.monotonic() + ttl)

    def _get_stale_cache(self, token: str, endpoint: str) -> Optional[Dict[str, Any]]:
        with self._cache_lock:
            if token in self._cache and endpoint in self._cache[token]:
                entry = self._cache[token][endpoint]
                if time.monotonic() < entry.expiry + STALE_CACHE_TTL:
                    return entry.data
        return None

    def _api_call(self, method: str, url: str, headers: Dict) -> Tuple[int, Optional[Dict]]:
        self._rate_limiter.consume()
        
        try:
            response = self._session.request(method, url, headers=headers, timeout=(2, 4))
            status = response.status_code
            
            if status == 200:
                try:
                    data = response.json()
                    self.stats["api_ok"] += 1
                    return status, data
                except requests.exceptions.JSONDecodeError:
                    _log_warn(f"API returned 200 OK but with invalid JSON for URL: {url}")
                    return 0, None
            elif status in (401, 403):
                self.stats["auth_error"] += 1
                return status, None
            elif status == 429:
                self.stats["rate_limited"] += 1
                return status, None
            elif status >= 500:
                self.stats["server_error"] += 1
                return status, None
            else:
                return status, None
                
        except requests.exceptions.RequestException as e:
            _log_warn(f"Request failed after retries: {e}")
            return 0, None

    def check_user_state(self, token: str, need_vip: bool = False) -> UserState:
        if not token:
            return UserState(token_valid=False, error="Empty token")
        
        if self._get_from_cache(token, "negative"):
            return UserState(token_valid=False, source="cache", error="Auth error (cached)")
        
        state = UserState(token_valid=True)
        user_data, vip_data = None, None
        
        user_data = self._get_from_cache(token, "user")
        state.source = "cache"
        
        if not user_data:
            state.source = "api"
            user_data, state.stale = self._fetch_endpoint(token, "user", USER_CACHE_TTL)
        
        if not user_data:
            return UserState(token_valid=False, source="fallback", error="Failed to fetch user data")
        
        if need_vip:
            vip_data = self._get_from_cache(token, "vip")
            if not vip_data:
                if state.source == "api":
                    vip_data, state.vip_stale = self._fetch_endpoint(token, "vip", VIP_CACHE_TTL)
                else:
                    state.source = "mixed"
                    vip_data, state.vip_stale = self._fetch_endpoint(token, "vip", VIP_CACHE_TTL)
        
        self._populate_state(state, user_data, vip_data)
        return state

    def _fetch_endpoint(self, token: str, endpoint: str, ttl: int) -> Tuple[Optional[Dict], bool]:
        flight_key = (endpoint, token)
        
        with self._in_flight_lock:
            if flight_key in self._in_flight:
                event = self._in_flight[flight_key]
                is_leader = False
            else:
                event = threading.Event()
                self._in_flight[flight_key] = event
                is_leader = True
        
        if not is_leader:
            event.wait()
            cached_data = self._get_from_cache(token, endpoint)
            if cached_data:
                return cached_data, False
            return self._get_stale_cache(token, endpoint), True
        
        try:
            url = f"{BASE_URL}/api/{endpoint}"
            headers = {"x-auth-token": token}
            status, data = self._api_call("GET", url, headers)
            
            if status == 200 and data:
                self._set_in_cache(token, endpoint, data, ttl)
                return data, False
            elif status in (401, 403):
                self._set_in_cache(token, "negative", {"status": status}, NEGATIVE_CACHE_TTL)
                return None, False
            else:
                stale_data = self._get_stale_cache(token, endpoint)
                if stale_data:
                    _log_info(f"API call for {endpoint} failed, serving stale cache.")
                    return stale_data, True
                return None, False
        finally:
            with self._in_flight_lock:
                self._in_flight.pop(flight_key, None)
            event.set()

    def _populate_state(self, state: UserState, user_data: Dict, vip_data: Optional[Dict]):
        if not user_data or "user" not in user_data:
            state.token_valid = False
            state.error = "Invalid user data structure"
            return
        
        ud = user_data.get("user", {})
        state.wallet = float(ud.get("wallet", 0.0) or 0.0)
        state.wager = float(ud.get("wager", 0.0) or 0.0)
        state.wagerNeededForWithdraw = float(ud.get("wagerNeededForWithdraw", 0.0) or 0.0)
        
        if vip_data and "currentLevel" in vip_data:
            cl = vip_data.get("currentLevel", {})
            nl = vip_data.get("nextLevel", {})
            state.vip_level = int(cl.get("level", 0) or 0)
            state.vip_name = str(cl.get("name", "0"))
            state.vip_next_wager = float(nl.get("wagerNeeded", 0.0) or 0.0)

http_manager = HttpManager()