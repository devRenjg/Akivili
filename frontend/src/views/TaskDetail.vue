<template>
  <div class="task-detail" v-loading="loading">
    <!-- 面包屑 + 操作 -->
    <div class="td-topbar">
      <div class="td-crumb">
        <el-button text :icon="ArrowLeft" @click="goBack">工作区</el-button>
        <span class="crumb-sep">/</span>
        <template v-if="task && task.parent_task_id">
          <span class="crumb-link" @click="openTask(task.parent_task_id)">{{ task.parent_title || '父任务' }}</span>
          <span class="crumb-sep">/</span>
        </template>
        <span class="crumb-title">{{ task?.title || '任务' }}</span>
      </div>
    </div>

    <div v-if="task" class="td-body">
      <!-- 主内容区 -->
      <div class="td-main">
        <h1 class="td-title">{{ task.title }}</h1>
        <div v-if="task.description" class="td-desc">{{ task.description }}</div>

        <!-- 子任务 -->
        <div class="td-section">
          <div class="td-sec-head">
            <span class="td-sec-title">子任务</span>
            <span v-if="subtasks.length" class="td-sub-progress">{{ subDone }}/{{ subtasks.length }}</span>
            <el-button v-if="isAdmin" text size="small" @click="addSubVisible = !addSubVisible">+ 新增</el-button>
          </div>
          <div v-if="addSubVisible" class="td-sub-add">
            <el-input v-model="newSubTitle" size="small" placeholder="子任务标题，回车创建" @keyup.enter="doAddSub" />
          </div>
          <div v-for="s in subtasks" :key="s.id" class="td-sub-row" @click="openTask(s.id)">
            <span class="sub-dot" :class="`st-${s.status}`"
                  :title="isAdmin ? '点击控制（暂停/重跑）' : ''"
                  @click.stop="isAdmin && onDotClick(s)">●</span>
            <AgentAvatar :agent="subAgent(s)" :size="20" class="sub-av" />
            <span class="sub-title">{{ s.title }}</span>
            <span class="sub-status">{{ statusLabel(s.status) }}</span>
            <span class="sub-arrow">›</span>
          </div>
          <div v-if="subtasks.length === 0" class="td-empty">暂无子任务</div>
        </div>

        <div class="td-tl-divider">
          <span>动态</span>
          <span class="tl-count">{{ timeline.length }} 条</span>
          <el-button text size="small" class="tl-fold-all" @click="toggleFoldAll">
            {{ allFolded ? '全部展开' : '全部收起' }}
          </el-button>
        </div>
        <div class="td-timeline" ref="tlEl" @scroll="onTlScroll">
          <template v-for="(it, i) in timeline" :key="i">
            <!-- 活动：细行 + 小头像 -->
            <div v-if="it.kind === 'activity'" class="tl-activity">
              <AgentAvatar v-if="it.author" :agent="it.author" :size="18" class="tl-av" />
              <span v-else class="tl-actor-ic">{{ it.actor_type === 'user' ? '👤' : '⚙️' }}</span>
              <span class="tl-text">{{ activityText(it) }}</span>
              <span class="tl-time">{{ shortTime(it.created_at) }}</span>
            </div>
            <!-- 消息：聊天气泡（头像 + 昵称 + 可折叠内容） -->
            <div v-else class="chat-row" :class="{ mine: it.role === 'user' }">
              <AgentAvatar :agent="msgAgent(it)" :size="34" class="chat-av" />
              <div class="chat-main">
                <div class="chat-name" @click="toggleFold(i)">
                  <span class="fold-caret">{{ isFolded(i) ? '▸' : '▾' }}</span>
                  {{ msgName(it) }}<span class="chat-time">{{ shortTime(it.created_at) }}</span>
                </div>
                <div v-show="!isFolded(i)" class="chat-bubble">{{ it.content }}</div>
                <div v-show="isFolded(i)" class="chat-bubble folded" @click="toggleFold(i)">{{ foldPreview(it.content) }}</div>
              </div>
            </div>
          </template>
          <!-- 流式执行中气泡 -->
          <div v-if="streaming" class="chat-row">
            <AgentAvatar :agent="currentAgent" :size="34" class="chat-av" />
            <div class="chat-main">
              <div class="chat-name">{{ currentAgentName }} <span class="running">⚙️ 执行中…</span></div>
              <div class="chat-bubble">{{ streamText || '思考中…' }}</div>
              <div v-if="toolEvents.length" class="tools">
                <div v-for="(t, ti) in toolEvents" :key="ti" class="tool-line">▸ {{ t }}</div>
              </div>
            </div>
          </div>
          <el-empty v-if="timeline.length === 0 && !streaming" :image-size="50" description="还没有活动" />
        </div>

        <!-- 追加指令 -->
        <div v-if="isAdmin" class="td-composer">
          <div class="composer-row">
            <el-select v-model="atSlug" placeholder="@ 谁" class="at-select" size="default">
              <el-option v-for="a in team" :key="a.id" :value="a.slug"
                         :label="`${dName(a)}${a.is_leader ? ' 👑' : ''}`" />
            </el-select>
            <el-button v-if="streaming" type="danger" :icon="VideoPause" circle
                       title="停止此任务执行" @click="doKill" />
          </div>
          <el-input v-model="input" type="textarea" :rows="3" :disabled="streaming"
                    placeholder="下达指令，Enter 发送 / Shift+Enter 换行" @keydown.enter.exact.prevent="send" />
          <div class="composer-foot">
            <el-button class="akivili-primary-btn" :disabled="streaming || !input.trim()" @click="send">发送</el-button>
          </div>
        </div>
        <div v-else class="readonly-hint">👁 只读模式 · 登录管理员后可安排任务</div>
      </div>

      <!-- 属性侧栏 -->
      <div class="td-side">
        <div class="side-block">
          <div class="side-row">
            <span class="side-label">状态</span>
            <el-select :model-value="task.status" size="small" :disabled="!isAdmin"
                       @change="(v) => changeStatus(v)" class="side-ctrl">
              <el-option v-for="s in STATUS_OPTS" :key="s" :value="s" :label="statusLabel(s)" />
            </el-select>
          </div>
          <div class="side-row">
            <span class="side-label">优先级</span>
            <el-select :model-value="task.priority || 'none'" size="small" :disabled="!isAdmin"
                       @change="(v) => changePriority(v)" class="side-ctrl">
              <el-option v-for="p in PRIORITY_OPTS" :key="p" :value="p" :label="priorityLabel(p)" />
            </el-select>
          </div>
          <div class="side-row">
            <span class="side-label">负责人</span>
            <span class="side-val">{{ assigneeName() || '—' }}</span>
          </div>
          <div class="side-row">
            <span class="side-label">创建</span>
            <span class="side-val">{{ shortTime(task.created_at) }}</span>
          </div>
        </div>

        <div class="side-block">
          <div class="side-block-title">执行日志</div>
          <!-- 执行进度：父任务或子任务还有 Agent 在跑/排队时显示 -->
          <div v-if="progress.active" class="exec-progress">
            <div class="exec-progress-head">
              <span class="exec-spinner">⚙️</span>
              <span>执行中 · 子任务 {{ progress.sub_done }}/{{ progress.sub_total }} 完成</span>
            </div>
            <div v-for="(r, i) in progress.running" :key="'run' + i" class="exec-line">
              <span class="exec-tag running" :title="isAdmin ? '点击暂停该 Agent' : ''"
                    @click="isAdmin && onPauseAgent(r)">运行中</span>
              <span class="exec-agent">{{ agentDisplayBySlug(r.agent_slug) }}</span>
              <span v-if="r.is_sub" class="exec-sub" @click="r.task_id && openTask(r.task_id)">子任务›</span>
            </div>
            <div v-for="(r, i) in progress.queued" :key="'q' + i" class="exec-line">
              <span class="exec-tag queued">排队中</span>
              <span class="exec-agent">{{ agentDisplayBySlug(r.agent_slug) }}</span>
              <span v-if="r.is_sub" class="exec-sub">子任务</span>
            </div>
          </div>
          <div v-else-if="progress.sub_total > 0" class="exec-progress done-hint">
            子任务 {{ progress.sub_done }}/{{ progress.sub_total }} 完成{{ progress.sub_done === progress.sub_total ? ' · 待负责人汇总收尾' : '' }}
          </div>
          <div v-for="r in runs" :key="r.id" class="run-item">
            <div class="run-head">
              <span class="run-status" :class="`rs-${r.status}`"
                    :title="runDotTitle(r)"
                    @click.stop="isAdmin && onRunDot(r)">{{ runStatusLabel(r.status) }}</span>
              <span class="run-agent" @click="toggleRun(r.id)">{{ agentDisplayBySlug(r.agent_slug) }}</span>
              <span class="run-toggle" @click="toggleRun(r.id)">{{ openRuns[r.id] ? '▾' : '▸' }}</span>
            </div>
            <div v-if="openRuns[r.id]" class="run-logs">
              <div v-for="(l, i) in (runLogs[r.id] || [])" :key="i" class="run-log" :class="l.channel">{{ l.content }}</div>
              <div v-if="(runLogs[r.id] || []).length === 0" class="run-log">（无日志）</div>
            </div>
          </div>
          <div v-if="runs.length === 0" class="side-empty">还没有执行记录</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, inject, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, VideoPause } from '@element-plus/icons-vue'
