import logging
import threading
import time
import socket
from PyQt6.QtCore import QObject, pyqtSignal

LOGGER = logging.getLogger("wireproxy_gui")
PORT_RANGE = (60000, 65535)

class AutoConnectService(QObject):
    progress = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, state_service, wireproxy_service):
        super().__init__()
        self.state_service = state_service
        self.wireproxy_service = wireproxy_service
        self._is_running = False
        self._lock = threading.Lock()
        self._reserved_ports = set()

    def is_running(self):
        return self._is_running

    def start(self, indices=None):
        if self._is_running:
            LOGGER.warning("Auto-connect is already running.")
            return

        self._is_running = True
        thread = threading.Thread(target=self._manager_thread, args=(indices,), daemon=True)
        thread.start()

    def _manager_thread(self, indices=None):
        try:
            state = self.state_service.get_state()
            limit = int(state.get("port_limit", 0))
            
            all_profiles = state.get("profiles", [])
            candidate_indices = indices if indices is not None else range(len(all_profiles))

            queue = []
            for idx in candidate_indices:
                if 0 <= idx < len(all_profiles):
                    profile = all_profiles[idx]
                    if not self.wireproxy_service.is_process_running(profile.get("pid")):
                        queue.append(idx)

            if not queue:
                return

            max_workers = min(4, len(queue))
            threads = []
            for _ in range(max_workers):
                t = threading.Thread(target=self._worker_thread, args=(queue, limit), daemon=True)
                t.start()
                threads.append(t)
            
            for t in threads:
                t.join()

        finally:
            self._is_running = False
            self._reserved_ports.clear()
            self.finished.emit()
            LOGGER.info("Auto-connect process finished.")

    def _worker_thread(self, queue: list, limit: int):
        while True:
            with self._lock:
                if not queue:
                    return # Queue is empty
                
                # Check connection limit
                if limit > 0:
                    used_ports = {
                        p["proxy_port"] for p in self.state_service.get_state()["profiles"]
                        if p.get("proxy_port") and self.wireproxy_service.is_process_running(p.get("pid"))
                    }
                    if len(used_ports) >= limit:
                        return # Limit reached

                idx = queue.pop(0)

            profile = self.state_service.get_state()["profiles"][idx]
            
            port = self._find_and_reserve_port(profile)
            if not port:
                continue # Could not find a port, try next profile

            pid = self.wireproxy_service.start_process(profile, port)
            
            with self._lock:
                self._reserved_ports.discard(port)

            if pid:
                profile["pid"] = pid
                profile["proxy_port"] = port
                profile["running"] = True
                self.state_service.save_state()
            
            self.progress.emit()
            time.sleep(0.1) # Small delay between connections

    def _find_and_reserve_port(self, profile):
        state = self.state_service.get_state()
        used_ports = {
            p["proxy_port"] for p in state["profiles"]
            if p.get("proxy_port") and self.wireproxy_service.is_process_running(p.get("pid"))
        }
        
        # Try last used port first
        last_port = profile.get("last_port")
        if last_port:
            with self._lock:
                if last_port not in used_ports and last_port not in self._reserved_ports and self._is_port_free_os(last_port):
                    self._reserved_ports.add(last_port)
                    return last_port

        # Scan for a new port
        start, end = PORT_RANGE
        for port in range(start, end + 1):
            with self._lock:
                if port not in used_ports and port not in self._reserved_ports and self._is_port_free_os(port):
                    self._reserved_ports.add(port)
                    return port
        return None

    def _is_port_free_os(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) != 0
