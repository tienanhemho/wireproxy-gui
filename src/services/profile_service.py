import os
import shutil
import logging
import urllib.request
import urllib.parse
import base64
import io
import re
from urllib.parse import urlparse, parse_qs

from PyQt6.QtCore import QByteArray, QBuffer
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

# Optional QR decoders
try:
    import cv2
    import numpy as np
    _HAVE_CV2 = True
except ImportError:
    _HAVE_CV2 = False

try:
    from PIL import Image
    from pyzbar.pyzbar import decode as pyzbar_decode
    _HAVE_PYZBAR = True
except ImportError:
    _HAVE_PYZBAR = False

LOGGER = logging.getLogger("wireproxy_gui")
PROFILE_DIR = "profiles"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".svg")

def is_http_url(s: str) -> bool:
    try:
        p = urllib.parse.urlparse(s)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

class ProfileService:
    def __init__(self, state_service):
        self.state_service = state_service

    def load_profiles_from_disk(self):
        """Scan PROFILE_DIR and add any new .conf files to the state."""
        state = self.state_service.get_state()
        profiles_in_state = {p["name"] for p in state["profiles"]}
        
        os.makedirs(PROFILE_DIR, exist_ok=True)
        for file in os.listdir(PROFILE_DIR):
            if file.endswith(".conf") and not file.endswith("_wireproxy.conf"):
                name = os.path.splitext(file)[0]
                if name not in profiles_in_state:
                    new_profile = {
                        "name": name,
                        "conf_path": os.path.join(PROFILE_DIR, file),
                        "proxy_port": None, "pid": None, "running": False, "last_port": None,
                    }
                    state["profiles"].append(new_profile)
                    LOGGER.info(f"Discovered and added new profile from disk: {name}")
        
        self.state_service.save_state()

    def get_profile_host(self, profile) -> str | None:
        """Parse the WireGuard .conf to extract Endpoint host (IP/Domain)."""
        conf_path = profile.get("conf_path")
        if not conf_path or not os.path.exists(conf_path):
            return None
        
        if profile.get("_host_cache"):
            return profile["_host_cache"]

        try:
            with open(conf_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.lower().startswith("endpoint"):
                        _, _, value = line.partition("=")
                        host = value.strip().split("#")[0].strip().split(";")[0].strip()
                        if host.startswith("["): # IPv6
                            host = host.split("]")[0][1:]
                        else:
                            host = host.rsplit(":", 1)[0]
                        profile["_host_cache"] = host
                        return host
        except Exception:
            LOGGER.exception(f"Failed to parse host from {conf_path}")
        return None

    def import_from_file(self, file_path: str) -> tuple[bool, str]:
        """Import a .conf file. Returns (success, message)."""
        name = os.path.splitext(os.path.basename(file_path))[0]
        state = self.state_service.get_state()

        if any(p["name"] == name for p in state["profiles"]):
            return False, f"Profile '{name}' already exists."

        dest_path = os.path.join(PROFILE_DIR, os.path.basename(file_path))
        if os.path.exists(dest_path):
            return False, f"A file named '{os.path.basename(file_path)}' already exists in profiles/."

        try:
            shutil.copy(file_path, dest_path)
            state["profiles"].append({
                "name": name, "conf_path": dest_path,
                "proxy_port": None, "pid": None, "running": False, "last_port": None,
            })
            self.state_service.save_state()
            return True, f"Profile '{name}' imported successfully."
        except Exception as e:
            return False, f"Failed to copy file: {e}"

    def import_from_text(self, name_hint: str, conf_text: str) -> tuple[bool, str]:
        """Import from raw config text. Returns (success, message)."""
        if not conf_text or "[Interface]" not in conf_text:
            return False, "Text does not look like a WireGuard config."

        safe_name = "".join(c for c in name_hint.strip() if c.isalnum() or c in "-_") or "imported"
        
        state = self.state_service.get_state()
        existing_names = {p["name"] for p in state["profiles"]}
        
        base_name = safe_name
        idx = 1
        while safe_name in existing_names:
            safe_name = f"{base_name}_{idx}"
            idx += 1

        dest_path = os.path.join(PROFILE_DIR, f"{safe_name}.conf")
        try:
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(conf_text)
            
            state["profiles"].append({
                "name": safe_name, "conf_path": dest_path,
                "proxy_port": None, "pid": None, "running": False, "last_port": None,
            })
            self.state_service.save_state()
            return True, f"Profile '{safe_name}' imported successfully."
        except Exception as e:
            return False, f"Failed to create profile file: {e}"

    def delete_profile(self, profile_name: str):
        state = self.state_service.get_state()
        profile = next((p for p in state["profiles"] if p["name"] == profile_name), None)
        if not profile:
            return

        # Stop process if running (requires wireproxy_service, handled in UI layer)
        
        # Delete .conf file
        conf_path = profile.get("conf_path")
        if conf_path and os.path.exists(conf_path):
            try:
                os.remove(conf_path)
            except Exception:
                LOGGER.exception(f"Failed to delete conf file: {conf_path}")

        # Delete temp wireproxy conf
        temp_conf = os.path.join(PROFILE_DIR, f"{profile['name']}_wireproxy.conf")
        if os.path.exists(temp_conf):
            try:
                os.remove(temp_conf)
            except Exception:
                LOGGER.exception(f"Failed to delete temp conf file: {temp_conf}")

        state["profiles"] = [p for p in state["profiles"] if p["name"] != profile_name]
        self.state_service.save_state()

    def update_profile(self, old_name: str, new_name: str, new_content: str) -> tuple[bool, str]:
        state = self.state_service.get_state()
        profile = next((p for p in state["profiles"] if p["name"] == old_name), None)
        if not profile:
            return False, "Profile not found."

        # Handle rename
        if old_name != new_name:
            if any(p["name"] == new_name for p in state["profiles"]):
                return False, "A profile with the new name already exists."
            
            old_path = profile["conf_path"]
            new_path = os.path.join(PROFILE_DIR, f"{new_name}.conf")
            
            if os.path.exists(new_path):
                return False, f"A file named '{new_name}.conf' already exists."
            
            try:
                os.rename(old_path, new_path)
                profile["name"] = new_name
                profile["conf_path"] = new_path
            except Exception as e:
                return False, f"Failed to rename file: {e}"

        # Write new content
        try:
            with open(profile["conf_path"], "w", encoding="utf-8") as f:
                f.write(new_content)
            # Invalidate host cache
            profile.pop("_host_cache", None)
            self.state_service.save_state()
            return True, "Profile updated successfully."
        except Exception as e:
            return False, f"Failed to write to config file: {e}"

    def _decode_svg_bytes_to_png_bytes(self, svg_data: bytes) -> bytes | None:
        """Renders SVG data to an in-memory PNG image using Qt."""
        try:
            renderer = QSvgRenderer()
            renderer.load(QByteArray(svg_data))
            if not renderer.isValid():
                return None

            size = renderer.defaultSize()
            if size.isEmpty():
                size.setWidth(300)
                size.setHeight(300)
            
            image = QImage(size, QImage.Format.Format_ARGB32)
            image.fill(0x00000000) # Transparent background

            painter = QPainter(image)
            renderer.render(painter)
            painter.end()

            byte_array = QByteArray()
            buffer = QBuffer(byte_array)
            buffer.open(QBuffer.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            buffer.close()
            
            return byte_array.data()
        except Exception:
            LOGGER.exception("Failed to convert SVG bytes to PNG")
            return None

    def decode_qr_from_path(self, image_path: str) -> str | None:
        """Decodes a QR code from an image file, with special handling for SVG."""
        
        if image_path.lower().endswith(".svg"):
            try:
                with open(image_path, "rb") as f:
                    svg_bytes = f.read()
                png_bytes = self._decode_svg_bytes_to_png_bytes(svg_bytes)
                if png_bytes:
                    return self.decode_qr_from_bytes(png_bytes)
            except Exception:
                LOGGER.exception(f"Failed to read or process SVG file: {image_path}")
            return None

        # Fallback to original methods for raster images
        if _HAVE_CV2:
            try:
                img = cv2.imread(image_path)
                if img is not None:
                    detector = cv2.QRCodeDetector()
                    data, _, _ = detector.detectAndDecode(img)
                    if data:
                        return data.strip()
            except Exception: pass
        
        if _HAVE_PYZBAR:
            try:
                with Image.open(image_path) as im:
                    for r in pyzbar_decode(im):
                        if r.data:
                            return r.data.decode("utf-8", "ignore").strip()
            except Exception: pass
        return None

    def decode_qr_from_bytes(self, data: bytes) -> str | None:
        """Decodes a QR code from bytes, with special handling for SVG."""
        # Logic to detect SVG content 
        print("Decoding QR from bytes, data size:", len(data))
        # Simple check if data is SVG
        if b"<svg" in data.lower():
            png_bytes = self._decode_svg_bytes_to_png_bytes(data)
            if not png_bytes:
                return None
            data = png_bytes # Continue with the converted PNG data

        if _HAVE_CV2:
            try:
                arr = np.frombuffer(data, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None:
                    detector = cv2.QRCodeDetector()
                    text, _, _ = detector.detectAndDecode(img)
                    if text:
                        return text.strip()
            except Exception: pass

        if _HAVE_PYZBAR:
            try:
                with Image.open(io.BytesIO(data)) as im:
                    for r in pyzbar_decode(im):
                        if r.data:
                            return r.data.decode("utf-8", "ignore").strip()
            except Exception: pass
        return None

    def download_text_from_url(self, url: str, timeout: int = 15) -> tuple[str, str] | None:
        """Downloads text content from a URL. Returns (name_hint, content_text)."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                # Ensure it's not an image, treat as text config
                if resp.headers.get("Content-Type", "").lower().startswith("image/"):
                    LOGGER.warning(f"URL pointed to an image, but was expected to be a config file: {url}")
                    return None
                
                raw_data = resp.read()
                path = urllib.parse.urlparse(url).path
                name_hint = os.path.splitext(os.path.basename(path) or "downloaded")[0]
                return (name_hint, raw_data.decode("utf-8", "ignore"))
        except Exception:
            LOGGER.exception(f"Failed to download text from URL: {url}")
            return None

    def import_from_wireguard_url(self, url: str) -> tuple[bool, str]:
        """Parses a wireguard:// URL and imports it as a profile."""
        try:
            url = url.rstrip('.,;')
            if not url.startswith("wireguard://"):
                return False, "Not a wireguard:// URL"

            # Manual parsing to handle special characters like '%' in the private key.
            # Format: wireguard://<private_key>@<endpoint>?<query>#<fragment>
            main_part = url[len("wireguard://"):]
            
            at_pos = -1
            query_pos = main_part.find('?')
            # Find the last '@' before the query string begins
            if query_pos != -1:
                at_pos = main_part.rfind('@', 0, query_pos)
            else:
                at_pos = main_part.rfind('@')

            if at_pos == -1:
                return False, "URL format error: missing '@' separator before endpoint."

            private_key_encoded = main_part[:at_pos]
            private_key = urllib.parse.unquote(private_key_encoded)

            endpoint_and_the_rest = main_part[at_pos+1:]
            
            # Use urlparse on the remainder, which is safer. Add a dummy scheme.
            temp_url_for_parsing = f"dummy://{endpoint_and_the_rest}"
            parsed = urlparse(temp_url_for_parsing)

            endpoint = parsed.hostname
            if parsed.port:
                endpoint += f":{parsed.port}"

            params = parse_qs(parsed.query)
            public_key = params.get("publickey", [""])[0]
            address = params.get("address", [""])[0]
            name_hint = urllib.parse.unquote(parsed.fragment or "wg_import")

            if not all([private_key, endpoint, public_key, address]):
                missing = []
                if not private_key: missing.append("privatekey (before @)")
                if not endpoint: missing.append("endpoint (after @)")
                if not public_key: missing.append("publickey parameter")
                if not address: missing.append("address parameter")
                return False, f"URL is missing required fields: {', '.join(missing)}"

            # Build the .conf content
            lines = [
                "[Interface]",
                f"PrivateKey = {private_key}",
                f"Address = {address}",
            ]
            if params.get("mtu"):
                lines.append(f"MTU = {params['mtu'][0]}")

            lines.append("\n[Peer]")
            lines.append(f"PublicKey = {public_key}")
            lines.append(f"Endpoint = {endpoint}")
            
            # Add allowed IPs, default to 0.0.0.0/0 if not present
            allowed_ips = params.get("allowedips", ["0.0.0.0/0"])[0]
            lines.append(f"AllowedIPs = {allowed_ips}")

            if params.get("keepalive"):
                lines.append(f"PersistentKeepalive = {params['keepalive'][0]}")

            # Add custom WireProxy params as comments
            custom_params = {
                "wnoise": "WNoise", "wpayloadsize": "WPayloadSize",
                "wquic": "WQUIC", "wnoisecount": "WNoiseCount",
                "wnoisedelay": "WNoiseDelay"
            }
            for key, conf_key in custom_params.items():
                if key in params:
                    lines.append(f"# {conf_key} = {params[key][0]}")

            conf_text = "\n".join(lines)
            return self.import_from_text(name_hint, conf_text)

        except Exception as e:
            LOGGER.exception(f"Failed to parse wireguard:// URL: {url}")
            return False, f"Error parsing URL: {e}"

    def import_from_clipboard_text(self, text: str) -> int:
        """Finds all wireguard:// URLs in a block of text and imports them."""
        urls = re.findall(r"wireguard://[^\s\'\"<]+", text)
        if not urls:
            return 0
        
        imported_count = 0
        for url in urls:
            success, msg = self.import_from_wireguard_url(url)
            if success:
                imported_count += 1
            else:
                LOGGER.warning(f"Failed to import from URL '{url}': {msg}")
        
        return imported_count
