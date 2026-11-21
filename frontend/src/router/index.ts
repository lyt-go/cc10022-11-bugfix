import { createRouter, createWebHistory } from 'vue-router'

import AddressDispatchView from '@/views/AddressDispatchView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/address-dispatch', name: 'address-dispatch', component: AddressDispatchView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
