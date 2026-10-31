import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { IconClose } from './icons.jsx'

const KINDS = [
  ['task', 'Задача'],
  ['meeting', 'Встреча'],
  ['reminder', 'Напоминание'],
]

export default function EventModal({ event, defaultDate, onClose, onSaved }) {
  const isEdit = Boolean(event)
  const [kind, setKind] = useState(event?.kind || 'task')
  const [title, setTitle] = useState(event?.title || '')
  const [date, setDate] = useState(event?.scheduled_date || defaultDate || '')
  const [time, setTime] = useState(event?.scheduled_time?.slice(0, 5) || '')
  const [duration, setDuration] = useState(event?.duration_minutes || 60)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const needsDateTime = kind === 'meeting' || kind === 'reminder'

  const save = async (e) => {
    e.preventDefault()
    if (!title.trim()) { setError('Введите название'); return }
    if (needsDateTime && (!date || !time)) {
      setError('Для встречи и напоминания нужны дата и время')
      return
    }
    setBusy(true)
    setError('')
    try {
      if (isEdit) {
        const patch = { expected_version: event.version }
        if (title.trim() !== event.title) patch.title = title.trim()
        if ((date || null) !== event.scheduled_date) patch.scheduled_date = date || null
        const newTime = time ? `${time}:00` : null
        if (newTime !== event.scheduled_time) patch.scheduled_time = newTime
        if (kind === 'meeting' && duration !== event.duration_minutes) patch.duration_minutes = Number(duration)
        await api.updateEvent(event.id, patch)
      } else {
        await api.createEvent({
          kind,
          title: title.trim(),
          scheduled_date: date || null,
          scheduled_time: time ? `${time}:00` : null,
          duration_minutes: kind === 'meeting' ? Number(duration) : null,
        })
      }
      onSaved()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>{isEdit ? 'Редактировать событие' : 'Новое событие'}</h3>
          <button className="icon-btn subtle" onClick={onClose} aria-label="Закрыть"><IconClose /></button>
        </div>
        <form onSubmit={save}>
          <div className="segmented">
            {KINDS.map(([value, label]) => (
              <button
                type="button"
                key={value}
                className={kind === value ? 'active' : ''}
                onClick={() => setKind(value)}
                disabled={isEdit}
              >
                {label}
              </button>
            ))}
          </div>

          <label className="field">
            <span>Название</span>
            <input
              autoFocus
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Например: Планёрка с командой"
              maxLength={500}
            />
          </label>

          <div className="field-row">
            <label className="field">
              <span>Дата{!needsDateTime && ' (необязательно)'}</span>
              <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
            </label>
            <label className="field">
              <span>Время{!needsDateTime && ' (необязательно)'}</span>
              <input type="time" value={time} onChange={(e) => setTime(e.target.value)} />
            </label>
          </div>

          {kind === 'meeting' && (
            <label className="field">
              <span>Длительность, минут</span>
              <input
                type="number"
                min="1"
                max="1440"
                value={duration}
                onChange={(e) => setDuration(e.target.value)}
              />
            </label>
          )}

          {error && <div className="inline-error">{error}</div>}

          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>Отмена</button>
            <button type="submit" className="btn btn-accent" disabled={busy}>
              {busy ? 'Сохранение…' : isEdit ? 'Сохранить' : 'Создать'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
