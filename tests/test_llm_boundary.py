"""Safety and external-model interface contract tests."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from assistant.api import create_app
from assistant.config import Settings
from assistant.contracts import LLMContext, ParsedCommand
from assistant.models import EventCreate
from assistant.routing import ChatRouter
from assistant.tools import ToolRegistry


class SpyProvider:
    def __init__(self, command):
        self.command = command
        self.contexts = []
        self.schemas = []

    def interpret(self, text, context, tools):
        self.contexts.append(context.model_dump(mode='json'))
        self.schemas.append(tools)
        return self.command

    def expand_search(self, query, context):
        self.contexts.append(context.model_dump(mode='json'))
        return []


def test_llm_json_context_only_now_and_iana_timezone(planner):
    provider = SpyProvider(ParsedCommand(tool='unknown'))
    r = ChatRouter(planner, provider=provider)
    r.process('какая-то незнакомая фраза', 'boundary_sid')
    assert provider.contexts
    payload = provider.contexts[0]
    assert set(payload) == {'now', 'timezone'}
    assert payload['timezone'] == planner.default_timezone
    now = datetime.fromisoformat(payload['now'])
    assert now.utcoffset() == now.astimezone(ZoneInfo(payload['timezone'])).utcoffset()
    assert {x['function']['name'] for x in provider.schemas[0]} == {
        'create_event', 'list_events', 'search_events',
        'get_event', 'update_event', 'cancel_event'
    }


def test_context_schema_rejects_extra_fields_naive_timestamp_and_bad_zone():
    now = datetime.now(ZoneInfo('Europe/London'))
    with pytest.raises(ValidationError):
        LLMContext(now=now, timezone='Europe/London', session_id='private')
    with pytest.raises(ValidationError):
        LLMContext(now=datetime(2026, 11, 2, 12), timezone='Europe/London')
    with pytest.raises(ValidationError):
        LLMContext(now=now, timezone='Invalid/Zone')


def test_models_cannot_ask_for_sql_or_dynamic_tools(planner):
    registry = ToolRegistry(planner)
    assert registry.execute('raw_sql', {'sql': 'DROP TABLE events'})['error'] == 'unknown_tool'
    sql_payload = {'kind': 'task', 'title': 'Keep record', 'sql': 'DELETE FROM events'}
    assert registry.execute('create_event', sql_payload)['error'] == 'invalid_arguments'
    assert planner.list()[1] == 0


def test_cancel_requires_separate_user_confirmation_and_cannot_be_bypassed(planner):
    event = planner.create(EventCreate(kind='task', title='Не удалять молча'))
    registry = ToolRegistry(planner)
    assert registry.execute('cancel_event', {'event_id': event.id})['error'] == 'confirmation_required'
    assert registry.execute('cancel_event', {'event_id': event.id, 'confirm': True})['error'] == 'invalid_arguments'
    assert registry.execute('update_event', {'event_id': event.id, 'status': 'cancelled'})['error'] == 'confirmation_required'
    provider = SpyProvider(ParsedCommand(tool='cancel_event', arguments={'event_id': event.id}))
    r = ChatRouter(planner, provider=provider)
    assert r.process('да', 'confirmation_sid').action != 'cancel_event'
    assert planner.get(event.id).status.value == 'open'
    assert r.process('избавься от этой записи', 'confirmation_sid').action == 'clarify'
    assert planner.get(event.id).status.value == 'open'
    assert r.process('нет', 'confirmation_sid').action == 'cancel_pending'
    assert planner.get(event.id).status.value == 'open'
    assert r.process('избавься от этой записи', 'confirmation_sid').action == 'clarify'
    assert r.process('да', 'confirmation_sid').action == 'cancel_event'
    assert planner.get(event.id).status.value == 'cancelled'


def test_http_cancel_is_two_step_and_stale_confirmation_fails(tmp_path):
    app = create_app(Settings(db_path=tmp_path/'boundary.db', seed_csv=None, import_seed=False))
    with TestClient(app) as client:
        event = client.post('/api/events', json={'kind': 'task', 'title': 'Защищенная запись'}).json()
        path = f"/api/events/{event['id']}"
        assert client.delete(path).status_code == 409
        assert client.patch(path, json={'status': 'cancelled'}).status_code == 409
        assert client.post(path + '/cancel-request', json={'session_id':'s1'}).status_code == 200
        # Version changes between prompt and confirmation: prohibit stale cancellation.
        assert client.patch(path, json={'title':'Изменённая запись', 'expected_version':1}).status_code == 200
        confirmed = client.post('/api/chat', json={'session_id':'s1','message':'да'}).json()
        assert confirmed['action'] == 'conflict'
        assert client.get(path).json()['status'] == 'open'
