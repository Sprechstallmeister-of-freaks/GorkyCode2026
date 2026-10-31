import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client.js'
import {
  isRecognitionSupported,
  isSynthesisSupported,
  speak,
  startListening,
  stopListening,
  stopSpeaking,
} from '../services/voiceModel.js' // ← ЗАГЛУШКА голосовой модели (см. комментарии в файле)
import { IconMic, IconSend, IconSpeaker } from './icons.jsx'

const SUGGESTIONS = [
  'Что у меня сегодня?',
  'Какие планы на эту неделю?',
  'Что у меня завтра?',
  'Напомни завтра в 10:00 позвонить Ивану',
]

const WELCOME = {
  id: 'welcome',
  role: 'assistant',
  text: 'Привет! Я Ритм — ваш планировщик. Спросите про планы на день или неделю, попросите что-нибудь запланировать. Можно голосом — нажмите на микрофон.',
}

export default function ChatPanel({ sessionId, onEventsChanged }) {
  const [messages, setMessages] = useState([WELCOME])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [listening, setListening] = useState(false)
  const [autoVoice, setAutoVoice] = useState(false)
  const [error, setError] = useState('')
  const scrollRef = useRef(null)
  const inputRef = useRef(null)
  const stopRef = useRef(null)

  // Загрузка сохранённой истории диалога
  useEffect(() => {
    let cancelled = false
    api.history(sessionId)
      .then((data) => {
        if (cancelled || !data.messages?.length) return
        const history = data.messages
          .slice()
          .reverse()
          .map((m, i) => ({ id: `h-${i}`, role: m.role, text: m.content }))
        setMessages([WELCOME, ...history])
      })
      .catch(() => { /* история необязательна */ })
    return () => { cancelled = true }
  }, [sessionId])

  // Автопрокрутка к последнему сообщению
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [messages, busy])

  useEffect(() => () => { stopListening(); stopSpeaking() }, [])

  const send = async (rawText) => {
    const text = (rawText ?? input).trim()
    if (!text || busy) return
    setError('')
    setInput('')
    setMessages((list) => [...list, { id: `u-${Date.now()}`, role: 'user', text }])
    setBusy(true)
    try {
      const result = await api.chat(text, sessionId)
      setMessages((list) => [
        ...list,
        { id: `a-${Date.now()}`, role: 'assistant', text: result.answer },
      ])
      if (autoVoice) speak(result.answer)
      // В чате могли создаться/измениться события — обновляем календарь
      onEventsChanged()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
      inputRef.current?.focus()
    }
  }

  const toggleListening = () => {
    if (listening) {
      stopRef.current?.()
      setListening(false)
      return
    }
    setError('')
    setListening(true)
    stopRef.current = startListening({
      onInterim: (text) => setInput(text),
      onFinal: (text) => {
        setInput('')
        if (text) send(text)
      },
      onError: (message) => setError(message),
      onEnd: () => setListening(false),
    })
  }

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="chat-panel">
      <div className="panel-header">
        <h2>Ассистент</h2>
        <button
          className={`voice-toggle ${autoVoice ? 'active' : ''}`}
          onClick={() => {
            setAutoVoice((v) => !v)
            if (autoVoice) stopSpeaking()
          }}
          title={autoVoice ? 'Выключить озвучку ответов' : 'Включить озвучку ответов'}
          disabled={!isSynthesisSupported}
        >
          <IconSpeaker off={!autoVoice} />
          <span>{autoVoice ? 'Озвучка вкл.' : 'Озвучка выкл.'}</span>
        </button>
      </div>

      <div className="messages" ref={scrollRef}>
        {messages.map((m) => (
          <div key={m.id} className={`message-row ${m.role}`}>
            <div className="message-bubble">
              {m.text}
              {m.role === 'assistant' && isSynthesisSupported && (
                <button
                  className="speak-btn"
                  onClick={() => speak(m.text)}
                  title="Озвучить сообщение"
                >
                  <IconSpeaker size={14} />
                </button>
              )}
            </div>
          </div>
        ))}
        {busy && (
          <div className="message-row assistant">
            <div className="message-bubble typing">
              <span /><span /><span />
            </div>
          </div>
        )}
      </div>

      <div className="suggestions">
        {SUGGESTIONS.map((s) => (
          <button key={s} className="chip suggestion" disabled={busy || listening} onClick={() => send(s)}>
            {s}
          </button>
        ))}
      </div>

      {error && <div className="inline-error">{error}</div>}

      {listening && (
        <div className="listening-banner">
          <span className="pulse-dot" /> Слушаю… говорите
          <button className="btn btn-ghost" onClick={toggleListening}>Стоп</button>
        </div>
      )}

      <div className="chat-input-row">
        <button
          className={`mic-btn ${listening ? 'listening' : ''}`}
          onClick={toggleListening}
          disabled={!isRecognitionSupported || busy}
          title={isRecognitionSupported ? 'Голосовой ввод' : 'Голосовой ввод не поддерживается браузером'}
        >
          <IconMic />
        </button>
        <input
          ref={inputRef}
          className="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={listening ? 'Слушаю…' : 'Спросите про планы или продиктуйте…'}
          disabled={busy}
          maxLength={2000}
        />
        <button className="send-btn" onClick={() => send()} disabled={busy || !input.trim()} title="Отправить">
          <IconSend />
        </button>
      </div>
    </div>
  )
}
