import axios from 'axios'

const api = axios.create({ baseURL: '/api', withCredentials: true })

export const authApi = {
  me: () => api.get('/auth/me').then((r) => r.data),
  login: (username, password) => api.post('/auth/login', { username, password }).then((r) => r.data),
  logout: () => api.post('/auth/logout').then((r) => r.data),
}

export const settingsApi = {
  get: () => api.get('/settings').then((r) => r.data),
  save: (payload) => api.put('/settings', payload).then((r) => r.data),
  test: (providerId) => api.post(`/settings/${providerId}/test`).then((r) => r.data),
}

export const agentsApi = {
  list: (params) => api.get('/agents/templates', { params }).then((r) => r.data),
  divisions: () => api.get('/agents/divisions').then((r) => r.data),
  detail: (id) => api.get(`/agents/templates/${id}`).then((r) => r.data),
  projects: (id) => api.get(`/agents/templates/${id}/projects`).then((r) => r.data),
  create: (payload) => api.post('/agents/templates', payload).then((r) => r.data),
  setDivision: (id, division) => api.put(`/agents/templates/${id}/division`, { division }).then((r) => r.data),
  renameDivision: (oldName, newName) => api.put('/agents/divisions/rename', { old_name: oldName, new_name: newName }).then((r) => r.data),
  deleteDivision: (name) => api.delete(`/agents/divisions/${encodeURIComponent(name)}`).then((r) => r.data),
  rescan: () => api.post('/agents/rescan').then((r) => r.data),
}

export const projectsApi = {
  list: () => api.get('/projects').then((r) => r.data),
  create: (payload) => api.post('/projects', payload).then((r) => r.data),
  get: (id) => api.get(`/projects/${id}`).then((r) => r.data),
  update: (id, payload) => api.put(`/projects/${id}`, payload).then((r) => r.data),
  remove: (id) => api.delete(`/projects/${id}`).then((r) => r.data),
}

export const projectAgentsApi = {
  list: (pid) => api.get(`/projects/${pid}/agents`).then((r) => r.data),
  import: (pid, templateId) =>
    api.post(`/projects/${pid}/agents/import`, { template_id: templateId }).then((r) => r.data),
  create: (pid, payload) => api.post(`/projects/${pid}/agents`, payload).then((r) => r.data),
  update: (pid, aid, payload) => api.put(`/projects/${pid}/agents/${aid}`, payload).then((r) => r.data),
  remove: (pid, aid) => api.delete(`/projects/${pid}/agents/${aid}`).then((r) => r.data),
  setLeader: (pid, aid) => api.put(`/projects/${pid}/agents/${aid}/leader`).then((r) => r.data),
}

export const memoryApi = {
  read: (slug) => api.get(`/memory/${slug}`).then((r) => r.data),
  write: (slug, content) => api.put(`/memory/${slug}`, { content }).then((r) => r.data),
  append: (slug, text) => api.post(`/memory/${slug}/append`, { text }).then((r) => r.data),
}

export const fsApi = {
  list: (path) => api.get('/fs/list', { params: { path: path || '' } }).then((r) => r.data),
}

export const tasksApi = {
  list: (pid) => api.get(`/projects/${pid}/tasks`).then((r) => r.data),
  get: (pid, tid) => api.get(`/projects/${pid}/tasks/${tid}`).then((r) => r.data),
  create: (pid, payload) => api.post(`/projects/${pid}/tasks`, payload).then((r) => r.data),
  update: (pid, tid, payload) => api.put(`/projects/${pid}/tasks/${tid}`, payload).then((r) => r.data),
  setStatus: (pid, tid, status) => api.put(`/projects/${pid}/tasks/${tid}/status`, { status }).then((r) => r.data),
  remove: (pid, tid) => api.delete(`/projects/${pid}/tasks/${tid}`).then((r) => r.data),
  messages: (tid) => api.get(`/tasks/${tid}/messages`).then((r) => r.data),
  runs: (tid) => api.get(`/tasks/${tid}/runs`).then((r) => r.data),
  activities: (pid, tid) => api.get(`/projects/${pid}/tasks/${tid}/activities`).then((r) => r.data),
  subtasks: (pid, tid) => api.get(`/projects/${pid}/tasks/${tid}/subtasks`).then((r) => r.data),
  createSubtask: (pid, tid, payload) => api.post(`/projects/${pid}/tasks/${tid}/subtasks`, payload).then((r) => r.data),
  progress: (pid, tid) => api.get(`/projects/${pid}/tasks/${tid}/progress`).then((r) => r.data),
  lineage: (tid) => api.get(`/tasks/${tid}/lineage`).then((r) => r.data),
}

export const runsApi = {
  kill: (runId) => api.post('/runs/kill', { run_id: runId }).then((r) => r.data),
  logs: (runId) => api.get(`/runs/${runId}/logs`).then((r) => r.data),
  transcript: (runId) => api.get(`/runs/${runId}/transcript`).then((r) => r.data),
  agentsOverview: () => api.get('/runs/agents-overview').then((r) => r.data),
  autoDispatch: (taskId) => api.post(`/tasks/${taskId}/auto-dispatch`).then((r) => r.data),
  // SSE 流式分派：用 fetch 读流，onEvent(每个事件), 返回 Promise
  dispatch: async (taskId, prompt, assigneeSlug, onEvent) => {
    const resp = await fetch(`/api/tasks/${taskId}/dispatch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ prompt, assignee_slug: assigneeSlug || '' }),
    })
    if (!resp.ok && resp.status >= 400) {
      let detail = `HTTP ${resp.status}`
      try { detail = (await resp.json()).detail || detail } catch { /* ignore */ }
      onEvent({ type: 'error', text: detail })
      return
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop()
      for (const part of parts) {
        const line = part.trim()
        if (line.startsWith('data:')) {
          try { onEvent(JSON.parse(line.slice(5).trim())) } catch { /* ignore */ }
        }
      }
    }
  },
}

export const skillsApi = {
  list: (params) => api.get('/skills', { params }).then((r) => r.data),
  detail: (id) => api.get(`/skills/${id}`).then((r) => r.data),
  rescan: () => api.post('/skills/rescan').then((r) => r.data),
  create: (payload) => api.post('/skills', payload).then((r) => r.data),
  downloadUrl: (id) => `/api/skills/${id}/download`,
  downloadLogs: (id) => api.get(`/skills/${id}/downloads`).then((r) => r.data),
}

export const agentConfigApi = {
  get: (slug) => api.get(`/agent-config/${slug}`).then((r) => r.data),
  setModel: (slug, providerId) =>
    api.put(`/agent-config/${slug}/model`, { provider_id: providerId }).then((r) => r.data),
  setSkills: (slug, skillSlugs) =>
    api.put(`/agent-config/${slug}/skills`, { skill_slugs: skillSlugs }).then((r) => r.data),
  setProfile: (slug, nickname, avatar) =>
    api.put(`/agent-config/${slug}/profile`, { nickname, avatar }).then((r) => r.data),
  taken: (excludeSlug) =>
    api.get('/agent-config/taken/list', { params: { exclude: excludeSlug || '' } }).then((r) => r.data),
}

export const iconsApi = {
  list: () => api.get('/icons').then((r) => r.data),
  url: (name) => `/api/icons/${encodeURIComponent(name)}`,
}

export default api
