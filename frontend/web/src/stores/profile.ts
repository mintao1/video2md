import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '../api'
import type { Quota } from '../types'

export const useProfileStore = defineStore('profile', () => {
  const quota = ref<Quota | null>(null)

  async function load() {
    quota.value = await api.getQuota()
  }

  return { quota, load }
})
