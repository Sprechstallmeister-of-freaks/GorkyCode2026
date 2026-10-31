from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_path: Path
    timezone: str = "Europe/Moscow"
    seed_csv: Path | None = None
    import_seed: bool = True
    deepseek_api_key: str | None = field(default=None, repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: float = 30.0

    def __post_init__(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {self.timezone}") from exc
        if self.deepseek_timeout_seconds <= 0:
            raise ValueError("DeepSeek timeout must be positive")


def from_env() -> Settings:
    csv_name = os.getenv("ASSISTANT_SEED_CSV", str(PROJECT_ROOT / "data" / "events.csv"))
    return Settings(
        db_path=Path(os.getenv("ASSISTANT_DB", str(PROJECT_ROOT / "assistant.db"))),
        timezone=os.getenv("ASSISTANT_TIMEZONE", "Europe/Moscow"),
        seed_csv=Path(csv_name) if csv_name else None,
        import_seed=os.getenv("ASSISTANT_IMPORT_SEED", "1") == "1",
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip() or None,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        deepseek_timeout_seconds=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "30")),
    )
