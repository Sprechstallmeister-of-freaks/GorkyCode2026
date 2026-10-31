"""Windows-friendly development entry point."""
import logging

from assistant.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("assistant.bootstrap")
logger.info("Запуск API сервиса (uvicorn)")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("assistant.api:app", host="127.0.0.1", port=8000, log_level="info")
