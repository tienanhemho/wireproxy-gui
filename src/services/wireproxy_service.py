import os
import sys
import shutil
import logging
import subprocess
import time
from PyQt6 import QtWidgets

LOGGER = logging.getLogger("wireproxy_gui")
PROFILE_DIR = "profiles"
LOG_DIR = "logs"
TEMP_WIREPROXY_SUFFIX = "_wireproxy.conf"

class WireProxyService:
    def __init__(self, state_service):
        self.state_service = state_service

    def is_process_running(self, pid):
        if not pid:
            return False
        try:
            if sys.platform.startswith("win"):
                import ctypes
                import ctypes.wintypes as wt

                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                STILL_ACTIVE = 259
                kernel = ctypes.windll.kernel32
                handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
                if not handle:
                    return False
                try:
                    exit_code = wt.DWORD()
                    if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return False
                    return exit_code.value == STILL_ACTIVE
                finally:
                    kernel.CloseHandle(handle)
            else:
                os.kill(int(pid), 0)
                return True
        except Exception:
            return False

    def _terminate_process(self, pid: int):
        if sys.platform.startswith("win"):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=True, capture_output=True)
                LOGGER.info(f"Successfully terminated process tree for pid={pid} with taskkill.")
                return
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                LOGGER.warning(f"taskkill failed, falling back to os.kill. Error: {e}")
        
        try:
            os.kill(int(pid), 9)
            LOGGER.info(f"Killed process pid={pid} with os.kill.")
        except Exception:
            LOGGER.exception(f"os.kill failed for pid={pid}")

    def get_wireproxy_log_path(self, profile_name: str) -> str:
        safe_name = "".join(c for c in profile_name if c.isalnum() or c in "-_")
        return os.path.join(LOG_DIR, f"wireproxy_{safe_name}.log")

    def rotate_profile_log(self, log_path: str, max_bytes: int = 2_000_000, backups: int = 2):
        try:
            if not os.path.exists(log_path):
                return
            if os.path.getsize(log_path) <= max_bytes:
                return
            for i in range(backups - 1, 0, -1):
                src = f"{log_path}.{i}"
                dst = f"{log_path}.{i+1}"
                if os.path.exists(src):
                    shutil.move(src, dst)
            if os.path.exists(log_path):
                shutil.move(log_path, f"{log_path}.1")
        except Exception:
            LOGGER.exception(f"Failed to rotate log file: {log_path}")

    def generate_wireproxy_conf(self, wg_conf, port, output_conf, proxy_type: str = "socks"):
        section = "http" if str(proxy_type).lower() == "http" else "Socks5"
        with open(output_conf, "w", encoding="utf-8") as f:
            f.write(f'WGConfig = "{wg_conf}"\n\n')
            f.write(f"[{section}]\n")
            f.write(f"BindAddress = 127.0.0.1:{port}\n")
        LOGGER.debug(f"Generated wireproxy conf for port={port}, type={proxy_type}")

    def ensure_wireproxy_path(self, parent_widget=None):
        state = self.state_service.get_state()
        path = state.get("wireproxy_path")
        if path and os.path.exists(path):
            return path
        
        guessed = shutil.which("wireproxy") or shutil.which("wireproxy.exe")
        if guessed:
            state["wireproxy_path"] = guessed
            self.state_service.save_state()
            LOGGER.info(f"Found wireproxy in PATH: {guessed}")
            return guessed

        if parent_widget:
            file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
                parent_widget, "Choose WireProxy executable", "", "Executables (wireproxy*);;All Files (*)"
            )
            if file_path and os.path.exists(file_path):
                state["wireproxy_path"] = file_path
                self.state_service.save_state()
                LOGGER.info(f"User selected wireproxy: {file_path}")
                return file_path
        
        return None

    def start_process(self, profile, port: int):
        state = self.state_service.get_state()
        wireproxy_path = self.ensure_wireproxy_path()
        if not wireproxy_path:
            # This should be handled in the UI layer before calling
            LOGGER.error("Cannot start process: WireProxy path is not configured.")
            return None

        temp_conf = os.path.join(PROFILE_DIR, f"{profile['name']}{TEMP_WIREPROXY_SUFFIX}")
        proxy_type = (state.get("proxy_type") or "socks").lower()
        self.generate_wireproxy_conf(profile["conf_path"], port, temp_conf, proxy_type)

        log_path = self.get_wireproxy_log_path(profile['name'])
        logging_enabled = bool(state.get("logging_enabled", True))
        
        try:
            if logging_enabled:
                self.rotate_profile_log(log_path)
                log_f = open(log_path, "a", encoding="utf-8")
                log_f.write(f"\n=== Launching WireProxy at {time.ctime()} ===\n")
                log_f.write(f"Cmd: {wireproxy_path} -c {temp_conf}\n")
                proc = subprocess.Popen([wireproxy_path, "-c", temp_conf], stdout=log_f, stderr=subprocess.STDOUT)
            else:
                proc = subprocess.Popen([wireproxy_path, "-c", temp_conf], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

            time.sleep(0.25) # Wait to see if it exits immediately
            if proc.poll() is not None:
                LOGGER.error(f"WireProxy exited immediately for '{profile['name']}' with code {proc.returncode}. See log: {log_path}")
                return None
            
            LOGGER.info(f"WireProxy started: pid={proc.pid}, profile='{profile['name']}', port={port}")
            return proc.pid
        except Exception as e:
            LOGGER.exception(f"Failed to start WireProxy for '{profile['name']}'")
            return None

    def stop_process(self, profile):
        pid = profile.get("pid")
        if pid and self.is_process_running(pid):
            LOGGER.info(f"Stopping wireproxy pid={pid} for profile='{profile.get('name')}'")
            self._terminate_process(pid)
        
        if profile.get("proxy_port"):
            profile["last_port"] = int(profile["proxy_port"])
        profile["pid"] = None
        profile["proxy_port"] = None
        profile["running"] = False
        self.state_service.save_state()
