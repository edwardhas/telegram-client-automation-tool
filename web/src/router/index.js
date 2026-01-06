import { createRouter, createWebHistory } from 'vue-router'

import DashboardPage from '../pages/DashboardPage.vue'
import CreatePage from '../pages/CreatePage.vue'
import ChatsPage from '../pages/ChatsPage.vue'
import CampaignsPage from '../pages/CampaignsPage.vue'
import SettingsPage from '../pages/SettingsPage.vue'

const routes = [
  { path: '/', component: DashboardPage },
  { path: '/create', component: CreatePage },
  { path: '/chats', component: ChatsPage },
  { path: '/campaigns', component: CampaignsPage },
  { path: '/settings', component: SettingsPage }
]

export default createRouter({
  history: createWebHistory(),
  routes
})
