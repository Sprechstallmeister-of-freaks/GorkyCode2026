from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_path: Path
    timezone: str = "Europe/Moscow"
    seed_csv: Path | None = None
    import_seed: bool = True
    deepseek_api_key: str | None = field(default=None, repr=False)
    deepseek_api_keys: tuple[str, ...] = field(default_factory=tuple, repr=False)
    deepseek_base_url: str = "https://api.literouter.com/v1"
    deepseek_model: str = "deepseek-v4.1-flash-1017:free"
    deepseek_timeout_seconds: float = 30.0

    def __post_init__(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {self.timezone}") from exc
        if self.deepseek_timeout_seconds <= 0:
            raise ValueError("DeepSeek timeout must be positive")

    @property
    def deepseek_keys(self) -> tuple[str, ...]:
        candidates = list(self.deepseek_api_keys)
        if self.deepseek_api_key:
            candidates.insert(0, self.deepseek_api_key)
        return tuple(dict.fromkeys(key for key in candidates if key))


def from_env() -> Settings:
    # Real environment variables take precedence over the optional local file.
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    csv_name = os.getenv("ASSISTANT_SEED_CSV", str(PROJECT_ROOT / "data" / "events.csv"))
    api_keys = tuple(key.strip() for key in os.getenv("DEEPSEEK_API_KEYS", "").split(",") if key.strip())
    return Settings(
        db_path=Path(os.getenv("ASSISTANT_DB", str(PROJECT_ROOT / "assistant.db"))),
        timezone=os.getenv("ASSISTANT_TIMEZONE", "Europe/Moscow"),
        seed_csv=Path(csv_name) if csv_name else None,
        import_seed=os.getenv("ASSISTANT_IMPORT_SEED", "1") == "1",
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip() or None,
        deepseek_api_keys=api_keys,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.literouter.com/v1"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4.1-flash-1017:free"),
        deepseek_timeout_seconds=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "30")),
    )
