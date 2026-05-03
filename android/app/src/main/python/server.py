"""
webAgent Python bootstrap for Android.
Starts uvicorn to serve the FastAPI app on localhost.
"""

import os
import sys
import threading
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webagent.server")

_app_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _app_dir)

os.environ.setdefault("DB_MODE", "local")

# Apply any saved provider config (API key) from previous sessions
try:
    from app.admin.settings import apply_provider_config
    apply_provider_config()
except Exception as e:
    logger.warning("Could not apply saved provider config: %s", e)

_server_ref = None


def start_server(port=8000):
    from app.main import app
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="info", reload=False, workers=1)
    global _server_ref
    _server_ref = uvicorn.Server(config)

    def run():
        try:
            _server_ref.run()
        except Exception as e:
            logger.error("Server error: %s", e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    logger.info("Server running on port %d", port)


def stop_server():
    global _server_ref
    if _server_ref:
        _server_ref.should_exit = True
        _server_ref = None
        logger.info("Server stopped")
