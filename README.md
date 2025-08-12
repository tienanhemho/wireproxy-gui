# WireProxy GUI Manager

PyQt6 desktop app to manage WireGuard profiles and spawn local proxies via WireProxy.

## Description

WireProxy GUI Manager lets you import, manage, connect, and disconnect WireGuard profiles easily, and start a local HTTP or SOCKS5 proxy via WireProxy.

## Requirements

- **Python 3.10+**
- **PyQt6**
- **WireProxy binary** (if not found in PATH, the app lets you choose the executable)
- **Windows PowerShell** (tested)

## Setup

### 1) Clone

```bash
git clone <repo-url>
cd wireproxy-gui
```

### 2) Virtual environment

```powershell
python -m venv venv
./venv/Scripts/Activate.ps1
```

### 3) Install dependencies

```powershell
pip install PyQt6
```

### 4) Install WireProxy

Download the WireProxy binary from `https://github.com/octeep/wireproxy/releases` and put it in PATH or select it in the app.

## Usage

### 1) Run the app

```powershell
# With activated venv
python app.py

# Or via the venv python
./venv/Scripts/python.exe app.py

# First-time WireProxy setup
# If not found in PATH, right-click on the table background → "Configure WireProxy path…" and select wireproxy.exe
```

### 2) Import WireGuard profile

Two ways:

#### Method A: Drag & Drop
1. Drag a `.conf` from Explorer
2. Drop into the app window
3. The app imports it and shows a notification

#### Method B: Import button
1. Click the “Drag and drop a .conf here or click to choose” button
2. Pick a `.conf` in the dialog
3. The file will be copied into the `profiles/` folder

### 3) Manage profiles

The table shows:
- **Profile Name**: Config filename without extension
- **Proxy Port**: Port chosen for the local proxy
- **Status**: “Running” or “Stopped”

### 4) Connect/Disconnect

- **Connect**: start a proxy for that profile
- **Disconnect**: stop the proxy
- Port is chosen automatically in range 60000–65535, or right-click a row → “Connect (pick port)” to pick within the limit.

### 5) Proxy type (SOCKS5/HTTP)

- Choose “Proxy type” at the top (SOCKS5 or HTTP).
- Stored in `state.json` and used when generating WireProxy config.
- Default: SOCKS5.

### 6) Active ports limit

- “Active ports limit” at the top controls how many concurrent ports to allow.
- Default: 10. Use 0 for unlimited.
- Right-click on background → “Auto-connect up to limit” to connect sequentially until the limit is hit.

## Folder structure

```
wireproxy-gui/
├── app.py                     # Main app
├── state.json                 # User state (auto-created, gitignored)
├── state.example.json         # Example state
├── profiles/                  # WireGuard .conf files (gitignored)
│   ├── profile1.conf
│   ├── profile2.conf
│   └── ...
├── venv/                      # Virtual environment (gitignored)
├── test_profile.conf          # Sample
└── README.md                  # This guide
```

## State, versioning, migration

- `state.json` is personal data and is ignored by git.
- Schema includes `version`; the code has `STATE_VERSION`.
- On startup, old schemas are migrated and a backup is written as `state.json.bak-<timestamp>`.
- Default `port_limit = 10` (editable in the UI and persisted).
- Since v2, `proxy_type` is supported (`"socks"` or `"http"`).
- Use `state.example.json` as a clean template.

## .gitignore

- Ignored: `profiles/`, `venv/`, `state.json`. Only source code and docs should be committed.

## Features

### ✅ Done
- [x] Import WireGuard profiles (.conf)
- [x] Drag & Drop support
- [x] Persist profile states (JSON)
- [x] Auto-pick free port
- [x] Connect/Disconnect
- [x] Process status check
- [x] PyQt6 UI
- [x] Duplicate detection
- [x] Import notifications
- [x] Port override within app (confirm → disconnect old → reuse port)
- [x] Proxy type selection (SOCKS5/HTTP)
- [x] Delete/Edit profile (context menu)
- [x] Quick port picking within limit (context menu)
- [x] Limit active ports (UI)

### 🔄 Roadmap
- [ ] Export profile
- [ ] Logs viewer
- [ ] System tray integration
- [ ] Auto-start profiles
- [ ] Profile groups/categories

## Troubleshooting

Common issues:

1. **“wireproxy not found in PATH”**
   - Install WireProxy and ensure it’s in PATH, or select the executable in the app.

2. **“No free port found”**
   - Check firewall
   - Adjust the port range in code if necessary

3. **“Profile already exists”**
   - Rename the file before import, or delete the existing one in `profiles/`

4. **“ImportError: cannot import name 'QtWidgets' from 'PyQt6'”**
   - Install PyQt6: `pip install PyQt6`
   - Activate the virtual environment

## Sample WireGuard config

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

## Development

### Code structure
- `WireProxyManager`: main GUI class
- `load_state()`: load state from JSON
- `save_state()`: persist state to JSON
- `import_profile_file()`: import a profile
- `connect_profile()`: start WireProxy for a profile
- `disconnect_profile()`: stop WireProxy process

### Contributing
1. Fork
2. Create a feature branch
3. Implement
4. Test thoroughly
5. Open a pull request

## Support

- **GitHub Issues**: bug reports and feature requests
- **Discussions**: general discussions

## License

MIT License — see `LICENSE` for details.

---

Note: Ensure you have the right to use VPNs and comply with local laws when using WireGuard.
