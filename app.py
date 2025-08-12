import sys
import os
import json
import shutil
import socket
import subprocess
from functools import partial
from PyQt6 import QtWidgets, QtCore, QtGui
from datetime import datetime

STATE_FILE = "state.json"
PROFILE_DIR = "profiles"
PORT_RANGE = (60000, 65535)
STATE_VERSION = 2


class WireProxyManager(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WireProxy GUI Manager")
        self.resize(820, 480)
        
        # Kích hoạt drag and drop
        self.setAcceptDrops(True)

        os.makedirs(PROFILE_DIR, exist_ok=True)
        self.state = self.load_state()

        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Tên Profile", "Port Proxy", "Trạng thái"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_context_menu)

        import_btn = QtWidgets.QPushButton("Kéo thả file .conf vào màn hình hoặc nhấn để chọn")
        import_btn.clicked.connect(self.import_profile)

        # Hàng cấu hình giới hạn port + loại proxy
        limit_layout = QtWidgets.QHBoxLayout()

        # Port limit
        limit_layout.addWidget(QtWidgets.QLabel("Giới hạn số port đang hoạt động (0 = không giới hạn):"))
        self.port_limit_spin = QtWidgets.QSpinBox()
        self.port_limit_spin.setRange(0, 10000)
        self.port_limit_spin.setValue(int(self.state.get("port_limit", 10)))
        self.port_limit_spin.valueChanged.connect(self.on_port_limit_change)
        limit_layout.addWidget(self.port_limit_spin)

        # Proxy type selector
        limit_layout.addSpacing(16)
        limit_layout.addWidget(QtWidgets.QLabel("Loại proxy:"))
        self.proxy_type_combo = QtWidgets.QComboBox()
        self.proxy_type_combo.addItems(["SOCKS5", "HTTP"])
        current_type = (self.state.get("proxy_type") or "socks").lower()
        self.proxy_type_combo.setCurrentIndex(0 if current_type == "socks" else 1)
        self.proxy_type_combo.currentIndexChanged.connect(self.on_proxy_type_change)
        limit_layout.addWidget(self.proxy_type_combo)

        limit_layout.addStretch(1)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(limit_layout)
        layout.addWidget(self.table)
        layout.addWidget(import_btn)

        container = QtWidgets.QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.load_profiles()

    def load_state(self):
        default_state = {"version": STATE_VERSION, "profiles": [], "port_limit": 10, "wireproxy_path": None, "proxy_type": "socks"}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                    # Versioning + migration
                    if not isinstance(data, dict):
                        return default_state
                    data.setdefault("version", 0)
                    if int(data.get("version", 0) or 0) < STATE_VERSION:
                        data = self.migrate_state(data)
                    # Merge mặc định
                    for k, v in default_state.items():
                        data.setdefault(k, v)
                    # Bổ sung key mặc định cho từng profile
                    for p in data.get("profiles", []):
                        p.setdefault("proxy_port", None)
                        p.setdefault("pid", None)
                        p.setdefault("running", False)
                        p.setdefault("conf_path", None)
                        p.setdefault("last_port", None)
                    return data
                except:
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

    def migrate_state(self, data: dict) -> dict:
        """Nâng cấp state cũ lên schema mới theo STATE_VERSION. Sẽ backup file cũ trước khi ghi."""
        try:
            if os.path.exists(STATE_FILE):
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy(STATE_FILE, f"{STATE_FILE}.bak-{ts}")
        except Exception:
            pass

        current = int(data.get("version", 0) or 0)
        # Thực hiện lần lượt từng bước migrate cho tới STATE_VERSION
        while current < STATE_VERSION:
            # Ví dụ nếu có thay đổi ở các version sau, xử lý tại đây
            if current < 1:
                # v1: khởi tạo schema cơ bản, không đổi gì thêm
                current = 1
                continue
            if current < 2:
                # v2: thêm cấu hình loại proxy ở state (mặc định socks)
                try:
                    if not isinstance(data, dict):
                        data = {}
                    data.setdefault("proxy_type", "socks")
                except Exception:
                    pass
                current = 2
                continue
            # An toàn: nếu không có rule cụ thể, thoát vòng lặp
            break

        data["version"] = STATE_VERSION
        # Ghi ngay sau migrate để đảm bảo file trên đĩa đã cập nhật
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
        """Đảm bảo có đường dẫn wireproxy hợp lệ: ưu tiên state, nếu không thì dò PATH, nếu vẫn không có thì hỏi người dùng chọn."""
        # 1) state lưu sẵn
        path = self.state.get("wireproxy_path")
        if path and os.path.exists(path):
            return path
        # 2) thử dò trong PATH
        guessed = shutil.which("wireproxy") or shutil.which("wireproxy.exe")
        if guessed and os.path.exists(guessed):
            self.state["wireproxy_path"] = guessed
            self.save_state()
            return guessed
        # 3) hỏi người dùng chọn
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Chọn file thực thi WireProxy",
            "",
            "WireProxy Executable (wireproxy*);;All Files (*)",
        )
        if not file_path:
            QtWidgets.QMessageBox.warning(self, "Thiếu WireProxy", "Vui lòng chọn file thực thi WireProxy để tiếp tục.")
            return None
        if not os.path.exists(file_path):
            QtWidgets.QMessageBox.critical(self, "Lỗi", "Đường dẫn WireProxy không hợp lệ.")
            return None
        self.state["wireproxy_path"] = file_path
        self.save_state()
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
        QtWidgets.QMessageBox.information(self, "Đã lưu", f"Đã thiết lập WireProxy: {file_path}")

    # ==== Port helpers ====
    def get_allowed_ports(self):
        limit = int(self.state.get("port_limit", 0))
        if limit and limit > 0:
            end = min(PORT_RANGE[0] + limit - 1, PORT_RANGE[1])
            return range(PORT_RANGE[0], end + 1)
        # Không giới hạn → toàn bộ range
        return range(PORT_RANGE[0], PORT_RANGE[1] + 1)

    def get_ports_in_use(self):
        ports = []
        for p in self.state["profiles"]:
            if self.is_process_running(p.get("pid")) and p.get("proxy_port"):
                ports.append(int(p["proxy_port"]))
        return set(ports)

    def is_port_free_os(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", int(port))) != 0

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
        # Quét thư mục profiles/ và state.json
        profiles = []
        for file in os.listdir(PROFILE_DIR):
            if file.endswith(".conf"):
                name = os.path.splitext(file)[0]
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
        self.table.setRowCount(len(self.state["profiles"]))
        for row, profile in enumerate(self.state["profiles"]):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(profile["name"]))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(profile["proxy_port"]) if profile["proxy_port"] else "—"))
            status_text = "Đang chạy" if self.is_process_running(profile.get("pid")) else "Chưa chạy"
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(status_text))
        self.save_state()

    def is_process_running(self, pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except:
            return False

    # ==== Context menu ====
    def on_table_context_menu(self, pos: QtCore.QPoint):
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

                # Nâng cao: chọn port trong giới hạn còn trống (nhanh, không quét OS)
                advanced = menu.addMenu("Connect (chọn port)")
                quick_ports = self.get_available_ports_for_menu(max_items=50)
                if not quick_ports:
                    disabled = advanced.addAction("Không còn port trống trong giới hạn")
                    disabled.setEnabled(False)
                else:
                    for p in quick_ports:
                        action = advanced.addAction(f"127.0.0.1:{p}")
                        action.triggered.connect(partial(self.connect_profile_with_port_row, row, int(p)))
                    if len(quick_ports) >= 50:
                        more = advanced.addAction("… còn nữa, nhập port cụ thể…")
                        more.triggered.connect(partial(self.prompt_and_connect_port_row, row))

            menu.addSeparator()
            act_edit = menu.addAction("Sửa")
            act_edit.triggered.connect(partial(self.edit_profile, row))

            act_delete = menu.addAction("Xóa")
            act_delete.triggered.connect(partial(self.delete_profile, row))
        else:
            # Không nằm trên hàng nào → menu chung
            act_auto = menu.addAction("Tự động kết nối theo giới hạn")
            act_auto.triggered.connect(self.auto_connect_up_to_limit)
            menu.addSeparator()
            act_import = menu.addAction("Import profile (.conf)")
            act_import.triggered.connect(self.import_profile)
            act_cfg = menu.addAction("Cấu hình đường dẫn WireProxy…")
            act_cfg.triggered.connect(self.choose_wireproxy_path)

        menu.exec(self.table.viewport().mapToGlobal(pos))

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
        # Kết nối lần lượt theo thứ tự bảng, tới khi hết port trong giới hạn
        # Không quét OS hàng loạt; chỉ kiểm tra khi kết nối từng profile
        used_before = len(self.get_ports_in_use())
        limit = int(self.state.get("port_limit", 0))
        if limit and used_before >= limit:
            QtWidgets.QMessageBox.information(self, "Hết port", "Đã đạt tối đa số port trong giới hạn.")
            return
        for profile in self.state["profiles"]:
            if limit:
                used_now = len(self.get_ports_in_use())
                if used_now >= limit:
                    break
            if not self.is_process_running(profile.get("pid")):
                port = self.pick_port_for_profile(profile)
                if not port:
                    break
                self.connect_profile_with_port(profile, port)
        self.refresh_table()

    # ==== Connect/Disconnect ====
    def toggle_connection(self, row):
        profile = self.state["profiles"][row]
        if self.is_process_running(profile.get("pid")):
            self.disconnect_profile(profile)
        else:
            self.connect_profile(profile)
        self.refresh_table()

    def pick_port_for_profile(self, profile):
        """Ưu tiên dùng last_port nếu hợp lệ; nếu không tìm port trống mới."""
        last_port = profile.get("last_port") or profile.get("proxy_port")
        if last_port:
            allowed = set(self.get_allowed_ports())
            if last_port in allowed and self.is_port_free_os(int(last_port)) and int(last_port) not in self.get_ports_in_use():
                return int(last_port)
        return self.find_free_port()

    def connect_profile_with_port(self, profile, port: int):
        # Đảm bảo file cấu hình tồn tại
        if not self.ensure_profile_conf_exists(profile):
            return
        # Kiểm tra port thuộc giới hạn và còn trống
        allowed = set(self.get_allowed_ports())
        if port not in allowed:
            QtWidgets.QMessageBox.warning(self, "Ngoài giới hạn", f"Port {port} không nằm trong giới hạn hiện tại.")
            return
        # Xử lý ghi đè: nếu port đang dùng bởi profile khác do app quản lý → hỏi xác nhận và disconnect
        # 1) Tìm profile khác đang dùng port này (trong app)
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
        # 2) Nếu có profile khác đang dùng → hỏi xác nhận ghi đè
        if other_profile_using_port is not None:
            reply = QtWidgets.QMessageBox.question(
                self,
                "Ghi đè port",
                f"Port {port} đang được sử dụng bởi profile '{other_profile_using_port['name']}'.\n"
                "Bạn có muốn ngắt kết nối profile đó và dùng port này không?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            # Ngắt profile cũ
            self.disconnect_profile(other_profile_using_port)
            # Kiểm tra lại: nếu vẫn bận thì không thể ghi đè (do process ngoài app)
            if not self.is_port_free_os(port):
                QtWidgets.QMessageBox.critical(self, "Port bận", f"Không thể dùng port {port} vì đang bận bởi tiến trình khác.")
                return
        else:
            # Không có profile nào của app dùng; nếu OS báo bận → process ngoài app, chặn
            if not self.is_port_free_os(port):
                QtWidgets.QMessageBox.warning(self, "Port bận", f"Port {port} đang được sử dụng bởi tiến trình khác.")
                return
            # Và nếu vì lý do nào đó port nằm trong get_ports_in_use (không nên xảy ra) thì chặn dự phòng
            if port in self.get_ports_in_use():
                QtWidgets.QMessageBox.warning(self, "Port bận", f"Port {port} đang được sử dụng.")
                return
        # Đảm bảo có wireproxy
        wireproxy_path = self.ensure_wireproxy_path()
        if not wireproxy_path:
            return
        try:
            temp_conf = os.path.join(PROFILE_DIR, f"{profile['name']}_wireproxy.conf")
            proxy_type = (self.state.get("proxy_type") or "socks").lower()
            self.generate_wireproxy_conf(profile["conf_path"], port, temp_conf, proxy_type)
            proc = subprocess.Popen([wireproxy_path, "-c", temp_conf])
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Lỗi", f"Không chạy được WireProxy: {e}")
            return
        profile["proxy_port"] = int(port)
        profile["last_port"] = int(port)
        profile["pid"] = proc.pid
        profile["running"] = True

    def connect_profile(self, profile):
        # Đảm bảo file cấu hình tồn tại, nếu thiếu cho phép liên kết lại hoặc xóa
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

        temp_conf = os.path.join(PROFILE_DIR, f"{profile['name']}_wireproxy.conf")
        proxy_type = (self.state.get("proxy_type") or "socks").lower()
        self.generate_wireproxy_conf(profile["conf_path"], port, temp_conf, proxy_type)

        try:
            proc = subprocess.Popen([wireproxy_path, "-c", temp_conf])
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Lỗi", f"Không chạy được WireProxy: {e}")
            return

        profile["proxy_port"] = int(port)
        profile["last_port"] = int(port)
        profile["pid"] = proc.pid
        profile["running"] = True

    def disconnect_profile(self, profile):
        pid = profile.get("pid")
        if pid and self.is_process_running(pid):
            try:
                os.kill(pid, 9)  # force kill
            except:
                pass
        # Lưu last_port trước khi xóa proxy_port
        if profile.get("proxy_port"):
            profile["last_port"] = int(profile["proxy_port"])
        profile["pid"] = None
        profile["proxy_port"] = None
        profile["running"] = False

    def generate_wireproxy_conf(self, wg_conf, port, output_conf, proxy_type: str = "socks"):
        with open(output_conf, "w", encoding="utf-8") as f:
            f.write(f"[WireGuard]\nConfigFile = {wg_conf}\n\n")
            f.write("[Proxy]\n")
            f.write(f"BindAddress = 127.0.0.1:{port}\n")
            # wireproxy chấp nhận 'socks' hoặc 'http'
            pt = "http" if str(proxy_type).lower() == "http" else "socks"
            f.write(f"ProxyType = {pt}\n")

    def dragEnterEvent(self, event):
        """Xử lý khi file được kéo vào cửa sổ"""
        if event.mimeData().hasUrls():
            # Kiểm tra xem có file .conf không
            for url in event.mimeData().urls():
                if url.toLocalFile().endswith('.conf'):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragMoveEvent(self, event):
        """Xử lý khi file đang được kéo di chuyển trong cửa sổ"""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().endswith('.conf'):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        """Xử lý khi file được thả vào cửa sổ"""
        if event.mimeData().hasUrls():
            files_imported = 0
            files_skipped = 0
            
            for url in event.mimeData().urls():
                file_path = url.toLocalFile()
                if file_path.endswith('.conf'):
                    try:
                        success = self.import_profile_file(file_path)
                        if success:
                            files_imported += 1
                        else:
                            files_skipped += 1
                    except Exception as e:
                        files_skipped += 1
                        print(f"Lỗi import file {file_path}: {e}")
            
            # Hiển thị thông báo kết quả
            if files_imported > 0:
                msg = f"Đã import thành công {files_imported} profile"
                if files_skipped > 0:
                    msg += f" ({files_skipped} file bị bỏ qua do trùng tên hoặc lỗi)"
                QtWidgets.QMessageBox.information(self, "Import thành công", msg)
                self.refresh_table()
            elif files_skipped > 0:
                QtWidgets.QMessageBox.warning(self, "Import thất bại", 
                                            f"{files_skipped} file không thể import (có thể do trùng tên hoặc lỗi)")
            
            event.acceptProposedAction()

    def import_profile_file(self, file_path):
        """Import một file profile cụ thể"""
        name = os.path.splitext(os.path.basename(file_path))[0]
        
        # Kiểm tra xem profile đã tồn tại chưa
        if any(p["name"] == name for p in self.state["profiles"]):
            return False  # Profile đã tồn tại
        
        dest_path = os.path.join(PROFILE_DIR, os.path.basename(file_path))
        
        # Kiểm tra file đích đã tồn tại chưa
        if os.path.exists(dest_path):
            return False  # File đã tồn tại
        
        # Copy file
        shutil.copy(file_path, dest_path)
        
        # Thêm vào state
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
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Chọn file WireGuard .conf", "", "WireGuard Config (*.conf)")
        if file_path:
            success = self.import_profile_file(file_path)
            if success:
                QtWidgets.QMessageBox.information(self, "Thành công", "Profile đã được import thành công!")
                self.refresh_table()
            else:
                QtWidgets.QMessageBox.warning(self, "Thất bại", "Profile đã tồn tại hoặc có lỗi xảy ra!")

    def edit_profile(self, row: int):
        profile = self.state["profiles"][row]
        # Không cho sửa khi đang chạy
        if self.is_process_running(profile.get("pid")):
            QtWidgets.QMessageBox.warning(self, "Đang chạy", "Vui lòng Disconnect profile trước khi sửa.")
            return
        
        # Đảm bảo file cấu hình tồn tại, nếu thiếu cho phép liên kết lại hoặc xóa
        if not self.ensure_profile_conf_exists(profile):
            return
        
        # Đọc nội dung file .conf
        try:
            with open(profile["conf_path"], "r", encoding="utf-8") as f:
                current_content = f.read()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Lỗi", f"Không thể đọc file cấu hình: {e}")
            return
        # Mở dialog sửa
        dialog = EditProfileDialog(self, current_name=profile["name"], conf_content=current_content)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            new_name = dialog.get_profile_name().strip()
            new_content = dialog.get_conf_content()
            if not new_name:
                QtWidgets.QMessageBox.warning(self, "Thiếu tên", "Tên profile không được để trống.")
                return
            # Xử lý đổi tên nếu cần
            if new_name != profile["name"]:
                if any(p["name"] == new_name for p in self.state["profiles"]):
                    QtWidgets.QMessageBox.warning(self, "Trùng tên", "Đã tồn tại profile với tên này.")
                    return
                new_conf_path = os.path.join(PROFILE_DIR, f"{new_name}.conf")
                if os.path.exists(new_conf_path):
                    QtWidgets.QMessageBox.warning(self, "Tệp tồn tại", "Đã tồn tại file .conf với tên này trong thư mục profiles.")
                    return
                try:
                    os.rename(profile["conf_path"], new_conf_path)
                except Exception as e:
                    QtWidgets.QMessageBox.critical(self, "Lỗi", f"Không thể đổi tên file: {e}")
                    return
                profile["name"] = new_name
                profile["conf_path"] = new_conf_path
            # Ghi nội dung mới vào file
            try:
                with open(profile["conf_path"], "w", encoding="utf-8") as f:
                    f.write(new_content)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Lỗi", f"Không thể lưu file cấu hình: {e}")
                return
            QtWidgets.QMessageBox.information(self, "Đã lưu", "Profile đã được cập nhật thành công.")
            self.refresh_table()

    def delete_profile(self, row: int):
        profile = self.state["profiles"][row]
        reply = QtWidgets.QMessageBox.question(
            self,
            "Xóa profile",
            f"Bạn có chắc muốn xóa '{profile['name']}'?",
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
        temp_conf = os.path.join(PROFILE_DIR, f"{profile['name']}_wireproxy.conf")
        try:
            if os.path.exists(temp_conf):
                os.remove(temp_conf)
        except Exception:
            pass
        # Gỡ khỏi state
        self.state["profiles"] = [p for p in self.state["profiles"] if p["name"] != profile["name"]]
        self.save_state()

    def ensure_profile_conf_exists(self, profile) -> bool:
        """Đảm bảo file cấu hình tồn tại. Nếu không, cho phép người dùng:
        - Chọn file .conf thay thế (copy vào thư mục profiles và cập nhật đường dẫn)
        - Xóa profile
        Trả về True nếu sau cùng có file hợp lệ, ngược lại False."""
        conf_path = profile.get("conf_path")
        if conf_path and os.path.exists(conf_path):
            return True

        msg = QtWidgets.QMessageBox(self)
        msg.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        msg.setWindowTitle("Không tìm thấy file")
        msg.setText("File cấu hình không tồn tại. Bạn muốn làm gì?")
        choose_btn = msg.addButton("Chọn file", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        delete_btn = msg.addButton("Xóa profile", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = msg.addButton("Hủy", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == choose_btn:
            file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Chọn file WireGuard .conf", "", "WireGuard Config (*.conf)")
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
                QtWidgets.QMessageBox.critical(self, "Lỗi", f"Không thể sao chép file: {e}")
                return False
        if clicked == delete_btn:
            self._delete_profile(profile)
            self.refresh_table()
            return False
        return False


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
