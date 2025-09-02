import os
import sys
import logging
import socket
from functools import partial
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import QRunnable, QThreadPool, pyqtSignal, QObject

from src.ui.edit_dialog import EditProfileDialog
from src.services.profile_service import IMAGE_EXTENSIONS, is_http_url
import base64

LOGGER = logging.getLogger("wireproxy_gui")
PROFILE_DIR = "profiles"
LOG_DIR = "logs"
PORT_RANGE = (60000, 65535)


class WorkerSignals(QObject):
    """Defines signals available from a running worker thread."""
    finished = pyqtSignal(int, int)  # pid, port
    error = pyqtSignal(str)

class FindPortWorkerSignals(QObject):
    """Defines signals for the port finding worker."""
    finished = pyqtSignal(int)  # port
    error = pyqtSignal(str)

class FindPortWorker(QRunnable):
    """Worker thread for finding a free port."""
    def __init__(self, find_free_port_func, profile):
        super().__init__()
        self.find_free_port = find_free_port_func
        self.profile = profile
        self.signals = FindPortWorkerSignals()

    @QtCore.pyqtSlot()
    def run(self):
        try:
            port = self.find_free_port(self.profile)
            if port:
                self.signals.finished.emit(port)
            else:
                self.signals.error.emit("No free port available within the current limit.")
        except Exception as e:
            LOGGER.error(f"Find port worker error: {e}", exc_info=True)
            self.signals.error.emit(f"An error occurred while finding a port: {e}")

