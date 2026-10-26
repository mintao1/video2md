import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '../api'
import type { Quota } from '../types'

export const useProfileStore = defineStore('profile', () => {
  const quota = ref<Quota | null>(null)
  const notionSync = ref(localStorage.getItem('v2pdf_notion_sync') !== '0')

  async function load() {
    quota.value = await api.getQuota()
  }

  function setNotionSync(on: boolean) {
    notionSync.value = on
    localStorage.setItem('v2pdf_notion_sync', on ? '1' : '0')
  }

  return { quota, notionSync, load, setNotionSync }
})
