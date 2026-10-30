from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_path: Path
    timezone: str = "Europe/Moscow"
    seed_csv: Path | None = None
    import_seed: bool = True

    def __post_init__(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {self.timezone}") from exc


def from_env() -> Settings:
    csv_name = os.getenv("ASSISTANT_SEED_CSV", str(PROJECT_ROOT / "data" / "events.csv"))
    return Settings(
        db_path=Path(os.getenv("ASSISTANT_DB", str(PROJECT_ROOT / "assistant.db"))),
        timezone=os.getenv("ASSISTANT_TIMEZONE", "Europe/Moscow"),
        seed_csv=Path(csv_name) if csv_name else None,
        import_seed=os.getenv("ASSISTANT_IMPORT_SEED", "1") == "1",
    )
