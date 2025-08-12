# WireProxy GUI Manager

Ứng dụng quản lý WireGuard profiles thông qua WireProxy với giao diện đồ họa PyQt6.

## Mô tả

WireProxy GUI Manager là một công cụ GUI giúp bạn dễ dàng quản lý các profile WireGuard và tự động tạo SOCKS proxy thông qua WireProxy. Ứng dụng hỗ trợ import, quản lý và kết nối/ngắt kết nối các profile một cách trực quan.

## Yêu cầu hệ thống

- **Python 3.10+**
- **PyQt6**
- **WireProxy binary** (có sẵn; nếu không nằm trong PATH, ứng dụng sẽ cho phép bạn chọn file thực thi)
- **Windows PowerShell** (đã test)

## Cài đặt

### 1. Clone hoặc tải về project

```bash
git clone <repo-url>
cd wireproxy-gui
```

### 2. Tạo virtual environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. Cài đặt dependencies

```powershell
pip install PyQt6
```

### 4. Cài đặt WireProxy

Tải WireProxy binary từ [GitHub releases](https://github.com/octeep/wireproxy/releases) và đặt vào PATH hoặc thư mục project.

## Cách sử dụng

### 1. Khởi chạy ứng dụng

```powershell
# Với virtual environment đã activate
python app.py

# Hoặc chạy trực tiếp Python trong venv từ thư mục project
./venv/Scripts/python.exe app.py

# Thiết lập WireProxy (lần đầu)
# Nếu không tìm thấy WireProxy trong PATH, vào menu chuột phải (nền bảng)
# chọn "Cấu hình đường dẫn WireProxy…" để chọn file thực thi wireproxy.exe
```

### 2. Import Profile WireGuard

Có 2 cách để import profile:

#### Cách 1: Kéo thả file (Drag & Drop)
1. Kéo file `.conf` từ Windows Explorer
2. Thả vào cửa sổ ứng dụng
3. Ứng dụng sẽ tự động import và hiển thị thông báo

#### Cách 2: Sử dụng nút Import
1. Nhấn nút **"Kéo thả file .conf vào màn hình hoặc nhấn để chọn"**
2. Chọn file `.conf` trong hộp thoại
3. File sẽ được import vào thư mục `profiles/`

### 3. Quản lý Profile

Sau khi import, bạn sẽ thấy profile trong bảng với các cột:

- **Tên Profile**: Tên file config (không có phần mở rộng)
- **Port Proxy**: Cổng SOCKS proxy (tự động chọn khi kết nối)
- **Trạng thái**: "Đang chạy" hoặc "Chưa chạy"
- **Hành động**: Nút Connect/Disconnect

### 4. Kết nối/Ngắt kết nối

- **Connect**: Nhấn nút "Connect" để bắt đầu SOCKS proxy
- **Disconnect**: Nhấn nút "Disconnect" để dừng proxy
- Port sẽ được tự động chọn trong khoảng 60000-65535 hoặc bạn có thể
  click phải vào hàng → "Connect (chọn port)" để chọn nhanh trong giới hạn

### 5. Chọn loại proxy (SOCKS5/HTTP)

- Ở thanh cấu hình phía trên, chọn mục "Loại proxy" giữa `SOCKS5` và `HTTP`.
- Lựa chọn này sẽ được lưu vào `state.json` và áp dụng khi khởi chạy WireProxy.
- Mặc định: SOCKS5.

### 6. Giới hạn số port đang hoạt động

- Ô "Giới hạn số port đang hoạt động" ở đầu cửa sổ cho phép đặt limit.
- Mặc định: 10. Đặt 0 để không giới hạn.
- Menu chuột phải ngoài hàng có mục "Tự động kết nối theo giới hạn" để auto connect tuần tự cho đến khi đạt limit.

### 5. Sử dụng SOCKS Proxy

Sau khi kết nối thành công, bạn có thể cấu hình ứng dụng để sử dụng SOCKS proxy:

```
Host: 127.0.0.1
Port: <Port hiển thị trong cột "Port Proxy">
Type: SOCKS5
```

## Cấu trúc thư mục

```
wireproxy-gui/
├── app.py                     # File chính của ứng dụng
├── state.json                 # Lưu trạng thái (tự tạo, đã .gitignore)
├── state.example.json         # Mẫu state để tham khảo/chia sẻ
├── profiles/                  # Thư mục chứa file .conf (đã .gitignore)
│   ├── profile1.conf
│   ├── profile2.conf
│   └── ...
├── venv/                      # Virtual environment (đã .gitignore)
├── test_profile.conf          # File test mẫu
└── README.md                  # File hướng dẫn này
```

## State, versioning và migrate

- File `state.json` là dữ liệu cá nhân, đã nằm trong `.gitignore`.
- Schema có trường `version`; code có hằng `STATE_VERSION`.
- Khi mở app, nếu `state.json` cũ hơn schema mới, app sẽ tự động migrate và tạo
  backup `state.json.bak-<timestamp>`.
- Mặc định `port_limit = 10`. Có thể chỉnh từ UI; app sẽ lưu lại vào `state.json`.
- Từ phiên bản schema v2, có thêm `proxy_type` (`"socks"` hoặc `"http"`).
- Dùng `state.example.json` làm mẫu khi cần reset hoặc chia sẻ cấu hình mặc định.

## .gitignore (quan trọng)

- Đã bỏ qua: `profiles/`, `venv/`, `state.json` để tránh lộ cấu hình/nhị phân cá nhân.
  Khi đóng góp code, chỉ commit source code (ví dụ `app.py`, docs, v.v.).

## Tính năng

### ✅ Đã hoàn thành
- [x] Import profile WireGuard (.conf)
- [x] Drag & Drop support cho file .conf
- [x] Quản lý trạng thái profile (JSON)
- [x] Tự động tìm port trống
- [x] Kết nối/ngắt kết nối profile
- [x] Kiểm tra trạng thái process
- [x] Giao diện PyQt6 thân thiện
- [x] Kiểm tra trùng lặp profile
- [x] Thông báo kết quả import
 - [x] Ghi đè port trong phạm vi app quản lý (xác nhận → ngắt profile cũ → dùng lại port)
 - [x] Chọn loại proxy (SOCKS5/HTTP) và áp dụng vào cấu hình WireProxy
 - [x] Xóa profile (context menu)
 - [x] Chỉnh sửa profile (context menu)
 - [x] Chọn port nhanh trong giới hạn (context menu)
 - [x] Giới hạn số port đang hoạt động (UI)

### 🔄 Có thể mở rộng
- [ ] Export profile
- [ ] Logs viewer
- [ ] System tray integration
- [ ] Auto-start profiles
- [ ] Profile groups/categories

## Xử lý lỗi

### Lỗi thường gặp:

1. **"Không tìm thấy binary 'wireproxy' trong PATH!"**
   - Tải và cài đặt WireProxy binary
   - Đảm bảo có trong PATH hoặc copy vào thư mục project

2. **"Không tìm thấy cổng trống!"**
   - Kiểm tra firewall
   - Thử thay đổi PORT_RANGE trong code nếu cần

3. **"Profile đã tồn tại"**
   - Rename file trước khi import
   - Hoặc xóa profile cũ trong thư mục profiles/

4. **"ImportError: cannot import name 'QtWidgets' from 'PyQt6'"**
   - Cài đặt PyQt6: `pip install PyQt6`
   - Activate virtual environment

## File cấu hình WireGuard mẫu

```ini
[Interface]
PrivateKey = <private-key>
Address = 10.64.222.21/32
DNS = 1.1.1.1

[Peer]
PublicKey = <public-key>
Endpoint = <server>:2408
AllowedIPs = 0.0.0.0/0
```

## Phát triển

### Cấu trúc code:
- `WireProxyManager`: Class chính quản lý GUI
- `load_state()`: Tải trạng thái từ JSON
- `save_state()`: Lưu trạng thái vào JSON
- `import_profile_file()`: Import một file profile
- `connect_profile()`: Khởi động WireProxy cho profile
- `disconnect_profile()`: Dừng WireProxy process

### Thêm tính năng mới:
1. Fork repository
2. Tạo branch mới
3. Implement tính năng
4. Test kỹ lưỡng
5. Submit pull request

## Liên hệ & Hỗ trợ

- **GitHub Issues**: Báo cáo bug hoặc yêu cầu tính năng
- **Discussions**: Thảo luận chung về project

## License

MIT License - Xem file LICENSE để biết chi tiết.

---

**Lưu ý**: Đảm bảo bạn có quyền sử dụng VPN và tuân thủ luật pháp địa phương khi sử dụng WireGuard.