import { tasksApi, runsApi, projectAgentsApi } from '../api'
import { displayName } from '../utils/agentDisplay'
import AgentAvatar from '../components/AgentAvatar.vue'

const route = useRoute()
const router = useRouter()
const pid = Number(route.params.id)
const taskId = Number(route.params.taskId)
const isAdmin = inject('isAdmin')
const currentUser = inject('currentUser')
const userName = computed(() => currentUser?.value?.username || '我')

const STATUS_OPTS = ['backlog', 'in_progress', 'reviewing', 'blocked', 'done']
const PRIORITY_OPTS = ['urgent', 'high', 'medium', 'low', 'none']
const STATUS_LABEL = {
  backlog: '待办', in_progress: '进行中', reviewing: '验证中', done: '已完成', blocked: '阻塞',
}
const PRIORITY_LABEL = { urgent: '🔴 紧急', high: '🟠 高', medium: '🟡 中', low: '🔵 低', none: '⚪ 无' }

const loading = ref(false)
const task = ref(null)
const team = ref([])
const timeline = ref([])
const subtasks = ref([])
const runs = ref([])
const runLogs = ref({})
const openRuns = ref({})
const input = ref('')
const atSlug = ref('')
const streaming = ref(false)
const streamText = ref('')
const toolEvents = ref([])
const currentRunId = ref(null)
const addSubVisible = ref(false)
const newSubTitle = ref('')
const tlEl = ref(null)
const atBottom = ref(true)
const progress = ref({ running: [], queued: [], sub_total: 0, sub_done: 0, active: false })
let pollTimer = null

