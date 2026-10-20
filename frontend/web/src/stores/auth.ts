import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '../api'
import type { User } from '../types'
import { clearToken, getToken, setToken } from '../api/client'

/**
 * Google OAuth returns an ID token in the login URL fragment. The token is
 * kept locally and sent as a Bearer credential by the API client.
 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)

  function acceptToken(token: string): void {
    setToken(token)
  }

  /** A full page load (not router.replace) drops every Pinia store, so the
   *  previous user's books/runs/quota cannot stay on screen after sign-out. */
  function logout(): void {
    clearToken()
    user.value = null
    window.location.replace('/login')
  }

  async function fetchUser() {
    if (user.value || !getToken()) return
    user.value = await api.currentUser()
  }

  return { user, acceptToken, logout, fetchUser, isAuthenticated: () => Boolean(getToken()) }
})
