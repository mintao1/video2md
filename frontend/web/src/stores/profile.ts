import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '../api'
import type { Quota } from '../types'

export const useProfileStore = defineStore('profile', () => {
  const quota = ref<Quota | null>(null)
  const notionKey = 'v2md_notion_sync'
  const storedNotionSync = localStorage.getItem(notionKey)
  const notionSync = ref(storedNotionSync !== '0')

  async function load() {
    quota.value = await api.getQuota()
  }

  function setNotionSync(on: boolean) {
    notionSync.value = on
    localStorage.setItem(notionKey, on ? '1' : '0')
  }

  return { quota, notionSync, load, setNotionSync }
})
