"""Windows-friendly development entry point with Render PORT support."""
import logging
import os

from assistant.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("assistant.bootstrap")
logger.info("Запуск API сервиса (uvicorn)")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    # Render назначает PORT и требует слушать 0.0.0.0. Без PORT сохраняем
    # прежний безопасный локальный запуск на 127.0.0.1:8000.
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    uvicorn.run("assistant.api:app", host=host, port=port, log_level="info")
