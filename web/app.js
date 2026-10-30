'use strict';

const $ = (id) => document.getElementById(id);
const state = {offset: 0, total: 0, events: [], timezone: 'Europe/Moscow', editingId: null, loading: false, searchId: 0};
const PAGE = 60;
const sessionId = (() => {
  let id = localStorage.getItem('ritm_session');
  if (!id || !/^[a-zA-Z0-9_-]{1,80}$/.test(id)) {
    id = (globalThis.crypto?.randomUUID?.() || `sess_${Math.random().toString(36).slice(2)}`).replaceAll('-', '_');
    localStorage.setItem('ritm_session', id);
  }
  return id;
})();
const fmt = (value) => value ? value.split('-').reverse().join('.') : 'Без срока';
const escapeDate = (date) => new Intl.DateTimeFormat('en-CA', {timeZone: state.timezone, year: 'numeric', month: '2-digit', day: '2-digit'}).format(date);
const weekBounds = () => {
  const day = escapeDate(new Date());
  const utc = new Date(day + 'T12:00:00Z');
  const mon = (utc.getUTCDay() + 6) % 7;
  const first = new Date(utc); first.setUTCDate(utc.getUTCDate() - mon);
  const last = new Date(first); last.setUTCDate(first.getUTCDate() + 6);
  return [first.toISOString().slice(0, 10), last.toISOString().slice(0, 10)];
};

