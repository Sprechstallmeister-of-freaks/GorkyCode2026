from assistant.tools import ToolRegistry


def test_schemas_all_tools_declared(planner):
    specs=ToolRegistry(planner).specs()
    assert {x['function']['name'] for x in specs}=={'create_event','get_event','list_events','search_events','update_event','cancel_event'}
    assert all(x['function']['parameters'] for x in specs)


def test_tool_happy_path_and_no_sql(planner):
    tools=ToolRegistry(planner)
    created=tools.execute('create_event',{'kind':'task','title':'Отправить договор','scheduled_date':'2026-11-02'})
    assert created['ok']
    eid=created['event']['id']
    assert tools.execute('get_event',{'event_id':eid})['ok']
    assert tools.execute('search_events',{'query':'договору'})['items'][0]['id']==eid
    assert tools.execute('list_events',{'from_date':'2026-11-02','to_date':'2026-11-02'})['total']==1
    assert tools.execute('update_event',{'event_id':eid,'status':'done'})['event']['status']=='done'
    assert not tools.execute('cancel_event',{'event_id':eid})['ok']
    assert tools.execute('cancel_event',{'event_id':eid},confirmed_by_user=True)['ok']
    assert not tools.execute('cancel_event',{'event_id':eid,'confirm':True},confirmed_by_user=False)['ok']
    assert not tools.execute('create_event',{'kind':'task','title':'x','raw_sql':'DROP TABLE events'})['ok']
    assert not tools.execute('arbitrary_python',{})['ok']


def test_tool_bad_dates_and_conflicts(planner):
    t=ToolRegistry(planner)
    assert not t.execute('create_event',{'kind':'meeting','title':'Meeting'})['ok']
    assert t.execute('create_event',{'kind':'meeting','title':'A','scheduled_date':'2026-11-01','scheduled_time':'15:00'})['ok']
    result=t.execute('create_event',{'kind':'meeting','title':'B','scheduled_date':'2026-11-01','scheduled_time':'15:30'})
    assert result['error']=='conflict'