const subDone = computed(() => subtasks.value.filter((s) => s.status === 'done').length)
const currentAgent = computed(() => team.value.find((x) => x.slug === atSlug.value) || null)
const currentAgentName = computed(() => {
  const a = currentAgent.value
  return a ? dName(a) : '负责人'
})

// 聊天气泡：解析一条消息的发言人（后端已带 author；user 消息=当前登录管理员）
function msgAgent(it) {
  if (it.role === 'user') return { name: userName.value, emoji: '👤' }
  return it.author || team.value.find((x) => x.slug === it.author_slug) || { name: 'Agent', emoji: '🤖' }
}
function msgName(it) {
  if (it.role === 'user') return userName.value
  const a = it.author || team.value.find((x) => x.slug === it.author_slug)
  return a ? displayName(a) : '成员'
}

function dName(a) { return displayName(a) }
function statusLabel(s) { return STATUS_LABEL[s] || s }
function priorityLabel(p) { return PRIORITY_LABEL[p] || p }
function runStatusLabel(s) {
  return { running: '⚙️ 执行中', succeeded: '✓ 完成', failed: '✗ 失败', killed: '■ 终止' }[s] || s
}
function shortTime(t) { return (t || '').slice(5, 16) }
function assigneeName() {
  const a = team.value.find((x) => x.slug === task.value?.assignee_slug)
  return a ? displayName(a) : ''
}
function activityText(it) {
  // 优先用解析出的成员昵称（花火/流萤…），否则回退后端 actor_display / 登录名
  const who = (it.author && displayName(it.author))
    || it.actor_display || it.actor_name
    || (it.actor_type === 'user' ? userName.value : it.actor_type === 'agent' ? 'Agent' : '系统')
  const d = it.detail || {}
  if (it.action === 'status_changed') return `${who}：状态 ${statusLabel(d.from)} → ${statusLabel(d.to)}`
  if (it.action === 'priority_changed') return `${who}：优先级 ${priorityLabel(d.from)} → ${priorityLabel(d.to)}`
  if (it.action === 'commented') return `${who}：${d.note || ''}`
  if (it.action === 'task_completed') return `${who} 执行完成`
  if (it.action === 'task_failed') return `${who} 执行失败${d.reason ? '：' + d.reason : ''}`
  if (it.action === 'task_started') return `${who} 开始执行`
  if (it.action === 'created') return `${who} 创建了任务`
  return `${who} ${it.action}`
}
function agentDisplayBySlug(slug) {
  const a = team.value.find((x) => x.slug === slug)
  return a ? displayName(a) : slug
}
function subAgent(s) {
  return team.value.find((x) => x.slug === s.assignee_slug) || { emoji: '🤖' }
}

