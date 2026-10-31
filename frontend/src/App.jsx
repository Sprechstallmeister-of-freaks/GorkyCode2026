import { useCallback, useEffect, useRef, useState } from 'react'
import CalendarPanel from './components/CalendarPanel.jsx'
import ChatPanel from './components/ChatPanel.jsx'
import { IconBell, IconBellOff, IconCalendar, IconChat, IconRefresh } from './components/icons.jsx'
import { api } from './api/client.js'
import {
  getNotificationPermission,
  showEventNotification,
  showTestNotification,
} from './services/notifications.js'

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

function notificationErrorText(error) {
  const detail = error instanceof Error ? error.message : String(error)
  return `Не удалось проверить или показать уведомление. ${detail}`
}

export default function App() {
  const [sessionId] = useState(getSessionId)
  const [eventsVersion, setEventsVersion] = useState(0)
  const [tab, setTab] = useState('calendar')
  const [health, setHealth] = useState(null)

  const [notificationPermission, setNotificationPermission] = useState(getNotificationPermission)
  const [pollStatus, setPollStatus] = useState('idle') // idle | checking | error
  const [notificationError, setNotificationError] = useState(null)
  const pollInFlight = useRef(false)

  const notificationsSupported = notificationPermission !== 'unsupported'
  const canNotify = notificationPermission === 'granted'

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null))
  }, [eventsVersion])

  // Опрос продолжается и в скрытой вкладке. Браузер может замедлять таймеры,
  // поэтому при возвращении на страницу дополнительно проверяем события сразу.
  const pollNotifications = useCallback(async () => {
    const actualPermission = getNotificationPermission()
    if (actualPermission !== 'granted') {
      setNotificationPermission(actualPermission)
      return
    }
    if (pollInFlight.current) return

    pollInFlight.current = true
    setPollStatus('checking')
    setNotificationError(null)
    try {
      const data = await api.getDueNotifications()
      let displayFailed = false
      for (const event of data.items) {
        try {
          await showEventNotification(event)
        } catch (error) {
          console.error('Не удалось показать уведомление о событии', event.id, error)
          displayFailed = true
        }
      }
      if (displayFailed) {
        throw new Error('Проверьте разрешение уведомлений для этого сайта.')
      }
      setPollStatus('idle')
    } catch (error) {
      setPollStatus('error')
      setNotificationError(notificationErrorText(error))
      // Ошибка опроса не прерывает работу остального приложения.
    } finally {
      pollInFlight.current = false
    }
  }, [])

  useEffect(() => {
    if (notificationPermission !== 'granted') return undefined

    const check = () => { void pollNotifications() }
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') check()
    }

    check()
    const timer = window.setInterval(check, POLL_INTERVAL_MS)
    document.addEventListener('visibilitychange', onVisibilityChange)
    window.addEventListener('focus', check)
    window.addEventListener('pageshow', check)
    window.addEventListener('online', check)

    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('focus', check)
      window.removeEventListener('pageshow', check)
      window.removeEventListener('online', check)
    }
  }, [notificationPermission, pollNotifications])

  const enableOrTestNotifications = async () => {
    setNotificationError(null)
    const currentPermission = getNotificationPermission()
    if (currentPermission === 'unsupported') return
    if (currentPermission === 'denied') {
      setNotificationError('Уведомления заблокированы. Разрешите их в настройках этого сайта в браузере.')
      return
    }

    try {
      const permission = currentPermission === 'default'
        ? await Notification.requestPermission()
        : currentPermission
      setNotificationPermission(permission)
      if (permission === 'denied') {
        setNotificationError('Уведомления заблокированы. Разрешите их в настройках этого сайта в браузере.')
        return
      }
      if (permission !== 'granted') return

      // Тест сразу проверяет и разрешение, и регистрацию Service Worker.
      await showTestNotification()
      await pollNotifications()
    } catch (error) {
      setPollStatus('error')
      setNotificationError(notificationErrorText(error))
    }
  }

  const refreshEvents = () => setEventsVersion((v) => v + 1)

  const notificationBlockReason = !notificationsSupported
    ? 'Браузер не поддерживает уведомления либо сайт открыт без защищённого HTTPS-соединения.'
    : notificationPermission === 'denied'
      ? 'Уведомления заблокированы. Разрешите их в настройках этого сайта в браузере.'
      : null

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
            type="button"
            className={`icon-btn notification-toggle ${canNotify ? 'active' : ''}`}
            onClick={enableOrTestNotifications}
            aria-label={canNotify ? 'Проверить браузерное уведомление' : 'Включить уведомления о событиях'}
            title={canNotify
              ? 'Уведомления включены. Нажмите для теста'
              : notificationPermission === 'denied'
                ? 'Уведомления заблокированы браузером'
                : 'Включить уведомления о событиях'}
            disabled={!notificationsSupported}
          >
            {notificationsSupported ? (
              <>{canNotify ? <IconBell /> : <IconBellOff />}</>
            ) : (
              <span>—</span>
            )}
          </button>
          <button
            type="button"
            className={`icon-btn notification-check ${pollStatus === 'checking' ? 'checking' : ''}`}
            onClick={() => { void pollNotifications() }}
            aria-label="Проверить наступившие события"
            title={
              pollStatus === 'checking'
                ? 'Проверяем уведомления…'
                : pollStatus === 'error'
                  ? `Ошибка проверки: ${notificationError || 'неизвестная ошибка'}`
                  : 'Проверить уведомления сейчас'
            }
            disabled={!canNotify || pollStatus === 'checking'}
          >
            <IconRefresh />
          </button>
        </div>
      </header>

      {(notificationError || notificationBlockReason) && (
        <div className="notification-feedback" role="status">
          {notificationError || notificationBlockReason}
        </div>
      )}

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
