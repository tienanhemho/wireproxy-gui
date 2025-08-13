import os
import sys
import logging
import socket
from functools import partial
from PyQt6 import QtWidgets, QtCore, QtGui

from src.ui.edit_dialog import EditProfileDialog
from src.services.profile_service import IMAGE_EXTENSIONS

LOGGER = logging.getLogger("wireproxy_gui")
PROFILE_DIR = "profiles"
LOG_DIR = "logs"
PORT_RANGE = (60000, 65535)

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

        import_btn = QtWidgets.QPushButton("Drag and drop a .conf here or click to choose")
        import_btn.clicked.connect(self.on_import_button_clicked)

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
        main_layout.addWidget(import_btn)

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

            port = str(profile["proxy_port"]) if profile.get("proxy_port") and profile["running"] else "—"
            self.table.setItem(row, 4, QtWidgets.QTableWidgetItem(port))
            
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
                menu.addAction("Connect").triggered.connect(lambda: self.toggle_connection(row))
            
            menu.addSeparator()
            menu.addAction("Edit").triggered.connect(lambda: self.edit_profile(row))
            menu.addAction("Delete").triggered.connect(lambda: self.delete_profile(row))
            
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
            menu.addAction("Configure WireProxy Path...").triggered.connect(self.choose_wireproxy_path)
            menu.addAction("Open Logs Folder...").triggered.connect(self.open_logs_folder)

        menu.exec(self.table.viewport().mapToGlobal(pos))

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

    def toggle_connection(self, row):
        profile = self.state_service.get_state()["profiles"][row]
        if profile.get("running"):
            self.wireproxy_service.stop_process(profile)
        else:
            port = self.find_free_port(profile)
            if not port:
                QtWidgets.QMessageBox.critical(self, "Error", "No free port available within the current limit.")
                return
            
            pid = self.wireproxy_service.start_process(profile, port)
            if pid:
                profile["pid"] = pid
                profile["proxy_port"] = port
                profile["running"] = True
                self.state_service.save_state()
            else:
                QtWidgets.QMessageBox.critical(self, "Error", "Failed to start WireProxy. Check logs for details.")

        self.refresh_table()

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

    def on_import_button_clicked(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose WireGuard .conf", "", "WireGuard Config (*.conf)")
        if file_path:
            success, msg = self.profile_service.import_from_file(file_path)
            if success:
                QtWidgets.QMessageBox.information(self, "Success", msg)
                self.refresh_table()
            else:
                QtWidgets.QMessageBox.warning(self, "Failed", msg)

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
            path = url.toLocalFile()
            if path: # Local file drop
                if os.path.splitext(path)[1].lower() == '.conf':
                    success, _ = self.profile_service.import_from_file(path)
                    if success: imported_count += 1
                elif os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS:
                    text = self.profile_service.decode_qr_from_path(path)
                    if text:
                        success, _ = self.profile_service.import_from_text(os.path.basename(path), text)
                        if success: imported_count += 1
            else: # URL drop
                content = self.profile_service.download_from_url(url.toString())
                if content:
                    name_hint, text = content
                    success, _ = self.profile_service.import_from_text(name_hint, text)
                    if success: imported_count += 1
        
        if imported_count > 0:
            QtWidgets.QMessageBox.information(self, "Success", f"Imported {imported_count} profile(s).")
            self.refresh_table()
