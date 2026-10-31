import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Сборка уходит в ../web — оттуда FastAPI раздаёт готовый сайт.
// base '/static/' нужен, потому что FastAPI монтирует web/ на /static.
// В dev-режиме API проксируется на uvicorn (порт 8000).
export default defineConfig(({ command }) => ({
  plugins: [react()],
  base: command === 'build' ? '/static/' : '/',
  build: {
    outDir: '../web',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
}))