class ConnectWorker(QRunnable):
    """Worker thread for connecting to WireProxy."""
    def __init__(self, wireproxy_service, profile, port, is_port_free_os_func=None):
        super().__init__()
        self.wireproxy_service = wireproxy_service
        self.profile = profile
        self.port = port
        self.is_port_free_os = is_port_free_os_func
        self.signals = WorkerSignals()

    @QtCore.pyqtSlot()
    def run(self):
        try:
            # If a port check function is provided, use it.
            if self.is_port_free_os and not self.is_port_free_os(self.port):
                self.signals.error.emit(f"Port {self.port} is still in use by an external process.")
                return

            pid = self.wireproxy_service.start_process(self.profile, self.port)
            if pid:
                self.signals.finished.emit(pid, self.port)
            else:
                self.signals.error.emit("Failed to start WireProxy. Check logs for details.")
        except Exception as e:
            LOGGER.error(f"Connection worker error: {e}", exc_info=True)
            self.signals.error.emit(f"An unexpected error occurred: {e}")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, state_service, profile_service, wireproxy_service, geoip_service, auto_connect_service):
        super().__init__()
        self.state_service = state_service
        self.profile_service = profile_service
        self.wireproxy_service = wireproxy_service
        self.geoip_service = geoip_service
        self.auto_connect_service = auto_connect_service

        self.setWindowTitle("WireProxy GUI Manager")
        self.resize(820, 480)
        self.setAcceptDrops(True)

        self._setup_ui()

        self.threadpool = QThreadPool()
        
        # Connect signals from services to UI slots
        self.geoip_service.location_fetched.connect(self._on_location_fetched)
        self.auto_connect_service.progress.connect(self.refresh_table)
        self.auto_connect_service.finished.connect(self.refresh_table)
        
        self.profile_service.load_profiles_from_disk()
        self.cleanup_temp_files()
        self.refresh_table()

    def _setup_ui(self):
        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Profile Name", "Host", "Location", "ZIP", "Proxy Port", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 80)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_context_menu)

        # Bottom buttons
        import_btn = QtWidgets.QPushButton()
        import_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DialogOpenButton))
        import_btn.setToolTip("Import from File...")
        import_btn.clicked.connect(self.on_import_button_clicked)

        clipboard_btn = QtWidgets.QPushButton()
        # Use a theme icon for 'paste' with a fallback for systems without it
        paste_icon = QtGui.QIcon.fromTheme("edit-paste", self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileIcon))
        clipboard_btn.setIcon(paste_icon)
        clipboard_btn.setToolTip("Import from Clipboard")
        clipboard_btn.clicked.connect(self.on_import_from_clipboard)
        
        bottom_buttons_layout = QtWidgets.QHBoxLayout()
        bottom_buttons_layout.addStretch(1)
        bottom_buttons_layout.addWidget(import_btn)
        bottom_buttons_layout.addWidget(clipboard_btn)

        # Controls layout
        controls_layout = QtWidgets.QHBoxLayout()
        state = self.state_service.get_state()

        controls_layout.addWidget(QtWidgets.QLabel("Active ports limit (0 = unlimited):"))
        self.port_limit_spin = QtWidgets.QSpinBox()
        self.port_limit_spin.setRange(0, 10000)
        self.port_limit_spin.setValue(int(state.get("port_limit", 10)))
        self.port_limit_spin.valueChanged.connect(self.on_port_limit_change)
        controls_layout.addWidget(self.port_limit_spin)

        controls_layout.addSpacing(16)
        controls_layout.addWidget(QtWidgets.QLabel("Proxy type:"))
        self.proxy_type_combo = QtWidgets.QComboBox()
        self.proxy_type_combo.addItems(["SOCKS5", "HTTP"])
        self.proxy_type_combo.setCurrentIndex(0 if (state.get("proxy_type") or "socks").lower() == "socks" else 1)
        self.proxy_type_combo.currentIndexChanged.connect(self.on_proxy_type_change)
        controls_layout.addWidget(self.proxy_type_combo)

        controls_layout.addSpacing(16)
        self.logging_checkbox = QtWidgets.QCheckBox("Logging")
        self.logging_checkbox.setChecked(bool(state.get("logging_enabled", True)))
        self.logging_checkbox.stateChanged.connect(self.on_logging_change)
        controls_layout.addWidget(self.logging_checkbox)
        controls_layout.addStretch(1)

        main_layout = QtWidgets.QVBoxLayout()
        main_layout.addLayout(controls_layout)
        main_layout.addWidget(self.table)
        main_layout.addLayout(bottom_buttons_layout)

        container = QtWidgets.QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def refresh_table(self):
        state = self.state_service.get_state()
        profiles = state.get("profiles", [])
        self.table.setRowCount(len(profiles))

        for row, profile in enumerate(profiles):
            # Update status just in case
            profile["running"] = self.wireproxy_service.is_process_running(profile.get("pid"))

            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(profile["name"]))
            
            host = self.profile_service.get_profile_host(profile) or "—"
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(host))

            location_info = self.geoip_service.get_location(host)
            loc = location_info["location"] if location_info else "Loading…"
            zip_code = location_info["zip"] if location_info else "…"
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(loc))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(zip_code))

            port_text = "—"
            if profile.get("running") and profile.get("proxy_port"):
                port_text = str(profile["proxy_port"])
            elif profile.get("last_port"):
                port_text = str(profile["last_port"])
            self.table.setItem(row, 4, QtWidgets.QTableWidgetItem(port_text))
            
            status_text = "Running" if profile["running"] else "Stopped"
            self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(status_text))
        
        self.state_service.save_state()

    def _on_location_fetched(self, host: str, location: str, zip_code: str):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item and item.text() == host:
                self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(location or "Unknown"))
                self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(zip_code or "—"))

    def on_table_context_menu(self, pos: QtCore.QPoint):
        menu = QtWidgets.QMenu(self)
        index = self.table.indexAt(pos)
        row = index.row() if index.isValid() else -1

        if row >= 0:
            profile = self.state_service.get_state()["profiles"][row]
            if profile.get("running"):
                menu.addAction("Disconnect").triggered.connect(lambda: self.toggle_connection(row))
            else:
                act_connect = menu.addAction("Connect (auto-pick port)")
                act_connect.triggered.connect(lambda: self.toggle_connection(row))

                # Add "Connect (pick port)" submenu
                pick_port_menu = menu.addMenu("Connect (pick port)")
                self.populate_pick_port_menu(pick_port_menu, row)

            menu.addSeparator()
            menu.addAction("Edit").triggered.connect(lambda: self.edit_profile(row))
            menu.addAction("Delete").triggered.connect(lambda: self.delete_selected_profiles())
            
            menu.addSeparator()
            act_from_here = menu.addAction("Auto-connect from here")
            act_from_here.triggered.connect(lambda: self.auto_connect_from_row(row))
            if self.auto_connect_service.is_running():
                act_from_here.setEnabled(False)

        else:
            # Context menu on empty area
            act_auto_all = menu.addAction("Auto-connect up to limit")
            act_auto_all.triggered.connect(self.auto_connect_all)
            if self.auto_connect_service.is_running():
                act_auto_all.setEnabled(False)

            menu.addSeparator()
            menu.addAction("Import Profile...").triggered.connect(self.on_import_button_clicked)
            menu.addAction("Import from Clipboard").triggered.connect(self.on_import_from_clipboard)
            menu.addSeparator()
            menu.addAction("Configure WireProxy Path...").triggered.connect(self.choose_wireproxy_path)
            menu.addAction("Open Logs Folder...").triggered.connect(self.open_logs_folder)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def populate_pick_port_menu(self, menu, row):
        ports_with_status = self.get_ports_for_menu()
        if not ports_with_status:
            menu.addAction("No ports available in limit").setEnabled(False)
            return

        for port, is_used in ports_with_status:
            label = f"Port {port}" + (" (in use)" if is_used else "")
            action = menu.addAction(label)
            action.triggered.connect(partial(self.connect_with_specific_port, row, port))
        
        if len(ports_with_status) >= 50: # Add option to enter manually if list is long
            menu.addSeparator()
            menu.addAction("Enter port...").triggered.connect(lambda: self.prompt_and_connect(row))

    def get_ports_for_menu(self, max_ports=50):
        """Gets a list of (port, is_used) tuples for the context menu."""
        state = self.state_service.get_state()
        used_ports = {
            p["proxy_port"] for p in state["profiles"]
            if p.get("proxy_port") and self.wireproxy_service.is_process_running(p.get("pid"))
        }
        
        limit = int(state.get("port_limit", 0))
        allowed_ports = range(PORT_RANGE[0], PORT_RANGE[0] + limit) if limit > 0 else PORT_RANGE
        
        ports_list = []
        for port in allowed_ports:
            if len(ports_list) >= max_ports:
                break
            ports_list.append((port, port in used_ports))
        return ports_list

    def prompt_and_connect(self, row):
        port_str, ok = QtWidgets.QInputDialog.getInt(self, "Choose Port", "Enter a port number:", PORT_RANGE[0], PORT_RANGE[0], PORT_RANGE[1], 1)
        if ok:
            self.connect_with_specific_port(row, port_str)

    def connect_with_specific_port(self, row, port):
        profile_to_connect = self.state_service.get_state()["profiles"][row]
        state = self.state_service.get_state()

        # Check if another profile is using this port
        other_profile = next((p for p in state["profiles"] if p.get("proxy_port") == port and p["name"] != profile_to_connect["name"]), None)
        
        if other_profile and self.wireproxy_service.is_process_running(other_profile.get("pid")):
            reply = QtWidgets.QMessageBox.question(self, "Port In Use",
                f"Port {port} is used by '{other_profile['name']}'.\nDisconnect it and connect '{profile_to_connect['name']}'?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            if reply == QtWidgets.QMessageBox.StandardButton.No:
                return
            self.wireproxy_service.stop_process(other_profile)
            self.refresh_table()
            # Give a moment for the port to be released
            QtCore.QTimer.singleShot(500, lambda: self._finish_connecting_specific_port(profile_to_connect, port))
        else:
            self._finish_connecting_specific_port(profile_to_connect, port)

    def on_connection_finished(self, row, pid, port):
        # Ensure row is still valid
        if row >= self.table.rowCount():
            self.refresh_table()
            return
            
        profile = self.state_service.get_state()["profiles"][row]
        profile["pid"] = pid
        profile["proxy_port"] = port
        profile["last_port"] = port
        profile["running"] = True
        self.state_service.save_state()
        self.refresh_table()

    def on_connection_error(self, error_message):
        QtWidgets.QMessageBox.critical(self, "Connection Error", error_message)
        self.refresh_table()

    def _finish_connecting_specific_port(self, profile, port):
        row = -1
        profiles = self.state_service.get_state()["profiles"]
        for i, p in enumerate(profiles):
            if p["name"] == profile["name"]:
                row = i
                break
        
        if row == -1:
            LOGGER.error(f"Could not find row for profile '{profile['name']}'")
            self.refresh_table()
            return

        self.table.setItem(row, 5, QtWidgets.QTableWidgetItem("Connecting..."))

        # Pass the port check function to the worker
        worker = ConnectWorker(self.wireproxy_service, profile, port, is_port_free_os_func=self.is_port_free_os)
        worker.signals.finished.connect(lambda pid, p, r=row: self.on_connection_finished(r, pid, p))
        worker.signals.error.connect(self.on_connection_error)
        self.threadpool.start(worker)

    def auto_connect_all(self):
        if self.auto_connect_service.is_running():
            return
        self.auto_connect_service.start()
        self.refresh_table() # Show immediate status change if any

    def auto_connect_from_row(self, start_row: int):
        if self.auto_connect_service.is_running():
            return
        
        total_rows = self.table.rowCount()
        indices = list(range(start_row, total_rows))
        self.auto_connect_service.start(indices=indices)
        self.refresh_table()

    def on_port_found(self, row, port):
        profile = self.state_service.get_state()["profiles"][row]
        self.table.setItem(row, 5, QtWidgets.QTableWidgetItem("Connecting..."))
        
        # Do not perform OS port check here, as find_free_port already did it
        connect_worker = ConnectWorker(self.wireproxy_service, profile, port)
        connect_worker.signals.finished.connect(lambda pid, p, r=row: self.on_connection_finished(r, pid, p))
        connect_worker.signals.error.connect(self.on_connection_error)
        self.threadpool.start(connect_worker)

    def toggle_connection(self, row):
        profile = self.state_service.get_state()["profiles"][row]
        if profile.get("running"):
            self.wireproxy_service.stop_process(profile)
            self.refresh_table()
        else:
            self.table.setItem(row, 5, QtWidgets.QTableWidgetItem("Finding port..."))
            
            port_worker = FindPortWorker(self.find_free_port, profile)
            port_worker.signals.finished.connect(lambda port, r=row: self.on_port_found(r, port))
            port_worker.signals.error.connect(self.on_connection_error)
            self.threadpool.start(port_worker)

    def find_free_port(self, profile_to_start):
        state = self.state_service.get_state()
        used_ports = {
            p["proxy_port"] for p in state["profiles"] 
            if p.get("proxy_port") and self.wireproxy_service.is_process_running(p.get("pid"))
        }
        
        limit = int(state.get("port_limit", 0))
        if limit > 0 and len(used_ports) >= limit:
            return None

        # Try last used port first
        last_port = profile_to_start.get("last_port")
        if last_port and last_port not in used_ports and self.is_port_free_os(last_port):
            return last_port

        # Scan for a new port
        start, end = PORT_RANGE
        for port in range(start, end + 1):
            if port not in used_ports and self.is_port_free_os(port):
                return port
        
        return None

    def is_port_free_os(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) != 0

    def edit_profile(self, row):
        profile = self.state_service.get_state()["profiles"][row]
        if profile.get("running"):
            QtWidgets.QMessageBox.warning(self, "Running", "Please disconnect the profile before editing.")
            return

        try:
            with open(profile["conf_path"], "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Cannot read config file: {e}")
            return

        dialog = EditProfileDialog(self, current_name=profile["name"], conf_content=content)
        if dialog.exec():
            new_name = dialog.get_profile_name()
            new_content = dialog.get_conf_content()
            if not new_name:
                QtWidgets.QMessageBox.warning(self, "Error", "Profile name cannot be empty.")
                return

            success, msg = self.profile_service.update_profile(profile["name"], new_name, new_content)
            if success:
                QtWidgets.QMessageBox.information(self, "Success", msg)
            else:
                QtWidgets.QMessageBox.critical(self, "Error", msg)
            self.refresh_table()

    def delete_profile(self, row):
        profile = self.state_service.get_state()["profiles"][row]
        reply = QtWidgets.QMessageBox.question(self, "Delete Profile", f"Delete '{profile['name']}'?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            if profile.get("running"):
                self.wireproxy_service.stop_process(profile)
            self.profile_service.delete_profile(profile["name"])
            self.refresh_table()

    def delete_selected_profiles(self):
        selected_items = self.table.selectedItems()
        if not selected_items:
            return

        rows = sorted(list(set(item.row() for item in selected_items)))
        profiles = [self.state_service.get_state()["profiles"][row] for row in rows]
        
        if not profiles:
            return

        if len(profiles) == 1:
            message = f"Delete '{profiles[0]['name']}'?"
        else:
            message = f"Delete {len(profiles)} selected profiles?"

        msg_box = QtWidgets.QMessageBox(self)
        msg_box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        msg_box.setText(message)
        msg_box.setWindowTitle("Delete Profile(s)")
        yes_button = msg_box.addButton("OK", QtWidgets.QMessageBox.ButtonRole.YesRole)
        msg_box.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.NoRole)
        msg_box.setDefaultButton(yes_button)
        
        msg_box.exec()

        if msg_box.clickedButton() == yes_button:
            # Iterate backwards to avoid index issues when removing items
            for row in reversed(rows):
                profile = self.state_service.get_state()["profiles"][row]
                if profile.get("running"):
                    self.wireproxy_service.stop_process(profile)
                self.profile_service.delete_profile(profile["name"])
            self.refresh_table()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Delete:
            self.delete_selected_profiles()
        else:
            super().keyPressEvent(event)

    def on_import_button_clicked(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose WireGuard .conf", "", "WireGuard Config (*.conf)")
        if file_path:
            success, msg = self.profile_service.import_from_file(file_path)
            if success:
                self.refresh_table()
            else:
                QtWidgets.QMessageBox.warning(self, "Failed", msg)

    def on_import_from_clipboard(self):
        clipboard = QtWidgets.QApplication.clipboard()
        text = clipboard.text()

        if not text:
            QtWidgets.QMessageBox.information(self, "Clipboard Empty", "Your clipboard is empty.")
            return

        imported_count = self.profile_service.import_from_clipboard_text(text)

        if imported_count > 0:
            self.refresh_table()
        else:
            QtWidgets.QMessageBox.warning(self, "Not Found", "No valid wireguard:// URLs found in your clipboard.")

    def choose_wireproxy_path(self):
        path = self.wireproxy_service.ensure_wireproxy_path(parent_widget=self)
        if path:
            QtWidgets.QMessageBox.information(self, "Success", f"WireProxy path set to: {path}")
        else:
            QtWidgets.QMessageBox.warning(self, "Cancelled", "No WireProxy path was selected.")

    def open_logs_folder(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(LOG_DIR)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", LOG_DIR])
            else:
                subprocess.Popen(["xdg-open", LOG_DIR])
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Error", f"Could not open logs folder: {e}")

    def on_port_limit_change(self, value):
        self.state_service.get_state()["port_limit"] = value
        self.state_service.save_state()

    def on_proxy_type_change(self, index):
        proxy_type = "http" if index == 1 else "socks"
        self.state_service.get_state()["proxy_type"] = proxy_type
        self.state_service.save_state()

    def on_logging_change(self, state):
        enabled = bool(state)
        self.state_service.get_state()["logging_enabled"] = enabled
        self.state_service.save_state()
        logging.getLogger("wireproxy_gui").setLevel(logging.DEBUG if enabled else logging.CRITICAL)

    def cleanup_temp_files(self):
        for file in os.listdir(PROFILE_DIR):
            if file.endswith("_wireproxy.conf"):
                try:
                    os.remove(os.path.join(PROFILE_DIR, file))
                except Exception:
                    pass

    def closeEvent(self, event):
        for profile in self.state_service.get_state()["profiles"]:
            if profile.get("running"):
                self.wireproxy_service.stop_process(profile)
        self.cleanup_temp_files()
        event.accept()

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return

        imported_count = 0
        for url in mime.urls():
            # Case 1: Local file (.conf or image)
            if url.isLocalFile():
                path = url.toLocalFile()
                if path.lower().endswith(".conf"):
                    success, _ = self.profile_service.import_from_file(path)
                    if success: imported_count += 1
                elif os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS:
                    imported_count += self._handle_qr_import(path)
                continue

            # Case 2: Remote URL (http/https or data)
            raw_url = url.toString()
            if raw_url.startswith("data:"): # Image dragged from browser
                try:
                    header, b64data = raw_url.split(",", 1)
                    data = base64.b64decode(b64data)
                    text = self.profile_service.decode_qr_from_bytes(data)
                    if text:
                        if is_http_url(text): # QR points to another URL
                            imported_count += self._handle_url_import(text)
                        else: # QR contains config
                            success, _ = self.profile_service.import_from_text("qr_import", text)
                            if success: imported_count += 1
                except Exception as e:
                    LOGGER.error(f"Failed to process data URL: {e}")
            elif is_http_url(raw_url): # Regular URL
                imported_count += self._handle_url_import(raw_url)

        if imported_count > 0:
            self.refresh_table()

    def _handle_qr_import(self, file_path: str) -> int:
        """Helper to process QR code from a file path. Returns 1 on success, 0 on failure."""
        qr_text = self.profile_service.decode_qr_from_path(file_path)
        if not qr_text:
            return 0
        
        if is_http_url(qr_text):
            return self._handle_url_import(qr_text)
        else:
            success, _ = self.profile_service.import_from_text(os.path.basename(file_path), qr_text)
            return 1 if success else 0

    def _handle_url_import(self, url: str) -> int:
        """Helper to download config from a URL. Returns 1 on success, 0 on failure."""
        download = self.profile_service.download_text_from_url(url)
        if download:
            name_hint, text = download
            success, _ = self.profile_service.import_from_text(name_hint, text)
            return 1 if success else 0
        return 0
