import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client.js'
import EventModal from './EventModal.jsx'
import { IconCheck, IconChevron, IconClose, IconPencil, IconPlus } from './icons.jsx'

const KIND_LABELS = { task: 'Задача', meeting: 'Встреча', reminder: 'Напоминание' }
const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

export function toISO(date) {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

function gridRange(month) {
  const first = new Date(month.getFullYear(), month.getMonth(), 1)
  const startOffset = (first.getDay() + 6) % 7 // неделя с понедельника
  const start = new Date(first)
  start.setDate(first.getDate() - startOffset)
  const end = new Date(start)
  end.setDate(start.getDate() + 41)
  return { start, end }
}

export default function CalendarPanel({ sessionId, eventsVersion, onEventsChanged }) {
  const todayISO = toISO(new Date())
  const [month, setMonth] = useState(() => {
    const now = new Date()
    return new Date(now.getFullYear(), now.getMonth(), 1)
  })
  const [selected, setSelected] = useState(todayISO)
  const [events, setEvents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [kindFilter, setKindFilter] = useState('all')
  const [modal, setModal] = useState(null) // { event? } — null = закрыто
  const [cancelState, setCancelState] = useState(null) // { id, question, busy }
  const [actionError, setActionError] = useState('')

  const { start, end } = useMemo(() => gridRange(month), [month])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await api.listEvents(toISO(start), toISO(end))
      setEvents(data.items)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [start, end])

  useEffect(() => { load() }, [load, eventsVersion])

  const cells = useMemo(() => {
    const list = []
    const cursor = new Date(start)
    for (let i = 0; i < 42; i += 1) {
      const iso = toISO(cursor)
      list.push({
        iso,
        day: cursor.getDate(),
        inMonth: cursor.getMonth() === month.getMonth(),
        events: events.filter((e) => e.scheduled_date === iso),
      })
      cursor.setDate(cursor.getDate() + 1)
    }
    return list
  }, [start, month, events])

  const agenda = useMemo(() => {
    return events
      .filter((e) => e.scheduled_date === selected)
      .filter((e) => kindFilter === 'all' || e.kind === kindFilter)
      .sort((a, b) => (a.scheduled_time || '99:99').localeCompare(b.scheduled_time || '99:99'))
  }, [events, selected, kindFilter])

  const monthTitle = useMemo(() => {
    const title = month.toLocaleDateString('ru-RU', { month: 'long', year: 'numeric' })
    return title.charAt(0).toUpperCase() + title.slice(1)
  }, [month])

  const selectedTitle = useMemo(() => {
    const date = new Date(`${selected}T12:00:00`)
    const title = date.toLocaleDateString('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' })
    return title.charAt(0).toUpperCase() + title.slice(1)
  }, [selected])

  const shiftMonth = (delta) => {
    setMonth((m) => new Date(m.getFullYear(), m.getMonth() + delta, 1))
  }

  const goToday = () => {
    const now = new Date()
    setMonth(new Date(now.getFullYear(), now.getMonth(), 1))
    setSelected(toISO(now))
  }

  const toggleDone = async (event) => {
    setActionError('')
    try {
      await api.updateEvent(event.id, {
        status: event.status === 'done' ? 'open' : 'done',
        expected_version: event.version,
      })
      onEventsChanged()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const startCancel = async (event) => {
    setActionError('')
    try {
      const result = await api.cancelRequest(event.id, sessionId)
      setCancelState({ id: event.id, question: result.answer, busy: false })
    } catch (e) {
      setActionError(e.message)
    }
  }

  const confirmCancel = async (answer) => {
    if (!cancelState) return
    setCancelState((s) => ({ ...s, busy: true }))
    try {
      await api.chat(answer, sessionId)
      setCancelState(null)
      onEventsChanged()
    } catch (e) {
      setActionError(e.message)
      setCancelState(null)
    }
  }

  return (
    <div className="calendar-panel">
      <div className="panel-header">
        <h2>Календарь</h2>
        <button className="btn btn-accent" onClick={() => setModal({})}>
          <IconPlus /> <span>Новое событие</span>
        </button>
      </div>

      <div className="month-card card">
        <div className="month-nav">
          <button className="icon-btn" onClick={() => shiftMonth(-1)} aria-label="Предыдущий месяц">
            <IconChevron left />
          </button>
          <button className="month-title" onClick={goToday} title="Вернуться к сегодняшнему дню">
            {monthTitle}
          </button>
          <button className="icon-btn" onClick={() => shiftMonth(1)} aria-label="Следующий месяц">
            <IconChevron />
          </button>
        </div>
        <div className="weekday-row">
          {WEEKDAYS.map((d) => <span key={d}>{d}</span>)}
        </div>
        <div className="month-grid">
          {cells.map((cell) => (
            <button
              key={cell.iso}
              className={[
                'day-cell',
                !cell.inMonth ? 'outside' : '',
                cell.iso === todayISO ? 'today' : '',
                cell.iso === selected ? 'selected' : '',
              ].join(' ')}
              onClick={() => {
                setSelected(cell.iso)
                if (!cell.inMonth) setMonth(new Date(`${cell.iso}T12:00:00`))
              }}
            >
              <span className="day-number">{cell.day}</span>
              <span className="day-dots">
                {cell.events.slice(0, 3).map((e) => (
                  <i key={e.id} className={`dot kind-${e.kind} ${e.status === 'done' ? 'done' : ''}`} />
                ))}
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="agenda-header">
        <h3>{selected === todayISO ? 'Сегодня' : selectedTitle}</h3>
        <span className="agenda-count">{agenda.length || ''}</span>
      </div>

      <div className="kind-filters">
        {[['all', 'Все'], ['task', 'Задачи'], ['meeting', 'Встречи'], ['reminder', 'Напоминания']].map(([value, label]) => (
          <button
            key={value}
            className={`chip ${kindFilter === value ? 'active' : ''}`}
            onClick={() => setKindFilter(value)}
          >
            {label}
          </button>
        ))}
      </div>

      {(error || actionError) && <div className="inline-error">{error || actionError}</div>}

      <div className="agenda-list">
        {loading && <div className="agenda-empty">Загрузка…</div>}
        {!loading && agenda.length === 0 && (
          <div className="agenda-empty">
            <p>На этот день планов нет</p>
            <button className="btn btn-ghost" onClick={() => setModal({})}>Добавить событие</button>
          </div>
        )}
        {agenda.map((event) => (
          <div key={event.id} className={`event-card card kind-border-${event.kind} ${event.status === 'done' ? 'is-done' : ''}`}>
            <div className="event-main">
              <button
                className={`done-toggle ${event.status === 'done' ? 'checked' : ''}`}
                onClick={() => toggleDone(event)}
                title={event.status === 'done' ? 'Вернуть в работу' : 'Отметить выполненным'}
              >
                {event.status === 'done' && <IconCheck size={13} />}
              </button>
              <div className="event-info">
                <span className="event-title">{event.title}</span>
                <span className="event-meta">
                  <i className={`dot kind-${event.kind}`} />
                  {KIND_LABELS[event.kind]}
                  {event.scheduled_time && ` · ${event.scheduled_time.slice(0, 5)}`}
                  {event.kind === 'meeting' && event.duration_minutes ? ` · ${event.duration_minutes} мин` : ''}
                </span>
              </div>
              <div className="event-actions">
                <button className="icon-btn subtle" onClick={() => setModal({ event })} title="Редактировать">
                  <IconPencil />
                </button>
                <button className="icon-btn subtle danger" onClick={() => startCancel(event)} title="Отменить событие">
                  <IconClose />
                </button>
              </div>
            </div>
            {cancelState?.id === event.id && (
              <div className="cancel-confirm">
                <span>{cancelState.question}</span>
                <div className="cancel-buttons">
                  <button className="btn btn-danger" disabled={cancelState.busy} onClick={() => confirmCancel('да')}>Да</button>
                  <button className="btn btn-ghost" disabled={cancelState.busy} onClick={() => confirmCancel('нет')}>Нет</button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {modal && (
        <EventModal
          event={modal.event}
          defaultDate={selected}
          onClose={() => setModal(null)}
          onSaved={() => { setModal(null); onEventsChanged() }}
        />
      )}
    </div>
  )
}
