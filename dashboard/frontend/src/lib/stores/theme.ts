import { writable } from 'svelte/store'

type Theme = 'light' | 'dark'

function createThemeStore() {
  const { subscribe, set, update } = writable<Theme>('dark')

  const saved = localStorage.getItem('theme') as Theme | null
  if (saved) set(saved)
  else if (window.matchMedia('(prefers-color-scheme: light)').matches) set('light')

  return {
    subscribe,
    set: (theme: Theme) => {
      set(theme)
      localStorage.setItem('theme', theme)
      document.documentElement.setAttribute('data-theme', theme)
    },
    toggle: () => update(t => {
      const next = t === 'dark' ? 'light' : 'dark'
      localStorage.setItem('theme', next)
      document.documentElement.setAttribute('data-theme', next)
      return next
    }),
    init: () => {
      const t = saved || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
      document.documentElement.setAttribute('data-theme', t)
      set(t)
    },
  }
}

export const theme = createThemeStore()
theme.init()