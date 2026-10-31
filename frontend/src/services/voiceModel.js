/* eslint-disable */
// ============================================================================
//                    ⚠️  ЗАГЛУШКА ГОЛОСОВОЙ МОДЕЛИ  ⚠️
// ============================================================================
//  ФАЙЛ: frontend/src/services/voiceModel.js
//
//  ЗДЕСЬ ПОДКЛЮЧАЕТСЯ НАСТОЯЩАЯ ГОЛОСОВАЯ МОДЕЛЬ, которая будет вести
//  голосовой диалог с пользователем о его планировании: планы на день,
//  на неделю, жизненные цели, приоритеты и т.д.
//
//  СЕЙЧАС (временная реализация-заглушка):
//    • Распознавание речи (STT) — браузерный Web Speech API
//      (window.SpeechRecognition / webkitSpeechRecognition, Chrome/Edge).
//    • Озвучка ответов (TTS) — браузерный window.speechSynthesis.
//    Сам текстовый ответ о планировании формирует существующий бэкенд
//    (POST /api/chat), этот модуль отвечает ТОЛЬКО за голос.
//
//  КАК ЗАМЕНИТЬ НА РЕАЛЬНУЮ МОДЕЛЬ (например, Silero TTS / Whisper /
//  Vosk / облачный голосовой ассистент):
//    1. Замените тело startListening() — вместо SpeechRecognition
//       вызывайте свою STT-модель. Сохраните контракт колбэков:
//       onInterim(промежуточныйТекст), onFinal(итоговыйТекст),
//       onError(текстОшибки), onEnd().
//    2. Замените тело speak() — вместо speechSynthesis проигрывайте
//       аудио, сгенерированное вашей TTS-моделью (Audio / AudioContext).
//    3. Остальной фронтенд (ChatPanel) трогать НЕ нужно: он работает
//       только с этими функциями.
//
//  TODO(voice-model): интегрировать реальную STT/TTS-модель.
// ============================================================================

const SpeechRecognitionImpl =
  typeof window !== 'undefined'
    ? window.SpeechRecognition || window.webkitSpeechRecognition
    : null

export const isRecognitionSupported = Boolean(SpeechRecognitionImpl)
export const isSynthesisSupported =
  typeof window !== 'undefined' && 'speechSynthesis' in window

let activeRecognition = null

/**
 * Начать прослушивание микрофона.
 * ЗАГЛУШКА: сейчас — браузерный Web Speech API, заменить на свою STT-модель.
 *
 * @param {Object}   callbacks
 * @param {(text: string) => void} callbacks.onInterim  промежуточный текст
 * @param {(text: string) => void} callbacks.onFinal    итоговый распознанный текст
 * @param {(message: string) => void} [callbacks.onError]
 * @param {() => void} [callbacks.onEnd]                завершение прослушивания
 * @returns {() => void} функция остановки
 */
export function startListening({ onInterim, onFinal, onError, onEnd }) {
  if (!SpeechRecognitionImpl) {
    onError?.('Распознавание речи не поддерживается этим браузером. Попробуйте Chrome или Edge.')
    onEnd?.()
    return () => {}
  }

  stopListening()

  const recognition = new SpeechRecognitionImpl()
  activeRecognition = recognition
  recognition.lang = 'ru-RU'
  recognition.interimResults = true
  recognition.continuous = false
  recognition.maxAlternatives = 1

  recognition.onresult = (event) => {
    let interim = ''
    let final = ''
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const transcript = event.results[i][0].transcript
      if (event.results[i].isFinal) final += transcript
      else interim += transcript
    }
    if (interim) onInterim?.(interim)
    if (final) onFinal?.(final.trim())
  }

  recognition.onerror = (event) => {
    const messages = {
      'not-allowed': 'Доступ к микрофону запрещён. Разрешите его в настройках браузера.',
      'no-speech': 'Ничего не расслышал — попробуйте ещё раз.',
      'audio-capture': 'Микрофон не найден.',
      network: 'Ошибка сети при распознавании речи.',
    }
    onError?.(messages[event.error] || `Ошибка распознавания: ${event.error}`)
  }

  recognition.onend = () => {
    if (activeRecognition === recognition) activeRecognition = null
    onEnd?.()
  }

  try {
    recognition.start()
  } catch {
    onError?.('Не удалось запустить микрофон.')
    onEnd?.()
  }

  return stopListening
}

/** Остановить текущее прослушивание. */
export function stopListening() {
  if (activeRecognition) {
    const recognition = activeRecognition
    activeRecognition = null
    try { recognition.stop() } catch { /* уже остановлен */ }
  }
}

/**
 * Озвучить текст ответа ассистента.
 * ЗАГЛУШКА: сейчас — браузерный speechSynthesis, заменить на свою TTS-модель
 * (воспроизведение сгенерированного аудио через Audio/AudioContext).
 *
 * @param {string} text
 * @param {Object} [options]
 * @param {() => void} [options.onEnd]
 */
export function speak(text, { onEnd } = {}) {
  if (!isSynthesisSupported || !text) {
    onEnd?.()
    return
  }
  stopSpeaking()

  const utterance = new SpeechSynthesisUtterance(text)
  utterance.lang = 'ru-RU'
  utterance.rate = 1.02
  utterance.pitch = 1

  // Стараемся выбрать русский голос, если он есть в системе.
  const voices = window.speechSynthesis.getVoices()
  const russian = voices.find((v) => v.lang?.toLowerCase().startsWith('ru'))
  if (russian) utterance.voice = russian

  utterance.onend = () => onEnd?.()
  utterance.onerror = () => onEnd?.()
  window.speechSynthesis.speak(utterance)
}

/** Остановить озвучку. */
export function stopSpeaking() {
  if (isSynthesisSupported) window.speechSynthesis.cancel()
}
