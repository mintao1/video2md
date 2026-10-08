import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '../api'
import type { User } from '../types'

const TOKEN_KEY = 'v2md_token'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const user = ref<User | null>(null)

  const isLoggedIn = computed(() => token.value !== null)

  async function login(result: { token: string; user: User }) {
    token.value = result.token
    localStorage.setItem(TOKEN_KEY, result.token)
    user.value = result.user
  }

  async function fetchUser() {
    if (!isLoggedIn.value || user.value) return
    user.value = await api.currentUser()
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
  }

  return { token, user, isLoggedIn, login, fetchUser, logout }
})
