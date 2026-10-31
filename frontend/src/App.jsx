import { useEffect, useState } from 'react'
import CalendarPanel from './components/CalendarPanel.jsx'
import ChatPanel from './components/ChatPanel.jsx'
import { IconCalendar, IconChat } from './components/icons.jsx'
import { api } from './api/client.js'

function getSessionId() {
  const key = 'ritm_session_id'
  let id = localStorage.getItem(key)
  if (!id) {
    id = `web-${crypto.randomUUID()}`
    localStorage.setItem(key, id)
  }
  return id
}

export default function App() {
  const [sessionId] = useState(getSessionId)
  const [eventsVersion, setEventsVersion] = useState(0)
  const [tab, setTab] = useState('calendar')
  const [health, setHealth] = useState(null)

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null))
  }, [eventsVersion])

  const refreshEvents = () => setEventsVersion((v) => v + 1)

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <div className="brand-logo">Р</div>
          <div>
            <h1 className="brand-name">Ритм</h1>
            <p className="brand-subtitle">голосовой планировщик</p>
          </div>
        </div>
        <div className="header-status" title={health ? `Записей в базе: ${health.records}` : 'Сервер недоступен'}>
          <span className={`status-dot ${health ? 'online' : 'offline'}`} />
          <span className="status-text">
            {health ? (health.llm_connected ? 'AI-ассистент онлайн' : 'Ассистент готов') : 'Нет связи с сервером'}
          </span>
        </div>
      </header>

      <nav className="mobile-tabs" aria-label="Разделы">
        <button className={tab === 'calendar' ? 'active' : ''} onClick={() => setTab('calendar')}>
          <IconCalendar /> Календарь
        </button>
        <button className={tab === 'chat' ? 'active' : ''} onClick={() => setTab('chat')}>
          <IconChat /> Чат
        </button>
      </nav>

      <main className="layout">
        <section className={`panel ${tab !== 'calendar' ? 'mobile-hidden' : ''}`}>
          <CalendarPanel sessionId={sessionId} eventsVersion={eventsVersion} onEventsChanged={refreshEvents} />
        </section>
        <section className={`panel ${tab !== 'chat' ? 'mobile-hidden' : ''}`}>
          <ChatPanel sessionId={sessionId} onEventsChanged={refreshEvents} />
        </section>
      </main>
    </div>
  )
}
