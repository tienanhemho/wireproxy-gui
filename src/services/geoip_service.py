import logging
import json
import urllib.request
import urllib.parse
import threading
from PyQt6.QtCore import QObject, pyqtSignal

LOGGER = logging.getLogger("wireproxy_gui")

class GeoIPService(QObject):
    # Signal: host, location, zip_code
    location_fetched = pyqtSignal(str, str, str)

    def __init__(self):
        super().__init__()
        self.geo_cache: dict[str, dict[str, str]] = {}
        self.geo_inflight: set[str] = set()

    def get_location(self, host: str) -> dict[str, str] | None:
        """Return cached location or start a fetch if not available."""
        if host in self.geo_cache:
            return self.geo_cache[host]
        
        if host and host not in self.geo_inflight:
            self._start_fetch(host)
        
        return None

    def _start_fetch(self, host: str):
        self.geo_inflight.add(host)
        thread = threading.Thread(target=self._fetch_worker, args=(host,), daemon=True)
        thread.start()

    def _fetch_worker(self, host: str):
        """Background worker to query ip-api.com."""
        location = "Unknown"
        zip_code = ""
        try:
            fields = "status,country,city,regionName,countryCode,zip"
            url = f"http://ip-api.com/json/{urllib.parse.quote(host)}?fields={fields}&lang=en"
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            
            if isinstance(data, dict) and data.get("status") == "success":
                city = data.get("city", "").strip()
                region = data.get("regionName", "").strip()
                ccode = data.get("countryCode", "").strip()
                country = data.get("country", "").strip()
                zip_code = data.get("zip", "").strip()
                
                parts = [p for p in [city, region, ccode] if p] or [country]
                location = ", ".join(parts) or "Unknown"
        except Exception:
            # Silently fail, UI will show "Unknown"
            pass
        finally:
            self.geo_inflight.discard(host)
            self.geo_cache[host] = {"location": location, "zip": zip_code}
            self.location_fetched.emit(host, location, zip_code)
