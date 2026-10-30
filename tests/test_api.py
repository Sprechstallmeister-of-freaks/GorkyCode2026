
def test_health_and_docs(client):
    assert client.get('/api/health').json()['llm_connected'] is False
    assert client.get('/').status_code==200
    assert client.get('/docs').status_code==200
    assert len(client.get('/api/tools').json()['tools'])==6


def test_crud(client):
    r=client.post('/api/events',json={'kind':'meeting','title':'Планёрка','scheduled_date':'2026-11-04','scheduled_time':'15:00','duration_minutes':45})
    assert r.status_code==201,r.text
    id=r.json()['id']
    assert client.get(f'/api/events/{id}').json()['title']=='Планёрка'
    assert client.get('/api/events',params={'from_date':'2026-11-04','to_date':'2026-11-04'}).json()['total']==1
    assert client.post('/api/events',json={'kind':'meeting','title':'Пересечение','scheduled_date':'2026-11-04','scheduled_time':'15:20'}).status_code==409
    assert client.patch(f'/api/events/{id}',json={'title':'Новый заголовок','expected_version':1}).status_code==200
    assert client.patch(f'/api/events/{id}',json={'title':'Старый запрос','expected_version':1}).status_code==409
    assert client.delete(f'/api/events/{id}',params={'expected_version':2}).status_code==200
    assert client.get('/api/events').json()['total']==0
    assert client.get('/api/events',params={'include_cancelled':True}).json()['total']==1


def test_validation_and_search(client):
    assert client.post('/api/events',json={'kind':'meeting','title':'Без даты'}).status_code==422
    assert client.post('/api/events',json={'kind':'task','title':'X','extra':'bad'}).status_code==422
    assert client.patch('/api/events/100',json={'title':'missing'}).status_code==404
    assert client.get('/api/events',params={'from_date':'2026-11-10','to_date':'2026-11-01'}).status_code==422
    client.post('/api/events',json={'kind':'task','title':'Проверить договор'})
    result=client.get('/api/search',params={'q':'договору'}).json()
    assert result['strategy']=='lexical_fuzzy' and result['items'][0]['title']=='Проверить договор'


def test_chat_session(client):
    a=client.post('/api/chat',json={'session_id':'demo','message':'Добавь встречу с Иваном завтра'}).json()
    assert a['action']=='clarify'
    b=client.post('/api/chat',json={'session_id':'demo','message':'в 15:00'}).json()
    assert b['action']=='create' and b['event']['kind']=='meeting'
    assert len(client.get('/api/sessions/demo/history').json()['messages'])==4