// 动态折叠
const folded = ref({})
function isFolded(i) { return !!folded.value[i] }
function toggleFold(i) { folded.value[i] = !folded.value[i] }
function foldPreview(t) { const s = (t || '').replace(/\s+/g, ' ').trim(); return s.length > 40 ? s.slice(0, 40) + '…' : s }
const allFolded = ref(false)
function toggleFoldAll() {
  allFolded.value = !allFolded.value
  const nf = {}
  timeline.value.forEach((it, i) => { if (it.kind === 'message') nf[i] = allFolded.value })
  folded.value = nf
}

// 打开某个任务详情（子任务也是正常任务，有独立详情页）
function openTask(id) { if (id && id !== taskId) router.push(`/projects/${pid}/tasks/${id}`) }

// 子任务状态点：进行中→暂停(kill该任务在跑的run)；失败/阻塞→重跑(唤醒owner)
async function onDotClick(s) {
  if (s.status === 'in_progress') {
    await pauseTask(s.id)
  } else if (s.status === 'failed' || s.status === 'blocked' || s.status === 'backlog') {
    await rerunTask(s.id)
  } else {
    ElMessage.info(`子任务当前为「${statusLabel(s.status)}」，无需操作`)
  }
}
async function pauseTask(tid) {
  try {
    const { runs } = await tasksApi.runs(tid)
    const running = (runs || []).find((r) => r.status === 'running')
    if (running) { await runsApi.kill(running.id); ElMessage.info('已发送暂停信号') }
    else ElMessage.info('该任务当前没有正在执行的 run')
  } catch (e) { ElMessage.error('暂停失败：' + e.message) }
}
async function rerunTask(tid) {
  try { await runsApi.autoDispatch(tid); ElMessage.success('已重新触发执行'); await refreshLite() }
  catch (e) { ElMessage.error(e?.response?.data?.detail || '重跑失败') }
}
// 执行日志区 run 状态点
function runDotTitle(r) {
  if (!isAdmin) return ''
  if (r.status === 'running') return '点击暂停此次执行'
  if (r.status === 'failed' || r.status === 'killed') return '点击在该任务上重新执行'
  return ''
}
async function onRunDot(r) {
  if (r.status === 'running') { await runsApi.kill(r.id); ElMessage.info('已发送暂停信号') }
  else if (r.status === 'failed' || r.status === 'killed') { await rerunTask(r.task_id || taskId) }
}
async function onPauseAgent(r) {
  // 进度面板里的运行项：按 task_id 找其 running run 并 kill
  await pauseTask(r.task_id || taskId)
}
async function refreshLite() {
  await Promise.all([loadSubtasks(), loadRuns(), loadProgress()])
}

