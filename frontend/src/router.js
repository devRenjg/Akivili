import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', name: 'dashboard', component: () => import('./views/Dashboard.vue') },
  { path: '/projects', name: 'project-space', component: () => import('./views/ProjectSpace.vue') },
  { path: '/projects/:id', name: 'project-detail', component: () => import('./views/ProjectDetail.vue') },
  { path: '/projects/:id/workspace', name: 'workspace', component: () => import('./views/Workspace.vue') },
  { path: '/projects/:id/tasks/:taskId', name: 'task-detail', component: () => import('./views/TaskDetail.vue') },
  { path: '/agents', name: 'agents', component: () => import('./views/Agents.vue') },
  { path: '/skills', name: 'skills', component: () => import('./views/Skills.vue') },
  { path: '/runtime', name: 'runtime', component: () => import('./views/Runtime.vue') },
  { path: '/settings', name: 'settings', component: () => import('./views/Settings.vue') },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 自愈：前端 build 后 chunk 文件名（含 hash）变化，旧缓存 index.html 会去加载已删除的旧 chunk → 404，
// 表现为「点 Tab 没反应」。捕获这类懒加载失败，强制整页刷新一次拿到最新入口。
// sessionStorage 标志位防止刷新死循环（真下线时只刷一次，不反复刷）。
router.onError((err, to) => {
  const msg = String(err && err.message)
  const isChunkLoadError = /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|dynamically imported module/i.test(msg)
  if (isChunkLoadError) {
    if (!sessionStorage.getItem('akivili_chunk_reloaded')) {
      sessionStorage.setItem('akivili_chunk_reloaded', '1')
      window.location.assign(to.fullPath)   // 整页重载，跳过 SPA 内部导航
    }
  }
})
// 成功导航后清除标志，使下一次 build 后仍能再次触发自愈
router.afterEach(() => { sessionStorage.removeItem('akivili_chunk_reloaded') })

export default router
