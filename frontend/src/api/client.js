const BASE = '/api'

async function request(path, options = {}) {
  const response = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!response.ok) {
    let detail = `Ошибка ${response.status}`
    try {
      const data = await response.json()
      if (data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch { /* оставляем текст по умолчанию */ }
    throw new Error(detail)
  }
  return response.json()
}

export const api = {
  health: () => request('/health'),
  listEvents: (from, to) =>
    request(`/events?from_date=${from}&to_date=${to}&limit=200`),
  createEvent: (payload) =>
    request('/events', { method: 'POST', body: JSON.stringify(payload) }),
  updateEvent: (id, patch) =>
    request(`/events/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  cancelRequest: (id, sessionId) =>
    request(`/events/${id}/cancel-request`, {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId }),
    }),
  chat: (message, sessionId) =>
    request('/chat', {
      method: 'POST',
      body: JSON.stringify({ message, session_id: sessionId }),
    }),
  history: (sessionId) => request(`/sessions/${sessionId}/history?limit=50`),
  getDueNotifications: () => request('/notifications/due'),
}
