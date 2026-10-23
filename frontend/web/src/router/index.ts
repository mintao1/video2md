import { createRouter, createWebHistory } from 'vue-router'
import { getToken, hasAnonymousSession } from '../api/client'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'brand', component: () => import('../views/BrandView.vue') },
    {
      path: '/library',
      name: 'library',
      component: () => import('../views/LibraryView.vue'),
    },
    {
      path: '/read/:id',
      name: 'read',
      component: () => import('../views/ReaderView.vue'),
    },
    {
      path: '/tasks',
      name: 'tasks',
      component: () => import('../views/QueueView.vue'),
    },
    {
      path: '/profile/colab',
      name: 'colab-auth',
      component: () => import('../views/ColabAuthView.vue'),
    },
    {
      path: '/profile',
      name: 'profile',
      component: () => import('../views/ProfileView.vue'),
    },
    { path: '/terms', name: 'terms', component: () => import('../views/TermsView.vue') },
    { path: '/privacy', name: 'privacy', component: () => import('../views/PrivacyView.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/library' },
  ],
})

router.beforeEach(async (to) => {
  if (to.name === 'terms' || to.name === 'privacy') return true
  const signedIn = Boolean(getToken()) || (await hasAnonymousSession())
  if (to.name === 'brand') {
    // The brand page owns sign-in and consumes the Google callback fragment.
    if (/(^|[#&])(id_token|auth_error)=/.test(to.hash)) return true
    return signedIn ? { name: 'library' } : true
  }
  if (!signedIn) return { name: 'brand', query: { redirect: to.fullPath } }
  return true
})

export default router
