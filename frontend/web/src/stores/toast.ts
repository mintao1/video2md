import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface ToastItem {
  id: number
  message: string
}

let seq = 0

export const useToastStore = defineStore('toast', () => {
  const items = ref<ToastItem[]>([])

  function show(message: string, duration = 2800) {
    const id = ++seq
    items.value.push({ id, message })
    setTimeout(() => dismiss(id), duration)
  }
  function dismiss(id: number) {
    items.value = items.value.filter((t) => t.id !== id)
  }

  return { items, show, dismiss }
})
