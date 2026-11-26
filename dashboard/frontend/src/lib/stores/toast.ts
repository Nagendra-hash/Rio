import { writable } from 'svelte/store'

export interface Toast {
  id: string
  type: 'success' | 'error' | 'warning' | 'info'
  title: string
  message?: string
  duration?: number
}

const toasts = writable<Toast[]>([])

function add(toast: Omit<Toast, 'id'>) {
  const id = crypto.randomUUID()
  const t: Toast = { ...toast, id }
  toasts.update(arr => [...arr, t])
  if (toast.duration && toast.duration > 0) {
    setTimeout(() => dismiss(id), toast.duration)
  }
  return id
}

function dismiss(id: string) {
  toasts.update(arr => arr.filter(t => t.id !== id))
}

function clear() {
  toasts.set([])
}

export const toast = {
  subscribe: toasts.subscribe,
  success: (title: string, message?: string, duration = 5000) => add({ type: 'success', title, message, duration }),
  error: (title: string, message?: string, duration = 8000) => add({ type: 'error', title, message, duration }),
  warning: (title: string, message?: string, duration = 6000) => add({ type: 'warning', title, message, duration }),
  info: (title: string, message?: string, duration = 5000) => add({ type: 'info', title, message, duration }),
  dismiss,
  clear,
}