import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'Home', component: () => import('@/views/Home.vue') },
    { path: '/meeting/:id', name: 'Meeting', component: () => import('@/views/Meeting.vue') },
  ],
})

export default router