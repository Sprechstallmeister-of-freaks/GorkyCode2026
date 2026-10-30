from pathlib import Path
from assistant.service import Planner
from assistant.database import Database
from assistant.config import PROJECT_ROOT


def test_real_seed_all_1000_and_no_duplications(tmp_path):
    planner=Planner(Database(tmp_path/'seed.db'), 'Europe/Moscow')
    source=PROJECT_ROOT/'data/events.csv'
    assert source.exists()
    assert planner.import_csv(source)==1000
    assert planner.import_csv(source)==0
    assert planner.list(limit=1)[1]==1000
    assert planner.get(1).title == 'Подготовить техническое задание'
