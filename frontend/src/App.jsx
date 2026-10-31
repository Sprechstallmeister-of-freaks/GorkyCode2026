import { useCallback, useEffect, useRef, useState } from 'react'
import CalendarPanel from './components/CalendarPanel.jsx'
import ChatPanel from './components/ChatPanel.jsx'
import { IconBell, IconBellOff, IconCalendar, IconChat } from './components/icons.jsx'
import { api } from './api/client.js'

const POLL_INTERVAL_MS = 60_000 // проверять наступление событий каждую минуту

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

  // Состояние браузерных уведомлений
  const [notificationPermission, setNotificationPermission] = useState(
    () => (typeof Notification !== 'undefined' ? Notification.permission : 'unsupported'),
  )
  const [supported, setSupported] = useState(
    () => typeof Notification !== 'undefined' && typeof document !== 'undefined',
  )
  const [dueCount, setDueCount] = useState(0)
  const [pollStatus, setPollStatus] = useState('idle') // idle | checking | error
  const [lastPollError, setLastPollError] = useState<string | null>(null)
  const pollTimer = useRef(null)

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null))
  }, [eventsVersion])

  // Браузерные уведомления: опрос сервера о наступлении времени событий.
  const pollNotifications = useCallback(async () => {
    if (!supported || notificationPermission !== 'granted') return
    if (document.visibilityState !== 'visible') return
    setPollStatus('checking')
    setLastPollError(null)
    try {
      const data = await api.getDueNotifications()
      if (data.count > 0) {
        setDueCount((c) => c + data.count)
        data.items.forEach((event) => {
          const when = event.scheduled_date
            ? `${event.scheduled_date}${event.scheduled_time ? ` в ${event.scheduled_time.slice(0, 5)}` : ''}`
            : ''
          new Notification(`Ритм: ${event.title}`, {
            body: when || 'Наступило время события',
            tag: `ritm-event-${event.id}`,
          })
        })
      }
      setPollStatus('idle')
    } catch (e) {
      setPollStatus('error')
      setLastPollError(e instanceof Error ? e.message : String(e))
      // Тихая ошибка опроса — не прерываем работу приложения.
    }
  }, [supported, notificationPermission])

  const startPolling = useCallback(() => {
    if (pollTimer.current) return
    pollNotifications()
    pollTimer.current = setInterval(pollNotifications, POLL_INTERVAL_MS)
  }, [pollNotifications])

  const stopPolling = useCallback(() => {
    if (pollTimer.current) {
      clearInterval(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  // Опрашиваем только когда вкладка видима и разрешение дано.
  useEffect(() => {
    if (notificationPermission !== 'granted') return
    startPolling()
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') startPolling()
      else stopPolling()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      stopPolling()
    }
  }, [notificationPermission, startPolling, stopPolling])

  const requestPermission = async () => {
    if (!supported) return
    try {
      const permission = await Notification.requestPermission()
      setNotificationPermission(permission)
      if (permission === 'granted') {
        startPolling()
      }
    } catch (e) {
      // Браузер может заблокировать запрос вне действия пользователя.
    }
  }

  const refreshEvents = () => setEventsVersion((v) => v + 1)

  const canNotify = supported && notificationPermission === 'granted'

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
        <div className="header-actions">
          <button
            className={`icon-btn notification-toggle ${canNotify ? 'active' : ''}`}
            onClick={requestPermission}
            title={canNotify
              ? 'Уведомления включены'
              : notificationPermission === 'denied'
                ? 'Уведомления заблокированы браузером'
                : 'Включить уведомления о событиях'}
            disabled={notificationPermission === 'denied'}
          >
            {supported ? (
              <>{canNotify ? <IconBell /> : <IconBellOff />}</>
            ) : (
              <span>—</span>
            )}
          </button>
          <button
            className={`icon-btn notification-check ${pollStatus === 'checking' ? 'checking' : ''}`}
            onClick={pollNotifications}
            title={
              pollStatus === 'checking'
                ? 'Проверяем уведомления…'
                : pollStatus === 'error'
                  ? 'Ошибка проверки: ' + lastPollError
                  : 'Проверить уведомления сейчас'
            }
            disabled={!canNotify || pollStatus === 'checking'}
          >
            {pollStatus === 'checking' ? '⟳' : '?'}
          </button>
          {dueCount > 0 && canNotify && (
            <span className="badge badge-notice">{dueCount}</span>
          )}
          {!supported && (
            <span className="badge badge-notice" title="Уведомления не поддерживаются браузером">—</span>
          )}
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
