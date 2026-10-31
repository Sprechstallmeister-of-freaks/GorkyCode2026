let workerRegistrationPromise = null

export function getNotificationPermission() {
  if (
    typeof window === 'undefined'
    || !('Notification' in window)
    || !window.isSecureContext
  ) {
    return 'unsupported'
  }
  return Notification.permission
}

export function formatEventMoment(event) {
  if (!event.scheduled_date) return 'Наступило время события'
  const [year, month, day] = event.scheduled_date.split('-')
  const date = `${day}.${month}.${year}`
  return event.scheduled_time ? `${date} в ${event.scheduled_time.slice(0, 5)}` : date
}

function notificationOptions(event) {
  return {
    body: formatEventMoment(event),
    tag: `ritm-event-${event.id}`,
    data: { eventId: event.id, url: '/' },
  }
}

async function getWorkerRegistration() {
  if (!workerRegistrationPromise) {
    workerRegistrationPromise = navigator.serviceWorker
      .register('/sw.js', { scope: '/' })
      .catch((error) => {
        workerRegistrationPromise = null
        throw error
      })
  }
  return workerRegistrationPromise
}

function showDirectNotification(title, options) {
  const notification = new Notification(title, options)
  notification.onclick = () => {
    notification.close()
    window.focus()
  }
}

async function show(title, options) {
  if ('serviceWorker' in navigator) {
    try {
      const registration = await getWorkerRegistration()
      await registration.showNotification(title, options)
      return
    } catch (error) {
      // На desktop можно использовать Notification API без Service Worker.
      // На Android конструктор Notification обычно недоступен — тогда сохраняем
      // исходную ошибку регистрации/показа, чтобы интерфейс сообщил о проблеме.
      try {
        showDirectNotification(title, options)
        return
      } catch {
        throw error
      }
    }
  }
  showDirectNotification(title, options)
}

export function showEventNotification(event) {
  return show(`Ритм: ${event.title}`, notificationOptions(event))
}

export function showTestNotification() {
  return show('Ритм: уведомления включены', {
    body: 'Напоминания будут приходить, пока открыта вкладка приложения.',
    tag: 'ritm-notification-test',
    data: { url: '/' },
  })
}
