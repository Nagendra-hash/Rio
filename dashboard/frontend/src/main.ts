import { mount } from 'svelte'
import './app.css'
import App from './App.svelte'
import { ws } from '$lib/api/ws'
import { config } from '$lib/stores/config'

// Initialize WebSocket connection
ws.connect()

// Load initial config
config.load()

const app = mount(App, {
  target: document.getElementById('app')!,
})

export default app