async function request(path, opts = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...opts});
  let body;
  try { body = await response.json(); } catch { throw new Error(`Ошибка сервера (${response.status})`); }
  if (!response.ok) {
    let detail = body.detail || `HTTP ${response.status}`;
    if (Array.isArray(detail)) detail = detail.map(x => x.msg).join('; ');
    throw new Error(detail);
  }
  return body;
}
function toast(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { $('toast').hidden = true; }, 4200);
}
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function button(icon, title, onClick) {
  const b = node('button', 'icon-btn', icon);
  b.type = 'button'; b.title = title; b.setAttribute('aria-label', title); b.addEventListener('click', onClick);
  return b;
}
function renderEvents() {
  const box = $('events'); box.replaceChildren();
  $('event-count').textContent = String(state.total);
  $('stat-period').textContent = String(state.total);
  $('paging-label').textContent = `Показано ${state.events.length} из ${state.total}`;
  $('load-more').hidden = !!$('search').value.trim() || state.events.length >= state.total;
  if (!state.events.length) {
    box.append(node('div', 'empty', 'Ничего не найдено. Измените даты или поисковый запрос.'));
    return;
  }
  for (const event of state.events) {
    const row = node('div', 'event');
    row.append(node('div', 'event-icon ' + event.kind,
      event.kind === 'meeting' ? '◉' : event.kind === 'reminder' ? '◷' : '✓'));
    const body = node('div', 'event-body');
    body.append(node('span', 'event-title' + (event.status === 'done' ? ' done' : ''), event.title));
    const info = `${fmt(event.scheduled_date)}${event.scheduled_time ? ' · ' + event.scheduled_time.slice(0,5) : ''} · ${event.kind === 'meeting' ? 'Встреча' : event.kind === 'reminder' ? 'Напоминание' : 'Задача'} · #${event.id}`;
    body.append(node('span', 'event-meta', info)); row.append(body);
    const actions = node('div', 'event-actions');
    if (event.status !== 'done') actions.append(button('✓', 'Отметить выполненным', () => changeEvent(event.id, {status:'done',expected_version:event.version})));
    actions.append(button('✎', 'Редактировать', () => openModal(event)));
    actions.append(button('✕', 'Отменить', () => cancelEvent(event)));
    row.append(actions); box.append(row);
  }
}
function searchArgs(offset = 0) {
  const query = $('search').value.trim();
  const params = new URLSearchParams({limit: String(PAGE)});
  if (query) {
    params.set('q', query);
    if ($('kind-filter').value) params.set('kind', $('kind-filter').value);
    return {path: `/api/search?${params}`, search: true};
  }
  params.set('offset', String(offset));
  if ($('from-date').value) params.set('from_date', $('from-date').value);
  if ($('to-date').value) params.set('to_date', $('to-date').value);
  if ($('kind-filter').value) params.set('kind', $('kind-filter').value);
  return {path: `/api/events?${params}`, search: false};
}
async function loadEvents(more = false) {
  const requestId = ++state.searchId;
  const requestedOffset = more ? state.events.length : 0;
  const {path, search} = searchArgs(requestedOffset);
  try {
    const data = await request(path);
    if (requestId !== state.searchId) return;
    state.total = data.total;
    state.events = more && !search ? state.events.concat(data.items) : data.items;
    renderEvents();
  } catch(err) { if(requestId === state.searchId) toast(err.message); }
}
async function loadHealth() {
  try {
    const data = await request('/api/health');
    state.timezone = data.timezone;
    $('stat-tz').textContent = data.timezone.replace('Europe/', '').replace('Asia/', '');
    $('stat-total').textContent = data.records;
    const [from, to] = weekBounds();
    $('from-date').value = from; $('to-date').value = to;
    $('schedule-subtitle').textContent = 'На выбранный период';
  } catch(err) { toast(err.message); }
  await loadEvents();
}
function openModal(event = null) {
  state.editingId = event?.id || null;
  $('modal-title').textContent = event ? 'Редактирование' : 'Новое событие';
  $('event-kind').value = event?.kind || 'task';
  $('event-kind').disabled = !!event;
  $('event-title').value = event?.title || '';
  $('event-date').value = event?.scheduled_date || '';
  $('event-time').value = event?.scheduled_time?.slice(0, 5) || '';
  $('event-duration').value = event?.duration_minutes || 60;
  $('event-form').dataset.version = event?.version || '';
  $('event-form').dataset.hadDate = event?.scheduled_date ? '1' : '0';
  $('form-error').hidden = true;
  kindChanged();
  $('event-modal').showModal(); $('event-title').focus();
}
function kindChanged() {
  const kind = $('event-kind').value;
  $('duration-container').hidden = kind !== 'meeting';
  $('event-date').required = kind !== 'task';
  $('event-time').required = kind !== 'task';
}
async function saveEvent(ev) {
  ev.preventDefault();
  const kind = $('event-kind').value;
  const payload = {
    title: $('event-title').value.trim(),
    ...(state.editingId ? {} : {kind}),
    ...($('event-date').value ? {scheduled_date:$('event-date').value} : {}),
    ...($('event-time').value ? {scheduled_time:$('event-time').value} : {}),
    ...(kind === 'meeting' ? {duration_minutes:Number($('event-duration').value)} : {})
  };
  if (state.editingId) payload.expected_version = Number($('event-form').dataset.version);
  // Patches intentionally cannot clear a date/time. Warn rather than silently retaining old data.
  if (state.editingId && ((($('event-form').dataset.hadDate === '1') && !$('event-date').value) || (kind !== 'task' && !$('event-time').value))) {
    $('form-error').textContent = 'Чтобы изменить запись, укажите её дату и время. Удаление даты пока не поддерживается.';
    $('form-error').hidden = false; return;
  }
  const save = $('save-event'); save.disabled = true;
  try {
    await request(state.editingId ? `/api/events/${state.editingId}` : '/api/events', {
      method: state.editingId ? 'PATCH':'POST', body: JSON.stringify(payload)
    });
    $('event-modal').close(); toast(state.editingId ? 'Событие обновлено' : 'Событие создано');
    await loadEvents();
    const health = await request('/api/health'); $('stat-total').textContent = health.records;
  } catch(err) { $('form-error').textContent = err.message; $('form-error').hidden = false; }
  finally { save.disabled = false; }
}
async function changeEvent(id, patch) {
  try { await request(`/api/events/${id}`, {method:'PATCH',body:JSON.stringify(patch)}); toast('Запись обновлена'); await loadEvents(); }
  catch(err) {toast(err.message);}
}
async function cancelEvent(event) {
  if (!confirm(`Отменить «${event.title}»? Запись останется в базе со статусом «отменена».`)) return;
  try {await request(`/api/events/${event.id}?expected_version=${event.version}`,{method:'DELETE'}); toast('Запись отменена'); await loadEvents();}
  catch(err){toast(err.message);}
}
function appendMessage(role, content) {
  const div = node('div','message ' + role,content);
  $('messages').append(div);
  $('messages').scrollTop = $('messages').scrollHeight;
}
async function loadHistory() {
  try {
    const data = await request(`/api/sessions/${encodeURIComponent(sessionId)}/history?limit=40`);
    if (data.messages.length) for (const msg of data.messages) appendMessage(msg.role, msg.content);
    else appendMessage('assistant','Привет! Я помогу организовать задачи и встречи. Начните с команды или воспользуйтесь формой «Новое событие».');
  } catch { appendMessage('assistant','Привет! Что запланируем?'); }
}
function speak(message) {
  if (!$('speak-responses').checked || !('speechSynthesis' in window)) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(message.slice(0, 800));
  utter.lang = 'ru-RU'; utter.rate = 1.04;
  window.speechSynthesis.speak(utter);
}
async function chatSubmit(event) {
  event.preventDefault(); const text = $('chat-input').value.trim();
  if (!text || state.loading) return;
  state.loading = true; $('send').disabled = true;
  $('chat-input').value = ''; appendMessage('user', text);
  try {
    const reply = await request('/api/chat', {method:'POST',body:JSON.stringify({message:text,session_id:sessionId})});
    appendMessage('assistant',reply.answer); speak(reply.answer);
    if(['create','update','list','search'].includes(reply.action)) await loadEvents();
  } catch(err) { appendMessage('assistant','Ошибка: '+err.message); }
  finally {state.loading=false;$('send').disabled=false;$('chat-input').focus();}
}
function setupSpeech() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    $('mic').disabled = true; $('mic').title = 'Ваш браузер не поддерживает SpeechRecognition';
    $('mic-note').textContent = 'Голосовой ввод не поддерживается'; return;
  }
  const rec = new SpeechRecognition(); rec.lang='ru-RU'; rec.interimResults=false; rec.maxAlternatives=1;
  rec.onresult = (ev) => { $('chat-input').value = ev.results[0][0].transcript; $('chat-input').focus(); };
  rec.onerror = (ev) => toast(`Микрофон: ${ev.error}`);
  rec.onend = () => { $('mic').classList.remove('listening'); $('mic-note').textContent='Диктовка зависит от браузера'; };
  $('mic').addEventListener('click', () => {
    try { rec.start(); $('mic').classList.add('listening'); $('mic-note').textContent='Слушаю…'; }
    catch(err) {toast(err.message);}
  });
}

$('add-event').addEventListener('click', () => openModal());
$('close-modal').addEventListener('click', () => $('event-modal').close());
$('cancel-modal').addEventListener('click', () => $('event-modal').close());
$('event-kind').addEventListener('change', kindChanged);
$('event-form').addEventListener('submit', saveEvent);
$('chat-form').addEventListener('submit', chatSubmit);
$('from-date').addEventListener('change', () => loadEvents());
$('to-date').addEventListener('change', () => loadEvents());
$('kind-filter').addEventListener('change', () => loadEvents());
$('search').addEventListener('input', () => {clearTimeout(state.searchTimer);state.searchTimer=setTimeout(() => loadEvents(), 240);});
$('this-week').addEventListener('click', () => {const [a,b]=weekBounds();$('from-date').value=a;$('to-date').value=b;$('search').value='';loadEvents();});
$('all-time').addEventListener('click', () => {$('from-date').value='';$('to-date').value='';$('search').value='';loadEvents();});
$('load-more').addEventListener('click', () => loadEvents(true));
for (const b of document.querySelectorAll('.suggestion')) b.addEventListener('click', () => {$('chat-input').value=b.dataset.say;$('chat-input').focus();});
setupSpeech(); loadHealth(); loadHistory();
