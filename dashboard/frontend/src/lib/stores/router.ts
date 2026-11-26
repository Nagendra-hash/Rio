import { writable } from 'svelte/store'

export const currentPath = writable<string>('/')

export function navigate(path: string) {
  currentPath.set(path)
  window.history.pushState({}, '', path)
}

export function initRouter() {
  currentPath.set(window.location.pathname || '/')
  window.addEventListener('popstate', () => {
    currentPath.set(window.location.pathname || '/')
  })
}