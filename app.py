import sys
import os
import json
import shutil
import socket
import subprocess
import time
import logging
import urllib.request
import urllib.parse
import base64
import io
from logging.handlers import RotatingFileHandler
from functools import partial
from PyQt6 import QtWidgets, QtCore, QtGui
from datetime import datetime

STATE_FILE = "state.json"
PROFILE_DIR = "profiles"
PORT_RANGE = (60000, 65535)
STATE_VERSION = 3
LOG_DIR = "logs"
TEMP_WIREPROXY_SUFFIX = "_wireproxy.conf"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

# Optional QR decoders
try:
    import cv2  # type: ignore
    _HAVE_CV2 = True
    import numpy as np  # type: ignore
except Exception:
    _HAVE_CV2 = False

try:
    from PIL import Image  # type: ignore
    from pyzbar.pyzbar import decode as pyzbar_decode  # type: ignore
    _HAVE_PYZBAR = True
except Exception:
    _HAVE_PYZBAR = False


def setup_logging() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("wireproxy_gui")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


LOGGER = setup_logging()


class WireProxyManager(QtWidgets.QMainWindow):
    # host, location, zip
    location_fetched = QtCore.pyqtSignal(str, str, str)
    auto_connect_finished = QtCore.pyqtSignal()
    auto_connect_progress = QtCore.pyqtSignal()
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WireProxy GUI Manager")
        self.resize(820, 480)
        
        # Enable drag and drop
        self.setAcceptDrops(True)

        os.makedirs(PROFILE_DIR, exist_ok=True)
        self.state = self.load_state()
        # Update logger level from state
        self.update_logger_level_from_state()

        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Profile Name", "Host", "Location", "ZIP", "Proxy Port", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)
        try:
            # Set sensible default widths
            self.table.setColumnWidth(0, 150)  # Name
            self.table.setColumnWidth(1, 150)  # Host
            self.table.setColumnWidth(2, 150)  # Location
            self.table.setColumnWidth(3, 80)   # ZIP
            self.table.setColumnWidth(4, 90)   # Port
            self.table.setColumnWidth(5, 80)  # Status
        except Exception:
            pass
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_context_menu)

        import_btn = QtWidgets.QPushButton("Drag and drop a .conf here or click to choose")
        import_btn.clicked.connect(self.import_profile)

        # Port limit + proxy type + logging row
        limit_layout = QtWidgets.QHBoxLayout()

        # Port limit
        limit_layout.addWidget(QtWidgets.QLabel("Active ports limit (0 = unlimited):"))
        self.port_limit_spin = QtWidgets.QSpinBox()
        self.port_limit_spin.setRange(0, 10000)
        self.port_limit_spin.setValue(int(self.state.get("port_limit", 10)))
        self.port_limit_spin.valueChanged.connect(self.on_port_limit_change)
        limit_layout.addWidget(self.port_limit_spin)

        # Proxy type selector
        limit_layout.addSpacing(16)
        limit_layout.addWidget(QtWidgets.QLabel("Proxy type:"))
        self.proxy_type_combo = QtWidgets.QComboBox()
        self.proxy_type_combo.addItems(["SOCKS5", "HTTP"])
        current_type = (self.state.get("proxy_type") or "socks").lower()
        self.proxy_type_combo.setCurrentIndex(0 if current_type == "socks" else 1)
        self.proxy_type_combo.currentIndexChanged.connect(self.on_proxy_type_change)
        limit_layout.addWidget(self.proxy_type_combo)

        # Logging toggle
        limit_layout.addSpacing(16)
        self.logging_checkbox = QtWidgets.QCheckBox("Logging")
        self.logging_checkbox.setChecked(bool(self.state.get("logging_enabled", True)))
        self.logging_checkbox.stateChanged.connect(self.on_logging_change)
        limit_layout.addWidget(self.logging_checkbox)

        limit_layout.addStretch(1)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(limit_layout)
        layout.addWidget(self.table)
        layout.addWidget(import_btn)

        container = QtWidgets.QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Geo cache for ip-api results: host -> {location, zip}
        self.geo_cache: dict[str, dict[str, str]] = {}
        self.geo_inflight: set[str] = set()
        self.location_fetched.connect(self._on_location_fetched)
        self.auto_connect_finished.connect(self.refresh_table)
        self.auto_connect_progress.connect(self.refresh_table)
        self._auto_connect_running = False
        self._auto_reserved_ports: set[int] = set()

        # Cleanup old temporary wireproxy files (if any)
        self.cleanup_temp_wireproxy_confs()
        self.load_profiles()

    # ==== App lifecycle ====
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore[override]
        try:
            # Terminate any running wireproxy processes we started
            for profile in list(self.state.get("profiles", [])):
                pid = profile.get("pid")
                if pid and self.is_process_running(pid):
                    try:
                        LOGGER.info(f"Shutting down wireproxy pid={pid} for profile='{profile.get('name')}' on app exit")
                        self._terminate_process(pid)
                    except Exception:
                        LOGGER.exception(f"Failed to terminate pid={pid} during shutdown")
                # Clear runtime fields
                if profile.get("proxy_port"):
                    profile["last_port"] = int(profile["proxy_port"])  # remember last
                profile["pid"] = None
                profile["proxy_port"] = None
                profile["running"] = False
            # Save state and clean temp files
            self.save_state()
            self.cleanup_temp_wireproxy_confs()
        except Exception:
            # Do not block closing due to cleanup errors
            LOGGER.exception("Error during app shutdown cleanup")
        event.accept()

    def get_wireproxy_log_path(self, profile_name: str) -> str:
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in profile_name)
        return os.path.join(LOG_DIR, f"wireproxy_{safe_name}.log")

    def rotate_profile_log(self, log_path: str, max_bytes: int = 2_000_000, backups: int = 2) -> None:
        try:
            if not os.path.exists(log_path):
                return
            size = os.path.getsize(log_path)
            if size <= max_bytes:
                return
            # Rotate: *.log -> *.log.1 -> *.log.2 (cap at backups)
            for idx in range(backups, 0, -1):
                older = f"{log_path}.{idx}"
                newer = f"{log_path}" if idx == 1 else f"{log_path}.{idx-1}"
                if os.path.exists(older):
                    try:
                        os.remove(older)
                    except Exception:
                        pass
                if os.path.exists(newer):
                    try:
                        os.rename(newer, older)
                    except Exception:
                        pass
            # Ensure new empty file will be created by writer later
        except Exception:
            pass

    def cleanup_temp_wireproxy_confs(self) -> None:
        try:
            for file in os.listdir(PROFILE_DIR):
                if file.endswith(TEMP_WIREPROXY_SUFFIX):
                    try:
                        os.remove(os.path.join(PROFILE_DIR, file))
                    except Exception:
                        pass
        except Exception:
            pass

    # ==== Import helpers (QR / URL / raw text) ====
    def _is_http_url(self, s: str) -> bool:
        try:
            p = urllib.parse.urlparse(s)
            return p.scheme in ("http", "https") and bool(p.netloc)
        except Exception:
            return False

    def _is_data_url(self, s: str) -> bool:
        return isinstance(s, str) and s.startswith("data:")

    def import_profile_text(self, name_hint: str, conf_text: str) -> bool:
        """Create a new profile from raw config text. Returns True on success."""
        if not conf_text or "[Interface]" not in conf_text:
            QtWidgets.QMessageBox.warning(self, "Invalid config", "Text does not look like a WireGuard config.")
            return False
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name_hint.strip() or "imported")
        if safe_name.endswith("_wireproxy"):
            safe_name = safe_name.rstrip("_") + "_wg"
        # ensure uniqueness
        base = safe_name or "imported"
        candidate = base
        idx = 1
        existing_names = {p["name"] for p in self.state.get("profiles", [])}
        while candidate in existing_names or os.path.exists(os.path.join(PROFILE_DIR, f"{candidate}.conf")):
            idx += 1
            candidate = f"{base}_{idx}"
        try:
            dest = os.path.join(PROFILE_DIR, f"{candidate}.conf")
            with open(dest, "w", encoding="utf-8") as f:
                f.write(conf_text)
            self.state.setdefault("profiles", []).append({
                "name": candidate,
                "conf_path": dest,
                "proxy_port": None,
                "pid": None,
                "running": False,
                "last_port": None,
            })
            self.save_state()
            return True
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to create profile: {e}")
            return False

    def _decode_qr_image_to_text(self, image_path: str) -> str | None:
        """Try to decode a QR code image file to text using OpenCV or pyzbar. Returns None if fails."""
        # Try OpenCV
        if _HAVE_CV2:
            try:
                img = cv2.imread(image_path)
                if img is not None:
                    detector = cv2.QRCodeDetector()
                    data, points, _ = detector.detectAndDecode(img)
                    if isinstance(data, str) and data.strip():
                        return data.strip()
            except Exception:
                pass
        # Try pyzbar
        if _HAVE_PYZBAR:
            try:
                with Image.open(image_path) as im:
                    results = pyzbar_decode(im)
                    for r in results:
                        data = r.data.decode("utf-8", errors="ignore").strip()
                        if data:
                            return data
            except Exception:
                pass
        return None

    def _download_url_text(self, url: str, timeout_sec: int = 20) -> tuple[str, str] | None:
        """Download text from URL. Returns (name_hint, text) or None on failure."""
        try:
            with urllib.request.urlopen(url, timeout=timeout_sec) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "").lower()
                # If it's an image, return as image
                if ctype.startswith("image/"):
                    path = urllib.parse.urlparse(url).path
                    name_hint = os.path.splitext(os.path.basename(path) or "downloaded_image")[0]
                    # Try decode QR from bytes
                    text = self._decode_qr_bytes_to_text(raw)
                    if text:
                        return (name_hint, text)
                    QtWidgets.QMessageBox.warning(self, "QR not found", "No QR code detected in the dropped image.")
                    return None
                # Else treat as text
                text = raw.decode("utf-8", errors="ignore")
                if not text.strip():
                    return None
                path = urllib.parse.urlparse(url).path
                name_hint = os.path.splitext(os.path.basename(path) or "downloaded")[0]
                return (name_hint or "downloaded", text)
        except Exception as e:
            LOGGER.exception(f"Failed to download URL {url}: {e}")
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to download: {url}\n{e}")
            return None

    def _mime_contains_acceptable_target(self, mime: QtCore.QMimeData) -> bool:
        try:
            # URLs: local conf/images or http(s) links
            if mime.hasUrls():
                for url in mime.urls():
                    lp = url.toLocalFile()
                    if lp:
                        ext = os.path.splitext(lp)[1].lower()
                        if lp.lower().endswith('.conf') or ext in IMAGE_EXTENSIONS:
                            return True
                    s = url.toString()
                    if s and (self._is_http_url(s) or self._is_data_url(s)):
                        return True
            # Some browsers provide only text
            if mime.hasText():
                t = mime.text().strip()
                if self._is_http_url(t) or self._is_data_url(t):
                    return True
        except Exception:
            pass
        return False

    def _decode_qr_bytes_to_text(self, data: bytes) -> str | None:
        # Try OpenCV first
        if _HAVE_CV2:
            try:
                arr = np.frombuffer(data, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None:
                    detector = cv2.QRCodeDetector()
                    text, points, _ = detector.detectAndDecode(img)
                    if isinstance(text, str) and text.strip():
                        return text.strip()
            except Exception:
                pass
        # Try PIL + pyzbar
        if _HAVE_PYZBAR:
            try:
                with Image.open(io.BytesIO(data)) as im:
                    results = pyzbar_decode(im)
                    for r in results:
                        t = r.data.decode("utf-8", errors="ignore").strip()
                        if t:
                            return t
            except Exception:
                pass
        return None

    def load_state(self):
        default_state = {"version": STATE_VERSION, "profiles": [], "port_limit": 10, "wireproxy_path": None, "proxy_type": "socks", "logging_enabled": True}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                    # Versioning + migration
                    if not isinstance(data, dict):
                        LOGGER.warning("state.json is not a dict. Using default_state.")
                        return default_state
                    data.setdefault("version", 0)
                    if int(data.get("version", 0) or 0) < STATE_VERSION:
                        LOGGER.info("Detected old state.json version. Migrating…")
                        data = self.migrate_state(data)
                    # Merge with defaults
                    for k, v in default_state.items():
                        data.setdefault(k, v)
                    # Ensure default keys for each profile entry
                    for p in data.get("profiles", []):
                        p.setdefault("proxy_port", None)
                        p.setdefault("pid", None)
                        p.setdefault("running", False)
                        p.setdefault("conf_path", None)
                        p.setdefault("last_port", None)
                    LOGGER.debug(f"State loaded: port_limit={data.get('port_limit')}, proxy_type={data.get('proxy_type')}, profiles={len(data.get('profiles', []))}")
                    return data
                except:
                    LOGGER.exception("Error reading state.json. Using default_state.")
                    return default_state
        return default_state

    def save_state(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def on_proxy_type_change(self, _index: int):
        selected = self.proxy_type_combo.currentText().lower()
        # Map GUI labels to wireproxy values
        self.state["proxy_type"] = "socks" if selected.startswith("socks") else "http"
        self.save_state()

    def on_logging_change(self, _state: int):
        enabled = self.logging_checkbox.isChecked()
        self.state["logging_enabled"] = bool(enabled)
        self.save_state()
        self.update_logger_level_from_state()

    def update_logger_level_from_state(self):
        enabled = bool(self.state.get("logging_enabled", True))
        logger = logging.getLogger("wireproxy_gui")
        logger.setLevel(logging.DEBUG if enabled else logging.CRITICAL)

    def migrate_state(self, data: dict) -> dict:
        """Upgrade old state to new schema version. Backs up the current file before writing."""
        try:
            if os.path.exists(STATE_FILE):
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy(STATE_FILE, f"{STATE_FILE}.bak-{ts}")
        except Exception:
            pass

        current = int(data.get("version", 0) or 0)
        # Stepwise migration until STATE_VERSION
        while current < STATE_VERSION:
            # Ví dụ nếu có thay đổi ở các version sau, xử lý tại đây
            if current < 1:
                # v1: baseline
                current = 1
                continue
            if current < 2:
                # v2: add proxy_type (default socks)
                try:
                    if not isinstance(data, dict):
                        data = {}
                    data.setdefault("proxy_type", "socks")
                except Exception:
                    pass
                current = 2
                continue
            if current < 3:
                # v3: add logging_enabled (default True)
                try:
                    if not isinstance(data, dict):
                        data = {}
                    data.setdefault("logging_enabled", True)
                except Exception:
                    pass
                current = 3
                continue
            # An toàn: nếu không có rule cụ thể, thoát vòng lặp
            break

        data["version"] = STATE_VERSION
        # Persist migrated state to disk
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return data

    def on_port_limit_change(self, value: int):
        self.state["port_limit"] = int(value)
        self.save_state()

    # ==== WireProxy path helpers ====
    def ensure_wireproxy_path(self) -> str | None:
        """Ensure a valid wireproxy path: prefer saved path, then search PATH, otherwise ask the user to choose."""
        # 1) saved in state
        path = self.state.get("wireproxy_path")
        if path and os.path.exists(path):
            LOGGER.debug(f"Dùng wireproxy từ state: {path}")
            return path
        # 2) search in PATH
        guessed = shutil.which("wireproxy") or shutil.which("wireproxy.exe")
        if guessed and os.path.exists(guessed):
            self.state["wireproxy_path"] = guessed
            self.save_state()
            LOGGER.info(f"Tìm thấy wireproxy trong PATH: {guessed}")
            return guessed
        # 3) ask the user to pick
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Choose WireProxy executable",
            "",
            "WireProxy Executable (wireproxy*);;All Files (*)",
        )
        if not file_path:
            QtWidgets.QMessageBox.warning(self, "Missing WireProxy", "Please select the WireProxy executable to continue.")
            return None
        if not os.path.exists(file_path):
            QtWidgets.QMessageBox.critical(self, "Error", "Invalid WireProxy path.")
            return None
        self.state["wireproxy_path"] = file_path
        self.save_state()
        LOGGER.info(f"User selected wireproxy: {file_path}")
        return file_path

    def choose_wireproxy_path(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Chọn file thực thi WireProxy",
            "",
            "WireProxy Executable (wireproxy*);;All Files (*)",
        )
        if not file_path:
            return
        if not os.path.exists(file_path):
            QtWidgets.QMessageBox.critical(self, "Lỗi", "Đường dẫn WireProxy không hợp lệ.")
            return
        self.state["wireproxy_path"] = file_path
        self.save_state()
        QtWidgets.QMessageBox.information(self, "Saved", f"WireProxy path set: {file_path}")

    # ==== Port helpers ====
    def get_allowed_ports(self):
        limit = int(self.state.get("port_limit", 0))
        if limit and limit > 0:
            end = min(PORT_RANGE[0] + limit - 1, PORT_RANGE[1])
            return range(PORT_RANGE[0], end + 1)
        # No limit → entire range
        return range(PORT_RANGE[0], PORT_RANGE[1] + 1)

    def get_ports_in_use(self):
        ports = []
        for p in self.state["profiles"]:
            if self.is_process_running(p.get("pid")) and p.get("proxy_port"):
                ports.append(int(p["proxy_port"]))
        LOGGER.debug(f"Ports in use (app): {ports}")
        return set(ports)

    def is_port_free_os(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            free = s.connect_ex(("127.0.0.1", int(port))) != 0
            LOGGER.debug(f"OS port check {port}: {'free' if free else 'busy'}")
            return free

    def iter_available_ports_quick(self):
        """Trả về generator port khả dụng theo giới hạn, chỉ loại trừ các port đã dùng trong app.
        Không quét hệ thống để tránh đứng UI."""
        used = self.get_ports_in_use()
        for p in self.get_allowed_ports():
            if p not in used:
                yield p

    def get_available_ports_for_menu(self, max_items: int = 50):
        """Danh sách port dùng để hiển thị menu (nhanh, giới hạn số lượng)."""
        ports = []
        for p in self.iter_available_ports_quick():
            ports.append(p)
            if len(ports) >= max_items:
                break
        return ports

    def get_ports_for_menu(self, max_items: int = 50):
        """Return a quick list of (port, used_by_app) within the allowed limit, up to max_items.
        Does NOT scan the OS; only marks ports used by app-managed profiles.
        """
        ports: list[tuple[int, bool]] = []
        used = self.get_ports_in_use()
        for p in self.get_allowed_ports():
            ports.append((int(p), int(p) in used))
            if len(ports) >= max_items:
                break
        return ports

    def find_free_port(self):
        # Nếu có giới hạn và đã dùng đủ, trả None ngay để tránh quét
        limit = int(self.state.get("port_limit", 0))
        if limit and len(self.get_ports_in_use()) >= limit:
            return None
        # Duyệt nhanh các port khả dụng theo giới hạn, chỉ kiểm tra OS khi cần
        for port in self.iter_available_ports_quick():
            if self.is_port_free_os(port):
                return port
        return None

    def load_profiles(self):
        # Scan profiles/ and state.json
        profiles = []
        for file in os.listdir(PROFILE_DIR):
            if file.endswith(".conf"):
                # Bỏ qua file tạm tạo cho wireproxy
                if file.endswith(TEMP_WIREPROXY_SUFFIX):
                    continue
                name = os.path.splitext(file)[0]
                if name.endswith("_wireproxy"):
                    continue
                existing = next((p for p in self.state["profiles"] if p["name"] == name), None)
                if not existing:
                    profiles.append({
                        "name": name,
                        "conf_path": os.path.join(PROFILE_DIR, file),
                        "proxy_port": None,
                        "pid": None,
                        "running": False,
                        "last_port": None,
                    })
        self.state["profiles"].extend(profiles)
        self.refresh_table()

    def refresh_table(self):
        try:
            self.table.setRowCount(len(self.state["profiles"]))
            for row, profile in enumerate(self.state["profiles"]):
                # Name
                self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(profile["name"]))
                # Host (IP/Domain)
                host = self.get_profile_host(profile) or "—"
                self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(host))
                # Location + ZIP (via ip-api.com)
                if host != "—":
                    info = self.geo_cache.get(host)
                    if info:
                        location = info.get("location") or "Unknown"
                        zip_code = info.get("zip") or "—"
                    else:
                        location = "Loading…"
                        zip_code = "Loading…"
                        self._start_location_fetch(host)
                else:
                    location = "—"
                    zip_code = "—"
                self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(location))
                self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(zip_code))
                # Proxy port
                self.table.setItem(
                    row,
                    4,
                    QtWidgets.QTableWidgetItem(str(profile["proxy_port"]) if profile.get("proxy_port") else "—"),
                )
                # Status
                status_text = "Đang chạy" if self.is_process_running(profile.get("pid")) else "Chưa chạy"
                self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(status_text))
            self.save_state()
        except KeyboardInterrupt:
            LOGGER.warning("Refresh table interrupted by user")
        except Exception:
            LOGGER.exception("Lỗi khi refresh_table")

    def get_profile_host(self, profile) -> str | None:
        """Parse the WireGuard .conf to extract Endpoint host (IP/Domain) from [Peer]."""
        try:
            conf_path = profile.get("conf_path")
            if not conf_path or not os.path.exists(conf_path):
                return None
            host_cached = profile.get("_host_cache")
            if host_cached:
                return host_cached
            with open(conf_path, "r", encoding="utf-8") as f:
                in_peer = False
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or line.startswith(";"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        section = line[1:-1].strip().lower()
                        in_peer = (section == "peer")
                        continue
                    if in_peer and line.lower().startswith("endpoint"):
                        # Format: Endpoint = host:port  OR Endpoint= [IPv6]:port
                        try:
                            _, _, value = line.partition("=")
                            value = value.strip()
                            # Remove comments at end of line
                            for sep in (" #", " ;"):
                                if sep in value:
                                    value = value.split(sep, 1)[0].strip()
                            # IPv6 in brackets
                            host: str
                            if value.startswith("["):
                                end = value.find("]")
                                if end > 1:
                                    host = value[1:end]
                                else:
                                    host = value
                            else:
                                # split last ':' as port separator
                                if ":" in value:
                                    host = value.rsplit(":", 1)[0].strip()
                                else:
                                    host = value
                            # Cache on profile to avoid re-reading
                            profile["_host_cache"] = host
                            return host
                        except Exception:
                            break
            return None
        except Exception:
            return None

    def _start_location_fetch(self, host: str) -> None:
        try:
            if not host or host in self.geo_inflight or host in self.geo_cache:
                return
            self.geo_inflight.add(host)
            import threading
            t = threading.Thread(target=self._location_fetch_worker, args=(host,), daemon=True)
            t.start()
        except Exception:
            pass

    def _location_fetch_worker(self, host: str) -> None:
        """Background worker to query ip-api.com for host location."""
        location = "Unknown"
        zip_code = ""
        try:
            # Free plan uses http (no SSL). Query can be IP or domain.
            fields = "status,country,city,regionName,countryCode,zip"
            url = f"http://ip-api.com/json/{urllib.parse.quote(host)}?fields={fields}&lang=en"
            with urllib.request.urlopen(url, timeout=6) as resp:
                data = resp.read()
            info = json.loads(data.decode("utf-8", errors="ignore")) if data else {}
            if isinstance(info, dict) and str(info.get("status")).lower() == "success":
                city = (info.get("city") or "").strip()
                region = (info.get("regionName") or "").strip()
                ccode = (info.get("countryCode") or "").strip()
                country = (info.get("country") or "").strip()
                zip_code = (info.get("zip") or "").strip()
                parts = []
                if city:
                    parts.append(city)
                elif region:
                    parts.append(region)
                if ccode:
                    parts.append(ccode)
                elif country:
                    parts.append(country)
                location = ", ".join([p for p in parts if p]) or (country or "Unknown")
        except Exception:
            pass
        finally:
            try:
                self.location_fetched.emit(host, location, zip_code or "")
            except Exception:
                pass

    def _on_location_fetched(self, host: str, location: str, zip_code: str) -> None:
        try:
            self.geo_inflight.discard(host)
            self.geo_cache[host] = {"location": location or "Unknown", "zip": zip_code or ""}
            # Update all rows where host matches
            row_count = self.table.rowCount()
            for row in range(row_count):
                item = self.table.item(row, 1)
                if item and item.text() == host:
                    self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(self.geo_cache[host]["location"]))
                    self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(self.geo_cache[host]["zip"] or "—"))
        except Exception:
            pass

    def is_process_running(self, pid):
        if not pid:
            return False
        try:
            if sys.platform.startswith("win"):
                # Windows: dùng WinAPI để kiểm tra thay vì os.kill (tránh terminate process)
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
                # POSIX: tín hiệu 0 để kiểm tra tồn tại
                os.kill(int(pid), 0)
                return True
        except Exception:
            return False

    # ==== Context menu ====
    def on_table_context_menu(self, pos: QtCore.QPoint):
        try:
            menu = QtWidgets.QMenu(self)
            index = self.table.indexAt(pos)
            row = index.row() if index.isValid() else -1

            # Khi click trên một hàng
            if row >= 0:
                profile = self.state["profiles"][row]
                running = self.is_process_running(profile.get("pid"))

                if running:
                    act_disconnect = menu.addAction("Disconnect")
                    act_disconnect.triggered.connect(partial(self.toggle_connection, row))
                else:
                    act_connect = menu.addAction("Connect")
                    act_connect.triggered.connect(partial(self.toggle_connection, row))

                    # Advanced: pick port (show used ones too)
                    advanced = menu.addMenu("Connect (pick port)")
                    quick_ports = self.get_ports_for_menu(max_items=50)
                    if not quick_ports:
                        disabled = advanced.addAction("No ports within current limit")
                        disabled.setEnabled(False)
                    else:
                        for p, is_used in quick_ports:
                            label = f"127.0.0.1:{p}" + ("  (using)" if is_used else "")
                            action = advanced.addAction(label)
                            action.triggered.connect(partial(self.connect_profile_with_port_row, row, int(p)))
                        if len(quick_ports) >= 50:
                            more = advanced.addAction("… more, enter port …")
                            more.triggered.connect(partial(self.prompt_and_connect_port_row, row))

                menu.addSeparator()
                act_edit = menu.addAction("Sửa")
                act_edit.triggered.connect(partial(self.edit_profile, row))

                act_delete = menu.addAction("Xóa")
                act_delete.triggered.connect(partial(self.delete_profile, row))
                # Auto connect helpers
                act_from_here = menu.addAction("Auto connect từ hàng này (tối đa theo giới hạn)")
                act_from_here.triggered.connect(partial(self.auto_connect_from_row, row))
            else:
                # Không nằm trên hàng nào → menu chung
                act_auto = menu.addAction("Tự động kết nối theo giới hạn")
                act_auto.triggered.connect(self.auto_connect_up_to_limit)
                act_range = menu.addAction("Auto connect: chọn phạm vi…")
                act_range.triggered.connect(self.auto_connect_range_prompt)
                menu.addSeparator()
                # act_range intentionally removed from top-level context menu to reduce redundancy
                act_import = menu.addAction("Import profile (.conf)")
                act_import.triggered.connect(self.import_profile)
                act_cfg = menu.addAction("Cấu hình đường dẫn WireProxy…")
                act_cfg.triggered.connect(self.choose_wireproxy_path)
                act_logs = menu.addAction("Mở thư mục logs…")
                act_logs.triggered.connect(self.open_logs_folder)

            # Prevent right-click from causing KeyboardInterrupt to affect UI
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.ArrowCursor)
            try:
                menu.exec(self.table.viewport().mapToGlobal(pos))
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                LOGGER.warning("on_table_context_menu got KeyboardInterrupt")
                return
            LOGGER.exception("Error displaying context menu")
            QtWidgets.QMessageBox.critical(self, "Error", "An error occurred while showing the context menu. Check logs/app.log for details.")

    def prompt_and_connect_port_row(self, row: int):
        port_str, ok = QtWidgets.QInputDialog.getText(self, "Chọn port", "Nhập port trong giới hạn:")
        if not ok or not port_str.strip():
            return
        try:
            port = int(port_str)
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Sai định dạng", "Port phải là số.")
            return
        self.connect_profile_with_port_row(row, port)

    def connect_profile_with_port_row(self, row: int, port: int):
        profile = self.state["profiles"][row]
        self.connect_profile_with_port(profile, port)
        self.refresh_table()

    def auto_connect_up_to_limit(self):
        # Run in background to avoid blocking UI
        if self._auto_connect_running:
            return
        self._auto_connect_running = True
        try:
            import threading
            t = threading.Thread(target=self._auto_connect_manager, args=(None, None), daemon=True)
            t.start()
        except Exception:
            self._auto_connect_running = False

    # ==== Connect/Disconnect ====
    def toggle_connection(self, row):
        try:
            profile = self.state["profiles"][row]
            if self.is_process_running(profile.get("pid")):
                self.disconnect_profile(profile)
            else:
                self.connect_profile(profile)
            self.refresh_table()
        except KeyboardInterrupt:
            LOGGER.warning("toggle_connection interrupted by user")
        except Exception:
            LOGGER.exception("Error in toggle_connection")
            QtWidgets.QMessageBox.critical(self, "Error", "An error occurred during connect/disconnect. Check logs/app.log for details.")

    def _auto_connect_manager(self, indices: list[int] | None, start_port: int | None) -> None:
        import threading
        queue: list[int] = []
        lock = threading.Lock()
        try:
            limit = int(self.state.get("port_limit", 0))
            # Prepare candidate indices
            iterable = list(range(len(self.state["profiles"]))) if not indices else list(indices)
            for idx in iterable:
                profile = self.state["profiles"][idx]
                if self.is_process_running(profile.get("pid")):
                    continue
                conf_path = profile.get("conf_path")
                if not conf_path or not os.path.exists(conf_path):
                    continue
                queue.append(idx)
            if not queue:
                return
            max_workers = max(1, min(4, len(queue)))
            # Shared next_port when start_port is provided
            next_port_ref = {"value": int(start_port) if start_port else None}
            # Compute allowed end port consistent with get_allowed_ports()
            if limit and limit > 0:
                end_port = min(PORT_RANGE[0] + limit - 1, PORT_RANGE[1])
            else:
                end_port = PORT_RANGE[1]
            threads: list[threading.Thread] = []
            for _ in range(max_workers):
                t = threading.Thread(
                    target=self._auto_connect_pool_worker,
                    args=(queue, lock, limit, next_port_ref, end_port),
                    daemon=True,
                )
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        finally:
            try:
                self.auto_connect_finished.emit()
            except Exception:
                pass
            self._auto_reserved_ports.clear()
            self._auto_connect_running = False

    def auto_connect_range_prompt(self) -> None:
        try:
            row_count = self.table.rowCount()
            if row_count <= 0:
                return
            # Simple dialog with two spin boxes
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Chọn phạm vi hàng (1-based)")
            layout = QtWidgets.QFormLayout(dlg)
            sp_start = QtWidgets.QSpinBox(dlg)
            sp_end = QtWidgets.QSpinBox(dlg)
            sp_start.setRange(1, row_count)
            sp_end.setRange(1, row_count)
            sp_start.setValue(1)
            sp_end.setValue(row_count)
            layout.addRow("Bắt đầu", sp_start)
            layout.addRow("Kết thúc", sp_end)
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            )
            layout.addWidget(buttons)
            buttons.accepted.connect(dlg.accept)
            buttons.rejected.connect(dlg.reject)
            if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return
            start = int(min(sp_start.value(), sp_end.value()))
            end = int(max(sp_start.value(), sp_end.value()))
            indices = list(range(start - 1, end))
            self.auto_connect_with_indices(indices)
        except Exception:
            LOGGER.exception("auto_connect_range_prompt failed")

    def auto_connect_with_indices(self, indices: list[int]) -> None:
        if self._auto_connect_running:
            return
        self._auto_connect_running = True
        try:
            import threading
            t = threading.Thread(target=self._auto_connect_manager, args=(indices, None), daemon=True)
            t.start()
        except Exception:
            self._auto_connect_running = False

    def auto_connect_from_row(self, row: int) -> None:
        try:
            total = len(self.state.get("profiles", []))
            if total == 0:
                return
            # Build indices from 'row' forward to the end; pool/limit will cap actual count
            indices: list[int] = list(range(row, total))
            # Respect starting port = beginning of allowed range
            start_port = PORT_RANGE[0]
            if int(self.state.get("port_limit", 0)):
                start_port = PORT_RANGE[0]
            import threading
            if self._auto_connect_running:
                return
            self._auto_connect_running = True
            t = threading.Thread(target=self._auto_connect_manager, args=(indices, start_port), daemon=True)
            t.start()
        except Exception:
            LOGGER.exception("auto_connect_from_row failed")

    def _auto_connect_pool_worker(self, queue: list[int], lock, limit: int, next_port_ref: dict, end_port: int) -> None:
        while True:
            try:
                # Take next index
                with lock:
                    # Check limit under lock to reduce race
                    if limit and len(self.get_ports_in_use()) >= limit:
                        return
                    if not queue:
                        return
                    # FIFO to preserve order so the starting row gets the first port
                    idx = queue.pop(0)
                profile = self.state["profiles"][idx]
                # Check again
                if self.is_process_running(profile.get("pid")):
                    continue
                conf_path = profile.get("conf_path")
                if not conf_path or not os.path.exists(conf_path):
                    continue
                # Pick and reserve a port
                port = None
                # Strategy 1: sequential from provided start_port
                if next_port_ref.get("value") is not None:
                    while True:
                        with lock:
                            if limit and len(self.get_ports_in_use()) >= limit:
                                return
                            candidate = int(next_port_ref["value"])
                            if candidate > int(end_port):
                                return
                            # Advance pointer for next consumer
                            next_port_ref["value"] = candidate + 1
                        # Check availability outside lock
                        if candidate in self._auto_reserved_ports:
                            continue
                        if candidate in self.get_ports_in_use():
                            continue
                        if not self.is_port_free_os(candidate):
                            continue
                        with lock:
                            if candidate in self._auto_reserved_ports:
                                continue
                            self._auto_reserved_ports.add(candidate)
                            port = candidate
                            break
                else:
                    port = self.pick_port_for_profile(profile)
                    if not port:
                        # No more ports available within limit
                        return
                    with lock:
                        if limit and len(self.get_ports_in_use()) >= limit:
                            return
                        if int(port) in self._auto_reserved_ports:
                            # try next later by re-queuing
                            queue.insert(0, idx)
                            continue
                        self._auto_reserved_ports.add(int(port))
                ok = self._connect_profile_with_port_silent(profile, int(port))
                # Release reservation
                with lock:
                    self._auto_reserved_ports.discard(int(port))
                # Notify UI progress regardless of success
                try:
                    self.auto_connect_progress.emit()
                except Exception:
                    pass
                # Continue to next item
                continue
            except Exception:
                LOGGER.exception("auto_connect pool worker failed")
                try:
                    self.auto_connect_progress.emit()
                except Exception:
                    pass
                continue

    def _connect_profile_with_port_silent(self, profile, port: int) -> bool:
        try:
            # Validate limit range
            allowed = set(self.get_allowed_ports())
            if port not in allowed:
                LOGGER.warning(f"Out-of-limit port {port} for '{profile.get('name')}'")
                return False
            # Do not override other app profile; skip if in use
            for p in self.state["profiles"]:
                if p is profile:
                    continue
                try:
                    if int(p.get("proxy_port") or 0) == int(port) and self.is_process_running(p.get("pid")):
                        LOGGER.info(f"Port {port} already used by '{p.get('name')}', skip '{profile.get('name')}'")
                        return False
                except Exception:
                    pass
            # OS busy?
            if not self.is_port_free_os(port):
                LOGGER.info(f"Port {port} busy by external process, skip '{profile.get('name')}'")
                return False
            if port in self.get_ports_in_use():
                LOGGER.info(f"Port {port} appears in in-use set, skip '{profile.get('name')}'")
                return False
            # Get wireproxy path without dialogs
            wireproxy_path = self.get_wireproxy_path_noninteractive()
            if not wireproxy_path:
                LOGGER.error("WireProxy path not configured. Configure it from the menu before auto connect.")
                return False
            # Generate temp conf and start
            temp_conf = os.path.join(PROFILE_DIR, f"{profile['name']}{TEMP_WIREPROXY_SUFFIX}")
            proxy_type = (self.state.get("proxy_type") or "socks").lower()
            self.generate_wireproxy_conf(profile["conf_path"], port, temp_conf, proxy_type)
            log_path = self.get_wireproxy_log_path(profile['name'])
            try:
                if bool(self.state.get("logging_enabled", True)):
                    self.rotate_profile_log(log_path)
                    with open(log_path, "a", encoding="utf-8") as log_f:
                        log_f.write("\n=== Launch wireproxy (auto) ===\n")
                        log_f.write(f"cmd: {wireproxy_path} -c {temp_conf}\n")
                        log_f.write(f"proxy_type: {proxy_type}\n")
                LOGGER.info(f"Auto starting wireproxy for '{profile['name']}' on 127.0.0.1:{port} ({proxy_type})")
                if bool(self.state.get("logging_enabled", True)):
                    log_f = open(log_path, "a", encoding="utf-8")
                    try:
                        proc = subprocess.Popen([wireproxy_path, "-c", temp_conf], stdout=log_f, stderr=subprocess.STDOUT)
                    finally:
                        try:
                            log_f.close()
                        except Exception:
                            pass
                else:
                    proc = subprocess.Popen([wireproxy_path, "-c", temp_conf], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                time.sleep(0.25)
                if proc.poll() is not None:
                    LOGGER.error(f"wireproxy exited immediately for '{profile['name']}' (auto), code={proc.returncode}")
                    return False
            except Exception:
                LOGGER.exception(f"Failed to auto start WireProxy for '{profile['name']}'")
                return False
            # Update state
            profile["proxy_port"] = int(port)
            profile["last_port"] = int(port)
            profile["pid"] = proc.pid
            profile["running"] = True
            LOGGER.info(f"wireproxy started (auto): pid={proc.pid}, profile='{profile['name']}', port={port}")
            return True
        except Exception:
            LOGGER.exception("_connect_profile_with_port_silent failed")
            return False

    def get_wireproxy_path_noninteractive(self) -> str | None:
        try:
            # 1) saved path
            path = self.state.get("wireproxy_path")
            if path and os.path.exists(path):
                return path
            # 2) PATH lookup
            guessed = shutil.which("wireproxy") or shutil.which("wireproxy.exe")
            if guessed and os.path.exists(guessed):
                self.state["wireproxy_path"] = guessed
                self.save_state()
                LOGGER.info(f"Found wireproxy in PATH: {guessed}")
                return guessed
        except Exception:
            pass
        return None

    def pick_port_for_profile(self, profile):
        """Prefer using last_port if valid; otherwise find a new free port."""
        last_port = profile.get("last_port") or profile.get("proxy_port")
        if last_port:
            allowed = set(self.get_allowed_ports())
            if last_port in allowed and self.is_port_free_os(int(last_port)) and int(last_port) not in self.get_ports_in_use():
                return int(last_port)
        return self.find_free_port()

    def connect_profile_with_port(self, profile, port: int):
        # Ensure configuration exists
        if not self.ensure_profile_conf_exists(profile):
            return
        # Kiểm tra port thuộc giới hạn và còn trống
        allowed = set(self.get_allowed_ports())
        if port not in allowed:
            QtWidgets.QMessageBox.warning(self, "Out of limit", f"Port {port} is not within the current limit.")
            LOGGER.warning(f"Out-of-limit request: port={port}")
            return
        # Handle override: if port used by another app-managed profile → confirm and disconnect it
        # 1) Find other profile using this port (within app)
        other_profile_using_port = None
        for p in self.state["profiles"]:
            if p is profile:
                continue
            try:
                if int(p.get("proxy_port") or 0) == int(port) and self.is_process_running(p.get("pid")):
                    other_profile_using_port = p
                    break
            except Exception:
                pass
        # 2) If another profile uses it → ask to override
        if other_profile_using_port is not None:
            reply = QtWidgets.QMessageBox.question(
                self,
                "Override port",
                f"Port {port} is being used by profile '{other_profile_using_port['name']}'.\n"
                "Do you want to disconnect it and use this port?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            # Disconnect the old profile
            self.disconnect_profile(other_profile_using_port)
            # Re-check: if still busy it is an external process
            if not self.is_port_free_os(port):
                QtWidgets.QMessageBox.critical(self, "Busy port", f"Cannot use port {port} because another process is using it.")
                LOGGER.error(f"Override failed: port {port} busy by external process")
                return
        else:
            # No app profile using it; if OS says busy → external process, block
            if not self.is_port_free_os(port):
                QtWidgets.QMessageBox.warning(self, "Busy port", f"Port {port} is used by another process.")
                LOGGER.warning(f"Port {port} busy by external process")
                return
            # Defensive: if appears in get_ports_in_use, block
            if port in self.get_ports_in_use():
                QtWidgets.QMessageBox.warning(self, "Busy port", f"Port {port} is in use.")
                LOGGER.warning(f"Port {port} appears in in-use set (unexpected)")
                return
        # Đảm bảo có wireproxy
        wireproxy_path = self.ensure_wireproxy_path()
        if not wireproxy_path:
            return
        try:
            temp_conf = os.path.join(PROFILE_DIR, f"{profile['name']}{TEMP_WIREPROXY_SUFFIX}")
            proxy_type = (self.state.get("proxy_type") or "socks").lower()
            self.generate_wireproxy_conf(profile["conf_path"], port, temp_conf, proxy_type)
            log_path = self.get_wireproxy_log_path(profile['name'])
            if bool(self.state.get("logging_enabled", True)):
                # Rotate profile log if too large
                self.rotate_profile_log(log_path)
                with open(log_path, "a", encoding="utf-8") as log_f:
                    log_f.write("\n=== Launch wireproxy ===\n")
                    log_f.write(f"cmd: {wireproxy_path} -c {temp_conf}\n")
                    log_f.write(f"proxy_type: {proxy_type}\n")
            LOGGER.info(f"Starting wireproxy for '{profile['name']}' on 127.0.0.1:{port} ({proxy_type}), log={log_path}")
            if bool(self.state.get("logging_enabled", True)):
                log_f = open(log_path, "a", encoding="utf-8")
                try:
                    proc = subprocess.Popen([wireproxy_path, "-c", temp_conf], stdout=log_f, stderr=subprocess.STDOUT)
                finally:
                    try:
                        log_f.close()
                    except Exception:
                        pass
            else:
                proc = subprocess.Popen([wireproxy_path, "-c", temp_conf], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            # Short wait to detect early exit
            time.sleep(0.25)
            if proc.poll() is not None:
                LOGGER.error(f"wireproxy exited immediately for '{profile['name']}' with code {proc.returncode}")
                QtWidgets.QMessageBox.critical(self, "WireProxy error", f"WireProxy exited immediately (code {proc.returncode}). See log: {log_path}")
                return
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to start WireProxy: {e}")
            LOGGER.exception(f"Failed to start WireProxy for '{profile['name']}'")
            return
        profile["proxy_port"] = int(port)
        profile["last_port"] = int(port)
        profile["pid"] = proc.pid
        profile["running"] = True
        LOGGER.info(f"wireproxy started: pid={proc.pid}, profile='{profile['name']}', port={port}")

    def connect_profile(self, profile):
        # Ensure configuration exists; allow relink or delete if missing
        if not self.ensure_profile_conf_exists(profile):
            return
        
        port = self.pick_port_for_profile(profile)
        if not port:
            QtWidgets.QMessageBox.critical(self, "Lỗi", "Không còn port trống trong giới hạn!")
            return
        # Đảm bảo có wireproxy
        wireproxy_path = self.ensure_wireproxy_path()
        if not wireproxy_path:
            return

        temp_conf = os.path.join(PROFILE_DIR, f"{profile['name']}{TEMP_WIREPROXY_SUFFIX}")
        proxy_type = (self.state.get("proxy_type") or "socks").lower()
        self.generate_wireproxy_conf(profile["conf_path"], port, temp_conf, proxy_type)

        try:
            log_path = self.get_wireproxy_log_path(profile['name'])
            if bool(self.state.get("logging_enabled", True)):
                self.rotate_profile_log(log_path)
                with open(log_path, "a", encoding="utf-8") as log_f:
                    log_f.write("\n=== Launch wireproxy ===\n")
                    log_f.write(f"cmd: {wireproxy_path} -c {temp_conf}\n")
                    log_f.write(f"proxy_type: {proxy_type}\n")
            LOGGER.info(f"Starting wireproxy for '{profile['name']}' on 127.0.0.1:{port} ({proxy_type}), log={log_path}")
            if bool(self.state.get("logging_enabled", True)):
                log_f = open(log_path, "a", encoding="utf-8")
                try:
                    proc = subprocess.Popen([wireproxy_path, "-c", temp_conf], stdout=log_f, stderr=subprocess.STDOUT)
                finally:
                    try:
                        log_f.close()
                    except Exception:
                        pass
            else:
                proc = subprocess.Popen([wireproxy_path, "-c", temp_conf], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            time.sleep(0.25)
            if proc.poll() is not None:
                LOGGER.error(f"wireproxy exited immediately for '{profile['name']}' with code {proc.returncode}")
                QtWidgets.QMessageBox.critical(self, "WireProxy error", f"WireProxy exited immediately (code {proc.returncode}). See log: {log_path}")
                return
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to start WireProxy: {e}")
            LOGGER.exception(f"Failed to start WireProxy for '{profile['name']}'")
            return

        profile["proxy_port"] = int(port)
        profile["last_port"] = int(port)
        profile["pid"] = proc.pid
        profile["running"] = True

    def disconnect_profile(self, profile):
        pid = profile.get("pid")
        if pid and self.is_process_running(pid):
            try:
                LOGGER.info(f"Killing wireproxy pid={pid} for profile='{profile.get('name')}'")
                self._terminate_process(pid)
            except Exception:
                LOGGER.exception(f"Lỗi khi kill pid={pid}")
        # Save last_port before clearing proxy_port
        if profile.get("proxy_port"):
            profile["last_port"] = int(profile["proxy_port"])
        profile["pid"] = None
        profile["proxy_port"] = None
        profile["running"] = False

    def generate_wireproxy_conf(self, wg_conf, port, output_conf, proxy_type: str = "socks"):
        # According to whyvl/wireproxy: use WGConfig + [Socks5] or [http]
        section = "http" if str(proxy_type).lower() == "http" else "Socks5"
        with open(output_conf, "w", encoding="utf-8") as f:
            f.write(f"WGConfig = {wg_conf}\n\n")
            f.write(f"[{section}]\n")
            f.write(f"BindAddress = 127.0.0.1:{port}\n")
        try:
            with open(output_conf, "r", encoding="utf-8") as rf:
                content_preview = rf.read()
            LOGGER.debug(f"Generated wireproxy conf (whyvl format) for port={port}, type={proxy_type}:\n{content_preview}")
        except Exception:
            pass

    def dragEnterEvent(self, event):
        """Handle when files are dragged into the window"""
        mime = event.mimeData()
        # Accept .conf, images, and http(s) URLs
        if self._mime_contains_acceptable_target(mime):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        """Handle when dragged files move within the window"""
        if self._mime_contains_acceptable_target(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """Handle when files are dropped into the window"""
        if event.mimeData().hasUrls():
            files_imported = 0
            files_skipped = 0

            for url in event.mimeData().urls():
                # 1) Local files (conf or image)
                local_path = url.toLocalFile()
                if local_path:
                    try:
                        lp = local_path
                        if lp.lower().endswith('.conf') and os.path.exists(lp):
                            success = self.import_profile_file(lp)
                            files_imported += 1 if success else 0
                            files_skipped += 0 if success else 1
                            continue
                        # QR image
                        if os.path.splitext(lp)[1].lower() in IMAGE_EXTENSIONS and os.path.exists(lp):
                            data_text = self._decode_qr_image_to_text(lp)
                            if data_text:
                                # If QR encodes a URL, try to download
                                if self._is_http_url(data_text):
                                    dl = self._download_url_text(data_text)
                                    if dl:
                                        name_hint, text = dl
                                        success = self.import_profile_text(name_hint, text)
                                        files_imported += 1 if success else 0
                                        files_skipped += 0 if success else 1
                                        continue
                                # Otherwise treat as raw config text
                                success = self.import_profile_text(os.path.splitext(os.path.basename(lp))[0], data_text)
                                files_imported += 1 if success else 0
                                files_skipped += 0 if success else 1
                                continue
                    except Exception as e:
                        files_skipped += 1
                        print(f"Import error for local {local_path}: {e}")
                        continue

                # 2) URL drops (http/https)
                try:
                    raw_url = url.toString()
                except Exception:
                    raw_url = ""
                if raw_url and (self._is_http_url(raw_url) or self._is_data_url(raw_url)):
                    # Handle data URLs (inline images or text)
                    if self._is_data_url(raw_url):
                        try:
                            header, b64data = raw_url.split(",", 1)
                            mediatype = header.split(";")[0][5:].lower() if header.startswith("data:") else ""
                            data = base64.b64decode(b64data, validate=False)
                            if mediatype.startswith("image/"):
                                text = self._decode_qr_bytes_to_text(data)
                                if text:
                                    success = self.import_profile_text("qr_image", text)
                                    files_imported += 1 if success else 0
                                    files_skipped += 0 if success else 1
                                    continue
                                QtWidgets.QMessageBox.warning(self, "QR not found", "No QR code detected in the dropped image.")
                                files_skipped += 1
                                continue
                            # Otherwise treat as UTF-8 text
                            text = data.decode("utf-8", errors="ignore")
                            if text.strip():
                                success = self.import_profile_text("dropped_text", text)
                                files_imported += 1 if success else 0
                                files_skipped += 0 if success else 1
                                continue
                        except Exception as e:
                            files_skipped += 1
                            print(f"Error parsing data URL: {e}")
                            continue

                    # Regular http(s)
                    dl = self._download_url_text(raw_url)
                    if dl:
                        name_hint, text = dl
                        success = self.import_profile_text(name_hint, text)
                        files_imported += 1 if success else 0
                        files_skipped += 0 if success else 1
                        continue

            # Show result notification
            if files_imported > 0:
                msg = f"Imported {files_imported} profile(s) successfully"
                if files_skipped > 0:
                    msg += f" ({files_skipped} item(s) skipped due to duplicate or error)"
                QtWidgets.QMessageBox.information(self, "Import succeeded", msg)
                self.refresh_table()
            elif files_skipped > 0:
                QtWidgets.QMessageBox.warning(self, "Import failed", f"{files_skipped} item(s) could not be imported (duplicate or error)")

            event.acceptProposedAction()

    def import_profile_file(self, file_path):
        """Import a specific profile .conf file"""
        name = os.path.splitext(os.path.basename(file_path))[0]
        
        # Prevent reserved temp suffix and duplicates
        if name.endswith("_wireproxy"):
            return False
        if any(p["name"] == name for p in self.state["profiles"]):
            return False
        
        dest_path = os.path.join(PROFILE_DIR, os.path.basename(file_path))
        
        # Ensure destination file does not exist
        if os.path.exists(dest_path):
            return False
        
        # Copy file
        shutil.copy(file_path, dest_path)
        
        # Append to state
        self.state["profiles"].append({
            "name": name,
            "conf_path": dest_path,
            "proxy_port": None,
            "pid": None,
            "running": False,
            "last_port": None,
        })
        
        return True

    def import_profile(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose WireGuard .conf", "", "WireGuard Config (*.conf)")
        if file_path:
            success = self.import_profile_file(file_path)
            if success:
                QtWidgets.QMessageBox.information(self, "Success", "Profile imported successfully!")
                self.refresh_table()
            else:
                QtWidgets.QMessageBox.warning(self, "Failed", "Profile already exists or an error occurred!")

    def edit_profile(self, row: int):
        profile = self.state["profiles"][row]
        # Disallow editing while running
        if self.is_process_running(profile.get("pid")):
            QtWidgets.QMessageBox.warning(self, "Running", "Please disconnect the profile before editing.")
            return
        
        # Ensure config exists; allow relink or delete if missing
        if not self.ensure_profile_conf_exists(profile):
            return
        
        # Read .conf contents
        try:
            with open(profile["conf_path"], "r", encoding="utf-8") as f:
                current_content = f.read()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Cannot read config file: {e}")
            return
        # Open edit dialog
        dialog = EditProfileDialog(self, current_name=profile["name"], conf_content=current_content)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            new_name = dialog.get_profile_name().strip()
            new_content = dialog.get_conf_content()
            if not new_name:
                QtWidgets.QMessageBox.warning(self, "Missing name", "Profile name cannot be empty.")
                return
            # Handle rename if needed
            if new_name != profile["name"]:
                if new_name.endswith("_wireproxy"):
                    QtWidgets.QMessageBox.warning(self, "Invalid name", "Profile name must not end with '_wireproxy'.")
                    return
                if any(p["name"] == new_name for p in self.state["profiles"]):
                    QtWidgets.QMessageBox.warning(self, "Duplicate name", "A profile with this name already exists.")
                    return
                new_conf_path = os.path.join(PROFILE_DIR, f"{new_name}.conf")
                if os.path.exists(new_conf_path):
                    QtWidgets.QMessageBox.warning(self, "File exists", "A .conf with this name already exists in profiles.")
                    return
                try:
                    os.rename(profile["conf_path"], new_conf_path)
                except Exception as e:
                    QtWidgets.QMessageBox.critical(self, "Error", f"Cannot rename file: {e}")
                    return
                profile["name"] = new_name
                profile["conf_path"] = new_conf_path
            # Write new content back
            try:
                with open(profile["conf_path"], "w", encoding="utf-8") as f:
                    f.write(new_content)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Error", f"Cannot save config file: {e}")
                return
            QtWidgets.QMessageBox.information(self, "Saved", "Profile updated successfully.")
            self.refresh_table()

    def delete_profile(self, row: int):
        profile = self.state["profiles"][row]
        reply = QtWidgets.QMessageBox.question(
            self,
            "Delete profile",
            f"Are you sure you want to delete '{profile['name']}'?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._delete_profile(profile)
        self.refresh_table()

    def _delete_profile(self, profile):
        # Dừng nếu đang chạy
        if self.is_process_running(profile.get("pid")):
            self.disconnect_profile(profile)
        # Xóa file .conf chính
        try:
            if profile.get("conf_path") and os.path.exists(profile["conf_path"]):
                os.remove(profile["conf_path"])
        except Exception:
            pass
        # Xóa file tạm wireproxy nếu có
        temp_conf = os.path.join(PROFILE_DIR, f"{profile['name']}{TEMP_WIREPROXY_SUFFIX}")
        try:
            if os.path.exists(temp_conf):
                os.remove(temp_conf)
        except Exception:
            pass
        # Gỡ khỏi state
        self.state["profiles"] = [p for p in self.state["profiles"] if p["name"] != profile["name"]]
        self.save_state()

    def ensure_profile_conf_exists(self, profile) -> bool:
        """Ensure the profile config file exists. If missing, allow the user to:
        - Pick a replacement .conf (copy into profiles and update path)
        - Delete the profile
        Return True if a valid file exists after handling, else False."""
        conf_path = profile.get("conf_path")
        if conf_path and os.path.exists(conf_path):
            return True

        msg = QtWidgets.QMessageBox(self)
        msg.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        msg.setWindowTitle("File missing")
        msg.setText("Config file not found. What would you like to do?")
        choose_btn = msg.addButton("Choose file", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        delete_btn = msg.addButton("Delete profile", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = msg.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == choose_btn:
            file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose WireGuard .conf", "", "WireGuard Config (*.conf)")
            if not file_path:
                return False
            try:
                os.makedirs(PROFILE_DIR, exist_ok=True)
                dest_path = os.path.join(PROFILE_DIR, f"{profile['name']}.conf")
                shutil.copy(file_path, dest_path)
                profile["conf_path"] = dest_path
                self.save_state()
                return True
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Error", f"Failed to copy file: {e}")
                return False
        if clicked == delete_btn:
            self._delete_profile(profile)
            self.refresh_table()
            return False
        return False

    def open_logs_folder(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(LOG_DIR)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", LOG_DIR])
            else:
                subprocess.Popen(["xdg-open", LOG_DIR])
        except Exception:
            QtWidgets.QMessageBox.warning(self, "Lỗi", f"Không mở được thư mục logs: {LOG_DIR}")

    def _terminate_process(self, pid: int) -> None:
        """Kết thúc tiến trình theo cách phù hợp hệ điều hành.
        - Windows: ưu tiên taskkill để chấm dứt cả tiến trình con.
        - Khác: dùng os.kill(pid, 9)"""
        if sys.platform.startswith("win"):
            try:
                completed = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
                if completed.returncode == 0:
                    return
                LOGGER.warning(f"taskkill trả về {completed.returncode}: {completed.stderr.strip()}")
            except Exception:
                LOGGER.exception("taskkill thất bại, fallback sang os.kill")
        # Fallback chung
        try:
            os.kill(int(pid), 9)
        except Exception:
            LOGGER.exception("os.kill thất bại khi kết thúc tiến trình")


class EditProfileDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget, current_name: str, conf_content: str):
        super().__init__(parent)
        self.setWindowTitle("Sửa profile WireGuard")
        self.setModal(True)
        self.resize(700, 500)

        self.name_edit = QtWidgets.QLineEdit(current_name)
        self.name_edit.setPlaceholderText("Tên profile (không cần .conf)")

        self.conf_edit = QtWidgets.QPlainTextEdit()
        self.conf_edit.setPlainText(conf_content)
        self.conf_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.conf_edit.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))

        form = QtWidgets.QFormLayout()
        form.addRow("Tên profile", self.name_edit)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.conf_edit, 1)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def get_profile_name(self) -> str:
        return self.name_edit.text()

    def get_conf_content(self) -> str:
        return self.conf_edit.toPlainText()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = WireProxyManager()
    window.show()
    sys.exit(app.exec())
