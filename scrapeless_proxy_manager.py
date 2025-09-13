import random
import logging
import time
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

class ScrapelessProxyManager:
    """
    Manager untuk proxy Scrapeless dengan fitur rotation dan health checking
    """
    
    def __init__(self, proxy_file: str = "proxy.txt"):
        self.proxy_file = proxy_file
        self.proxies: List[str] = []
        self.current_index = 0
        self.failed_proxies = set()
        self.last_rotation_time = 0
        self.rotation_interval = 60  # Rotate setiap 60 detik
        self.load_proxies()
    
    def load_proxies(self) -> None:
        """Load proxy dari file proxy.txt"""
        try:
            with open(self.proxy_file, 'r', encoding='utf-8') as f:
                self.proxies = [line.strip() for line in f if line.strip()]
            logger.info(f"Loaded {len(self.proxies)} proxies from {self.proxy_file}")
        except FileNotFoundError:
            logger.error(f"Proxy file {self.proxy_file} not found")
            self.proxies = []
        except Exception as e:
            logger.error(f"Error loading proxies: {e}")
            self.proxies = []
    
    def parse_proxy(self, proxy_line: str) -> Optional[Dict[str, str]]:
        """
        Parse proxy line dari format Scrapeless
        Format: username:password@host:port
        """
        try:
            if '@' not in proxy_line:
                return None
            
            auth_part, host_part = proxy_line.split('@', 1)
            username, password = auth_part.split(':', 1)
            host, port = host_part.split(':', 1)
            
            proxy_url = f"http://{username}:{password}@{host}:{port}"
            
            return {
                'http': proxy_url,
                'https': proxy_url,
                'username': username,
                'password': password,
                'host': host,
                'port': port
            }
        except Exception as e:
            logger.error(f"Error parsing proxy {proxy_line}: {e}")
            return None
    
    def get_next_proxy(self) -> Optional[Dict[str, str]]:
        """Get next proxy dengan rotation"""
        if not self.proxies:
            logger.warning("No proxies available")
            return None
        
        # Auto rotation berdasarkan waktu
        current_time = time.time()
        if current_time - self.last_rotation_time > self.rotation_interval:
            self.rotate_proxy()
            self.last_rotation_time = current_time
        
        # Skip failed proxies
        attempts = 0
        while attempts < len(self.proxies):
            proxy_line = self.proxies[self.current_index]
            
            if proxy_line not in self.failed_proxies:
                proxy_dict = self.parse_proxy(proxy_line)
                if proxy_dict:
                    logger.info(f"Using proxy: {proxy_dict['host']}:{proxy_dict['port']}")
                    return proxy_dict
            
            self.current_index = (self.current_index + 1) % len(self.proxies)
            attempts += 1
        
        logger.warning("All proxies are marked as failed")
        return None
    
    def get_random_proxy(self) -> Optional[Dict[str, str]]:
        """Get random proxy"""
        if not self.proxies:
            return None
        
        available_proxies = [p for p in self.proxies if p not in self.failed_proxies]
        if not available_proxies:
            logger.warning("No available proxies (all failed)")
            return None
        
        proxy_line = random.choice(available_proxies)
        proxy_dict = self.parse_proxy(proxy_line)
        if proxy_dict:
            logger.info(f"Using random proxy: {proxy_dict['host']}:{proxy_dict['port']}")
        return proxy_dict
    
    def rotate_proxy(self) -> None:
        """Rotate ke proxy berikutnya"""
        if self.proxies:
            self.current_index = (self.current_index + 1) % len(self.proxies)
            logger.info(f"Rotated to proxy index {self.current_index}")
    
    def mark_proxy_failed(self, proxy_dict: Dict[str, str]) -> None:
        """Mark proxy sebagai failed"""
        proxy_line = f"{proxy_dict['username']}:{proxy_dict['password']}@{proxy_dict['host']}:{proxy_dict['port']}"
        self.failed_proxies.add(proxy_line)
        logger.warning(f"Marked proxy as failed: {proxy_dict['host']}:{proxy_dict['port']}")
    
    def reset_failed_proxies(self) -> None:
        """Reset daftar failed proxies"""
        self.failed_proxies.clear()
        logger.info("Reset failed proxies list")
    
    def get_proxy_stats(self) -> Dict[str, Any]:
        """Get statistik proxy"""
        total_proxies = len(self.proxies)
        failed_count = len(self.failed_proxies)
        available_count = total_proxies - failed_count
        
        return {
            'total': total_proxies,
            'available': available_count,
            'failed': failed_count,
            'current_index': self.current_index
        }
    
    def test_proxy(self, proxy_dict: Dict[str, str], test_url: str = "http://httpbin.org/ip", timeout: int = 10) -> bool:
        """Test apakah proxy berfungsi"""
        try:
            import requests
            response = requests.get(test_url, proxies=proxy_dict, timeout=timeout)
            if response.status_code == 200:
                logger.info(f"Proxy test successful: {proxy_dict['host']}:{proxy_dict['port']}")
                return True
            else:
                logger.warning(f"Proxy test failed with status {response.status_code}: {proxy_dict['host']}:{proxy_dict['port']}")
                return False
        except Exception as e:
            logger.error(f"Proxy test error: {e}")
            return False
    
    def get_playwright_proxy_config(self, proxy_dict: Dict[str, str]) -> Dict[str, Any]:
        """Get konfigurasi proxy untuk Playwright"""
        return {
            'server': f"http://{proxy_dict['host']}:{proxy_dict['port']}",
            'username': proxy_dict['username'],
            'password': proxy_dict['password']
        }
    
    def reload_proxies(self) -> None:
        """Reload proxy dari file"""
        self.load_proxies()
        self.reset_failed_proxies()
        self.current_index = 0
        logger.info("Proxies reloaded from file")