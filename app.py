import sys
import os
import logging
from logging.handlers import RotatingFileHandler
from PyQt6 import QtWidgets

from src.services.state_service import StateService
from src.services.profile_service import ProfileService
from src.services.wireproxy_service import WireProxyService
from src.services.geoip_service import GeoIPService
from src.services.auto_connect_service import AutoConnectService
from src.ui.main_window import MainWindow

LOG_DIR = "logs"

def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("wireproxy_gui")
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.DEBUG)
    
    # File handler
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

def main():
    # Setup logging first
    logger = setup_logging()

    # Create the application
    app = QtWidgets.QApplication(sys.argv)

    # Instantiate services
    state_service = StateService()
    profile_service = ProfileService(state_service)
    wireproxy_service = WireProxyService(state_service)
    geoip_service = GeoIPService()
    auto_connect_service = AutoConnectService(state_service, wireproxy_service)

    # Set logger level based on loaded state
    if not state_service.get_state().get("logging_enabled", True):
        logger.setLevel(logging.CRITICAL)

    # Create and show the main window
    window = MainWindow(
        state_service=state_service,
        profile_service=profile_service,
        wireproxy_service=wireproxy_service,
        geoip_service=geoip_service,
        auto_connect_service=auto_connect_service
    )
    window.show()

    # Run the application event loop
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
