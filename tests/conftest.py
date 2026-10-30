import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from assistant.config import Settings
from assistant.database import Database
from assistant.service import Planner
from assistant.api import create_app


@pytest.fixture
def planner(tmp_path):
    return Planner(Database(tmp_path / "test.db"), "Europe/Moscow")


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "api.db", timezone="Europe/Moscow", seed_csv=None, import_seed=False))
    with TestClient(app) as c:
        yield c
