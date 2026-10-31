/* Показ системных уведомлений и возврат в приложение по клику.
   Запланированных фоновых задач здесь нет: список наступивших событий
   получает открытая страница через /api/notifications/due. */

self.addEventListener('install', () => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim())
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const targetUrl = event.notification.data?.url || '/'

  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    })
    const appWindow = windows.find((client) => new URL(client.url).origin === self.location.origin)

    if (appWindow) {
      await appWindow.focus()
      appWindow.postMessage({
        type: 'ritm-notification-open',
        eventId: event.notification.data?.eventId,
      })
      return
    }

    await self.clients.openWindow(targetUrl)
  })())
})
