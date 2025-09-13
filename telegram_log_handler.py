import logging
import requests
import threading
from queue import Queue, Empty

class TelegramLogHandler(logging.Handler):
    """
    Handler logging kustom yang mengirim log ke Telegram secara asynchronous.
    """
    def __init__(self, token: str, chat_id: str):
        super().__init__()
        self.token = token
        self.chat_id = chat_id
        self.log_queue = Queue()
        
        # Daftar pola yang akan diabaikan
        self.exclude_patterns = [
            "HTTP Request:",
            "[IDLE] Tidak ada active",
            "[ACTIVITY] Melakukan aktivitas random",
            "[ACTIVITY] Aktivitas",
            "[IDLE] tidur",
            "Connection pool is full",
            "Application started",
            "Application is stopping",
            "Scheduler started",
            "Scheduler has been shut down",
            "Job", # Mengabaikan log dari APScheduler
        ]

        # Worker thread untuk mengirim log
        self.worker_thread = threading.Thread(target=self._log_sender, daemon=True)
        self.worker_thread.start()

    def _log_sender(self):
        """Fungsi yang berjalan di thread terpisah untuk mengirim log."""
        while True:
            try:
                record = self.log_queue.get()
                if record is None: # Sinyal untuk berhenti
                    break
                
                message = self.format(record)
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                payload = {"chat_id": self.chat_id, "text": f"`{message}`", "parse_mode": "Markdown"}
                requests.post(url, json=payload, timeout=5)
            except Exception:
                pass # Jangan sampai worker crash

    def emit(self, record):
        """Filter dan masukkan log ke dalam antrian."""
        msg = self.format(record)
        if not any(pattern in msg for pattern in self.exclude_patterns):
            self.log_queue.put(record)