function goBack() { router.push(`/projects/${pid}?tab=workspace`) }

async function loadAll() {
  loading.value = true
  try {
    team.value = (await projectAgentsApi.list(pid)).agents
    // 用单任务接口取详情（顶层/子任务通用；看板 list 只含顶层任务，取不到子任务）
    task.value = await tasksApi.get(pid, taskId)
    atSlug.value = task.value?.assignee_slug || (team.value[0]?.slug || '')
    atBottom.value = true
    await Promise.all([loadTimeline(), loadSubtasks(), loadRuns(), loadProgress()])
    scrollToBottom(true)
    // 任务在「进行中」→ 自动轮询刷新执行进展
    if (task.value?.status === 'in_progress') startPolling()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e.message)
  } finally {
    loading.value = false
  }
}
async function loadTimeline() { timeline.value = (await tasksApi.activities(pid, taskId)).timeline }
async function loadSubtasks() { subtasks.value = (await tasksApi.subtasks(pid, taskId)).subtasks }
async function loadRuns() { runs.value = (await tasksApi.runs(taskId)).runs }
async function loadProgress() {
  try { progress.value = await tasksApi.progress(pid, taskId) } catch { /* 接口可选 */ }
}
// 用户是否停在页面底部：距底 <120px 视为“跟随”（整页滚动，非内部容器）
function onTlScroll() { /* 保留占位：容器不再内部滚动 */ }
function nearPageBottom() {
  const el = document.scrollingElement || document.documentElement
  return el.scrollHeight - el.scrollTop - el.clientHeight < 120
}
// force=true 无条件到底（首次/自己发言/流式）；否则只有用户本就在页面底部才跟随，避免打断阅读
function scrollToBottom(force = false) {
  if (!force && !nearPageBottom()) return
  nextTick(() => {
    const el = document.scrollingElement || document.documentElement
    el.scrollTop = el.scrollHeight
  })
}

async function changeStatus(v) {
  await tasksApi.setStatus(pid, taskId, v); task.value.status = v; await loadTimeline()
  if (v === 'in_progress') startPolling(); else if (v === 'done') stopPolling()
}
async function changePriority(v) {
  await tasksApi.update(pid, taskId, { priority: v }); task.value.priority = v; await loadTimeline()
}
async function doAddSub() {
  if (!newSubTitle.value.trim()) return
  await tasksApi.createSubtask(pid, taskId, { title: newSubTitle.value.trim() })
  newSubTitle.value = ''; addSubVisible.value = false
  await Promise.all([loadSubtasks(), loadTimeline(), loadProgress()])
}
async function toggleRun(rid) {
  openRuns.value[rid] = !openRuns.value[rid]
  if (openRuns.value[rid] && !runLogs.value[rid]) runLogs.value[rid] = (await runsApi.logs(rid)).logs
}
async function send() {
  if (!input.value.trim() || streaming.value) return
  if (!atSlug.value) return ElMessage.warning('请先 @ 一位成员')
  const prompt = input.value.trim()
  input.value = ''; streaming.value = true; streamText.value = ''; toolEvents.value = []; currentRunId.value = null
  await loadTimeline(); atBottom.value = true; scrollToBottom(true)
  try {
    await runsApi.dispatch(taskId, prompt, atSlug.value, (ev) => {
      if (ev.type === 'system' && ev.meta?.run_id) currentRunId.value = ev.meta.run_id
      else if (ev.type === 'text') { streamText.value += ev.text; scrollToBottom() }
      else if (ev.type === 'tool') toolEvents.value.push(ev.text)
      else if (ev.type === 'error') { ElMessage.error(ev.text); toolEvents.value.push('❌ ' + ev.text) }
    })
  } catch (e) { ElMessage.error('执行失败：' + e.message) }
  finally {
    streamText.value = ''; streaming.value = false; currentRunId.value = null
    await Promise.all([loadTimeline(), loadRuns(), loadProgress()]); scrollToBottom()
  }
}
async function doKill() { if (currentRunId.value) { await runsApi.kill(currentRunId.value); ElMessage.info('已发送终止信号') } }
function startPolling() {
  stopPolling()
  let idleRounds = 0
  pollTimer = setInterval(async () => {
    await Promise.all([loadTimeline(), loadRuns(), loadSubtasks(), loadProgress()])
    scrollToBottom()   // 仅当用户停在底部才跟随，不打断向上阅读
    // 队列排空连续两轮则停轮询
    if (!progress.value.active) { idleRounds += 1; if (idleRounds >= 2) stopPolling() }
    else idleRounds = 0
  }, 3000)
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }

onMounted(loadAll)
onUnmounted(stopPolling)
</script>

<style scoped>
.task-detail { max-width: 1280px; margin: 0 auto; }
.td-topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.td-crumb { display: flex; align-items: center; gap: 8px; }
.crumb-sep { color: #c0c4cc; }
.crumb-title { font-weight: 600; color: #303133; }
.crumb-link { color: #409eff; cursor: pointer; }
.crumb-link:hover { text-decoration: underline; }
.td-body { display: flex; gap: 24px; align-items: flex-start; }
.td-main { flex: 1; min-width: 0; }
.td-side { width: 300px; flex-shrink: 0; }
.td-title { font-size: 24px; font-weight: 700; margin: 0 0 12px; }
.td-desc { background: #f5f7fa; border-radius: 8px; padding: 14px 16px; font-size: 14px;
  line-height: 1.7; color: #303133; white-space: pre-wrap; margin-bottom: 20px; }
.td-section { margin-bottom: 20px; }
.td-sec-head { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 15px; margin-bottom: 10px; }
.td-sub-progress { font-size: 12px; color: #909399; }
.td-sub-add { margin-bottom: 8px; }
.td-sub-row { display: flex; align-items: center; gap: 10px; padding: 8px 6px; font-size: 14px; border-bottom: 1px solid #f5f5f5; cursor: pointer; border-radius: 6px; }
.td-sub-row:hover { background: #f5f7fa; }
.sub-dot { font-size: 12px; cursor: pointer; }
.sub-av { flex-shrink: 0; }
.st-done { color: #67c23a; } .st-in_progress { color: #e6a23c; } .st-backlog { color: #c0c4cc; }
.st-blocked { color: #f56c6c; } .st-reviewing { color: #409eff; } .st-failed { color: #f56c6c; }
.sub-title { flex: 1; } .sub-status { font-size: 12px; color: #909399; }
.sub-arrow { color: #c0c4cc; font-size: 16px; }
.td-empty, .side-empty { color: #c0c4cc; font-size: 13px; padding: 4px 0; }
.td-tl-divider { display: flex; align-items: center; gap: 10px; font-size: 16px; color: #303133; border-top: 1px solid #ebeef5; padding-top: 16px; margin-bottom: 12px; font-weight: 700; }
.tl-count { font-size: 12px; font-weight: 400; color: #909399; background: #f0f2f5; padding: 1px 8px; border-radius: 10px; }
.tl-fold-all { margin-left: auto; font-weight: 400; }
/* 动态区不框死高度：随内容自然增高，整页可下滑（外层滚动） */
.td-timeline { padding-right: 8px; }
.fold-caret { display: inline-block; width: 14px; color: #c0c4cc; }
.chat-name { cursor: pointer; }
.chat-bubble.folded { color: #909399; font-style: italic; cursor: pointer; background: #fafafa; }
.tl-activity { display: flex; align-items: center; gap: 8px; padding: 5px 0; font-size: 12px; color: #909399; }
.tl-av { flex-shrink: 0; }
.tl-actor-ic { width: 18px; text-align: center; flex-shrink: 0; font-size: 13px; }
.tl-text { flex: 1; }
.tl-time { font-size: 11px; color: #c0c4cc; }
/* 聊天气泡 */
.chat-row { display: flex; gap: 10px; margin: 14px 0; align-items: flex-start; }
.chat-row.mine { flex-direction: row-reverse; }
.chat-av { flex-shrink: 0; margin-top: 2px; }
.chat-main { min-width: 0; max-width: 82%; }
.chat-name { font-size: 12px; color: #909399; margin-bottom: 4px; }
.chat-row.mine .chat-name { text-align: right; }
.chat-time { color: #c0c4cc; margin-left: 8px; font-size: 11px; }
.chat-bubble { white-space: pre-wrap; word-break: break-word; line-height: 1.7; font-size: 14px;
  padding: 10px 14px; border-radius: 10px; background: #f5f7fa; color: #303133; }
.chat-row.mine .chat-bubble { background: #ecf3ff; }
.running { color: #e6a23c; margin-left: 6px; }
.tools { margin-top: 6px; }
.tool-line { font-size: 12px; color: #909399; font-family: monospace; }
.td-composer { border-top: 1px solid #ebeef5; padding-top: 14px; margin-top: 14px; }
.composer-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.at-select { width: 220px; }
.composer-foot { display: flex; justify-content: flex-end; margin-top: 8px; }
.readonly-hint { border-top: 1px solid #ebeef5; padding: 16px 0 4px; text-align: center; color: #909399; font-size: 13px; }
.side-block { background: #fafbfc; border: 1px solid #ebeef5; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
.side-block-title { font-size: 14px; font-weight: 600; color: #606266; margin-bottom: 12px; }
.side-row { display: flex; align-items: center; margin-bottom: 12px; }
.side-row:last-child { margin-bottom: 0; }
.side-label { width: 56px; font-size: 13px; color: #909399; flex-shrink: 0; }
.side-ctrl { flex: 1; }
.side-val { font-size: 13px; color: #303133; }
.run-item { border: 1px solid #ebeef5; border-radius: 8px; margin-bottom: 8px; background: #fff; }
.run-head { display: flex; align-items: center; gap: 8px; padding: 9px 12px; cursor: pointer; font-size: 12px; }
.run-status { font-weight: 600; cursor: pointer; }
.rs-running { color: #e6a23c; } .rs-succeeded { color: #67c23a; } .rs-failed { color: #f56c6c; } .rs-killed { color: #909399; }
.run-agent { flex: 1; color: #606266; cursor: pointer; }
.run-toggle { color: #c0c4cc; cursor: pointer; }
.run-logs { border-top: 1px solid #f5f5f5; padding: 8px 10px; max-height: 200px; overflow-y: auto; background: #1e1e1e; border-radius: 0 0 8px 8px; }
.run-log { font-family: monospace; font-size: 11px; line-height: 1.5; color: #d4d4d4; word-break: break-word; }
.run-log.stderr { color: #f48771; }
.exec-progress { background: #fdf6ec; border: 1px solid #f5dab1; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.exec-progress.done-hint { background: #f0f9eb; border-color: #d1edc4; color: #67c23a; font-size: 12px; }
.exec-progress-head { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; color: #b88230; margin-bottom: 8px; }
.exec-spinner { display: inline-block; animation: spin 1.4s linear infinite; }
@keyframes spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }
.exec-line { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 3px 0; }
.exec-tag { font-size: 10px; padding: 1px 6px; border-radius: 3px; flex-shrink: 0; }
.exec-tag.running { background: #e6a23c; color: #fff; cursor: pointer; }
.exec-tag.queued { background: #ebeef5; color: #909399; }
.exec-agent { flex: 1; color: #606266; }
.exec-sub { font-size: 10px; color: #c0c4cc; }
</style>


