<template>
  <div class="workspace">
    <div class="topbar">
      <el-button v-if="!embed" :icon="ArrowLeft" @click="$router.push(`/projects/${pid}`)">返回项目</el-button>
      <h2 v-if="!embed">工作区 · {{ project?.title || '' }}</h2>
      <div v-else class="embed-bar"><span class="board-hint">任务看板</span></div>
      <el-button v-if="isAdmin" class="akivili-primary-btn" :icon="Plus" @click="openCreate">新建任务</el-button>
    </div>

    <div v-loading="loading" class="board">
      <div v-for="col in COLUMNS" :key="col.key" class="column" :class="[`col-${col.key}`, { 'drop-hover': dragOverCol === col.key }]"
           @dragover.prevent="dragOverCol = col.key" @dragleave="dragOverCol = ''"
           @drop="onDrop(col.key)">
        <div class="col-head">
          <span class="col-title">{{ col.label }}</span>
          <span class="col-count">{{ (board[col.key] || []).length }}</span>
        </div>
        <div class="col-body">
          <div v-for="t in board[col.key] || []" :key="t.id" class="task-card"
               :class="{ dragging: dragTask && dragTask.id === t.id }"
               :style="{ borderLeftColor: cardColor(t.id) }"
               :draggable="isAdmin" @dragstart="onDragStart(t)" @dragend="onDragEnd"
               @click="onCardClick(t)">
            <div v-if="isAdmin" class="tc-ops">
              <el-button v-if="t.status === 'backlog'" text size="small" :icon="Edit" title="编辑（仅待办可编辑）" @click.stop="openEdit(t)" />
              <el-button text size="small" :icon="Delete" title="删除" @click.stop="removeTask(t)" />
            </div>
            <div class="tc-title">
              <span v-if="t.priority && t.priority !== 'none'" class="tc-prio" :title="'优先级'">{{ prioDot(t.priority) }}</span>
              {{ t.title }}
            </div>
            <div class="tc-desc" v-if="t.description">{{ t.description }}</div>
            <div v-if="t.last_result" class="tc-result">
              <div class="tc-result-label">💬 最新结果（{{ t.msg_count }} 条对话）</div>
              <div class="tc-result-text">{{ t.last_result }}</div>
            </div>
            <div class="tc-foot">
              <span v-if="t.sub_total > 0" class="tc-sub">☑ {{ t.sub_done }}/{{ t.sub_total }}</span>
              <span class="tc-run" :class="runClass(t.run_status)">{{ runLabel(t.run_status) }}</span>
            </div>
            <div class="tc-meta">
              <span class="tc-assignee">
                <AgentAvatar :agent="assigneeAgent(t)" :size="20" />
                <span class="tc-assignee-name">{{ assigneeAgent(t) ? memberName(t.assignee_slug) : '未分派' }}</span>
              </span>
              <span class="tc-time">更新于 {{ relTime(t.updated_at) }}</span>
            </div>
            <!-- 子任务：嵌套小卡，点击进入其详情 -->
            <div v-if="t.subtasks && t.subtasks.length" class="tc-subs">
              <div v-for="s in t.subtasks" :key="s.id" class="tc-sub-card"
                   @click.stop="openTask(s)">
                <span class="scb-dot" :class="`st-${s.status}`">●</span>
                <span v-if="s.priority && s.priority !== 'none'" class="scb-prio">{{ prioDot(s.priority) }}</span>
                <AgentAvatar :agent="assigneeAgent(s)" :size="16" class="scb-av" />
                <span class="scb-title">{{ s.title }}</span>
                <span class="scb-status">{{ statusLabel(s.status) }}</span>
              </div>
            </div>
          </div>
          <div v-if="(board[col.key] || []).length === 0" class="col-empty">拖拽任务到此</div>
        </div>
      </div>
    </div>

    <!-- 新建任务 -->
    <el-dialog v-model="createVisible" title="✦ 新建任务" width="600px" class="task-dialog" append-to-body>
      <el-form label-position="top">
        <el-form-item label="任务标题" required>
          <el-input v-model="form.title" placeholder="要做什么，如 实现登录接口" />
        </el-form-item>
        <el-form-item label="负责人 Owner（对结果负责，会拉人协调）" required>
          <el-select v-model="form.assignee_slug" placeholder="指定一位负责人" style="width:100%">
            <el-option v-for="a in team" :key="a.id" :value="a.slug" :label="ownerLabel(a)">
              <span class="owner-opt">
                <AgentAvatar :agent="a" :size="22" />
                <span class="owner-opt-name">{{ dName(a) }}{{ a.is_leader ? ' 👑' : '' }}</span>
              </span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="任务描述（输入 @ 可点名项目成员）">
          <MentionTextarea v-model="form.description" :members="team" :rows="10"
                           placeholder="详细描述任务目标、背景与要求…输入 @ 点名负责的数字人才" />
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="form.priority" style="width:160px">
            <el-option v-for="p in PRIORITY_OPTS" :key="p.v" :value="p.v" :label="p.l" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button class="akivili-primary-btn" @click="doCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑任务 -->
    <el-dialog v-model="editVisible" title="✦ 编辑任务" width="600px" class="task-dialog" append-to-body>
      <el-form label-position="top">
        <el-form-item label="任务标题" required>
          <el-input v-model="editForm.title" placeholder="要做什么" />
        </el-form-item>
        <el-form-item label="负责人 Owner（对结果负责，会拉人协调）" required>
          <el-select v-model="editForm.assignee_slug" placeholder="指定一位负责人" style="width:100%">
            <el-option v-for="a in team" :key="a.id" :value="a.slug" :label="ownerLabel(a)">
              <span class="owner-opt">
                <AgentAvatar :agent="a" :size="22" />
                <span class="owner-opt-name">{{ dName(a) }}{{ a.is_leader ? ' 👑' : '' }}</span>
              </span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="任务描述（输入 @ 可点名项目成员）">
          <MentionTextarea v-model="editForm.description" :members="team" :rows="10"
                           placeholder="详细描述任务目标、背景与要求…输入 @ 点名负责的数字人才" />
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="editForm.priority" style="width:160px">
            <el-option v-for="p in PRIORITY_OPTS" :key="p.v" :value="p.v" :label="p.l" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button class="akivili-primary-btn" @click="doEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Plus, Edit, Delete } from '@element-plus/icons-vue'
import { projectsApi, projectAgentsApi, tasksApi, runsApi } from '../api'
import { displayName } from '../utils/agentDisplay'
import AgentAvatar from '../components/AgentAvatar.vue'
import MentionTextarea from '../components/MentionTextarea.vue'

const isAdmin = inject('isAdmin')

const props = defineProps({
  embed: { type: Boolean, default: false },
  pidProp: { type: Number, default: 0 },
  teamProp: { type: Array, default: null },
})

// 看板列：待办 / 进行中 / 验证中 / 已完成。任务执行完成自动进「验证中」，人工验收后拖入「已完成」。
// 阻塞(blocked) 暂无独立列，load() 里并入「进行中」。
const COLUMNS = [
  { key: 'backlog', label: '待办' },
  { key: 'in_progress', label: '进行中' },
  { key: 'reviewing', label: '验证中' },
  { key: 'done', label: '已完成' },
]
function prioDot(p) {
  return { urgent: '🔴', high: '🟠', medium: '🟡', low: '🔵' }[p] || ''
}
function statusLabel(s) {
  return { backlog: '待办', in_progress: '进行中', reviewing: '验证中',
           done: '已完成', blocked: '阻塞', archived: '归档' }[s] || s
}
// 卡片左边框色：按 id 稳定映射到一组 Trello 风配色
const CARD_COLORS = ['#61bd4f', '#f2d600', '#ff9f1a', '#eb5a46', '#c377e0',
                     '#0079bf', '#00c2e0', '#51e898', '#ff78cb', '#344563']
function cardColor(id) {
  return CARD_COLORS[id % CARD_COLORS.length]
}

const route = useRoute()
const router = useRouter()
const pid = props.pidProp || Number(route.params.id)
const project = ref(null)
const team = ref([])
const board = ref({})
const loading = ref(false)
let pollTimer = null

const createVisible = ref(false)
const PRIORITY_OPTS = [
  { v: 'urgent', l: '🔴 紧急' }, { v: 'high', l: '🟠 高' }, { v: 'medium', l: '🟡 中' },
  { v: 'low', l: '🔵 低' }, { v: 'none', l: '⚪ 无' },
]
const form = ref({ title: '', description: '', priority: 'none', assignee_slug: '' })

const editVisible = ref(false)
const editForm = ref({ id: null, title: '', description: '', priority: 'none', assignee_slug: '' })
function dName(a) { return displayName(a) }
function ownerLabel(a) { return `${displayName(a)}${a.is_leader ? ' 👑' : ''}` }

function openEdit(t) {
  editForm.value = { id: t.id, title: t.title, description: t.description || '', priority: t.priority || 'none', assignee_slug: t.assignee_slug || '' }
  editVisible.value = true
}
async function doEdit() {
  if (!editForm.value.title.trim()) return ElMessage.warning('标题必填')
  if (!editForm.value.assignee_slug) return ElMessage.warning('请指定一位负责人 Owner')
  await tasksApi.update(pid, editForm.value.id, {
    title: editForm.value.title, description: editForm.value.description,
    priority: editForm.value.priority, assignee_slug: editForm.value.assignee_slug,
  })
  ElMessage.success('已保存')
  editVisible.value = false
  await load()
}
async function removeTask(t) {
  try {
    await ElMessageBox.confirm(`确定删除任务「${t.title}」？此操作不可撤销。`, '删除任务',
      { type: 'warning', confirmButtonText: '确定删除', cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger' })
  } catch { return }
  await tasksApi.remove(pid, t.id)
  ElMessage.success('已删除')
  await load()
}

async function load() {
  loading.value = true
  try {
    // 始终自己拉取团队，避免依赖父组件异步 prop 的时序（embed 下 teamProp 可能尚为空）
    if (props.teamProp && props.teamProp.length) {
      team.value = props.teamProp
    } else {
      team.value = (await projectAgentsApi.list(pid)).agents
    }
    if (!props.embed) {
      project.value = await projectsApi.get(pid)
    }
    const bd = (await tasksApi.list(pid)).board
    // 阻塞(blocked) 暂无独立列，并入「进行中」防丢失；验证中(reviewing)已有独立列，不并入
    bd.in_progress = [...(bd.in_progress || []), ...(bd.blocked || [])]
    board.value = bd
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e.message)
  } finally {
    loading.value = false
  }
}

defineExpose({ load })

// —— 拖拽 ——
const dragTask = ref(null)
const dragOverCol = ref('')
let justDragged = false
function onDragStart(t) { dragTask.value = t; justDragged = true }
function onDragEnd() { setTimeout(() => { justDragged = false }, 50); dragTask.value = null; dragOverCol.value = '' }
function onCardClick(t) {
  if (justDragged) return   // 刚拖拽过，不当作点击
  openTask(t)
}
async function onDrop(status) {
  const t = dragTask.value
  dragTask.value = null
  dragOverCol.value = ''
  if (!t || t.status === status) return
  try {
    await tasksApi.setStatus(pid, t.id, status)
    // 拖到「进行中」→ 让描述里 @ 的首位成员后台执行
    if (status === 'in_progress') {
      try {
        const r = await runsApi.autoDispatch(t.id)
        ElMessage.success('负责人已开始统筹：' + memberName(r.owner || r.assignee))
      } catch (e) {
        ElMessage.warning(e?.response?.data?.detail || '未能自动执行')
      }
    }
    await load()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e.message)
  }
}

function memberName(slug) {
  const a = team.value.find((x) => x.slug === slug)
  return a ? displayName(a) : slug
}
function assigneeAgent(t) {
  return team.value.find((x) => x.slug === t.assignee_slug) || null
}
// 相对时间，分钟粒度：刚刚 / N 分钟前 / N 小时前 / N 天前
function relTime(ts) {
  if (!ts) return '未知'
  // 后端 datetime('now') 是 UTC，无时区标记，补上 Z 再解析
  const t = new Date(ts.replace(' ', 'T') + 'Z').getTime()
  if (isNaN(t)) return '未知'
  const mins = Math.floor((Date.now() - t) / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs} 小时前`
  const days = Math.floor(hrs / 24)
  return `${days} 天前`
}

// —— 执行状态徽标 ——
function runLabel(s) {
  return { running: '⚙️ 执行中', succeeded: '✓ 执行完成', failed: '✗ 失败', killed: '■ 已终止' }[s] || ''
}
function runClass(s) {
  return s ? `run-${s}` : ''
}

function openCreate() {
  form.value = { title: '', description: '', priority: 'none', assignee_slug: '' }
  createVisible.value = true
}

async function doCreate() {
  if (!form.value.title.trim()) return ElMessage.warning('标题必填')
  if (!form.value.assignee_slug) return ElMessage.warning('请指定一位负责人 Owner')
  await tasksApi.create(pid, form.value)
  ElMessage.success('已创建')
  createVisible.value = false
  await load()
}

async function moveTask(t, status) {
  await tasksApi.setStatus(pid, t.id, status)
  await load()
}

function openTask(t) {
  router.push(`/projects/${pid}/tasks/${t.id}`)
}

onMounted(() => {
  load()
  // 有执行中的任务时定时刷新状态徽标
  pollTimer = setInterval(() => {
    const hasRunning = Object.values(board.value).flat().some((t) => t.run_status === 'running')
    if (hasRunning) load()
  }, 3000)
})
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.workspace { width: 100%; }
.topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.topbar h2 { margin: 0; flex: 1; font-size: 18px; }
.board { display: flex; gap: 14px; align-items: flex-start; overflow-x: auto; padding-bottom: 8px; }
.column { flex: 1; min-width: 300px; background: #f0f2f5; border-radius: 12px; padding: 12px; transition: background .15s, box-shadow .15s; }
/* 各状态淡背景色（Trello 风、克制） */
.col-backlog { background: #f4f5f7; }
.col-in_progress { background: #fef6e6; }
.col-reviewing { background: #eaf4ff; }
.col-done { background: #eaf7ed; }
.column.drop-hover { background: #e3ecff; box-shadow: inset 0 0 0 2px #a0c0ff; }
.col-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; padding: 0 4px; }
.col-title { font-weight: 600; font-size: 14px; color: #303133; }
.col-count { font-size: 12px; color: #909399; background: #e0e3e8; border-radius: 10px; padding: 0 8px; }
.col-body { display: flex; flex-direction: column; gap: 10px; min-height: 40px; }
.task-card {
  cursor: pointer; position: relative;
  background: #fff; border: 1px solid #ebeef5; border-left: 5px solid #ccc; border-radius: 8px;
  padding: 14px 16px; box-shadow: 0 1px 3px rgba(9,30,66,0.13);
  transition: box-shadow .15s, transform .15s;
}
.task-card.dragging { opacity: 0.4; }
.task-card:hover { box-shadow: 0 4px 14px rgba(9,30,66,0.2); transform: translateY(-1px); }
.tc-ops { position: absolute; top: 8px; right: 8px; z-index: 2; display: flex; gap: 0; opacity: 0; transition: opacity .15s; }
.task-card:hover .tc-ops { opacity: 1; }
.tc-ops .el-button { padding: 2px; margin: 0; }
.task-card[draggable="true"] { cursor: grab; }
.task-card[draggable="true"]:active { cursor: grabbing; }
.tc-title { font-weight: 600; font-size: 15px; line-height: 1.45; margin-bottom: 8px; padding-right: 56px; }
.tc-desc {
  color: #5e6c84; font-size: 13px; line-height: 1.55; margin-bottom: 10px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.tc-foot { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.tc-prio { margin-right: 4px; font-size: 11px; }
.tc-sub { font-size: 12px; color: #909399; }
.tc-meta { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 10px; padding-top: 8px; border-top: 1px solid #f2f3f5; }
.tc-assignee { display: flex; align-items: center; gap: 6px; min-width: 0; }
.tc-assignee-name { font-size: 12px; color: #5e6c84; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tc-time { font-size: 11px; color: #97a0af; flex-shrink: 0; }
/* 嵌套子任务小卡 */
.tc-subs { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; }
.tc-sub-card { display: flex; align-items: center; gap: 6px; padding: 5px 8px; border-radius: 6px;
  background: #f7f8fa; border: 1px solid #eceef1; cursor: pointer; transition: background .12s, box-shadow .12s; }
.tc-sub-card:hover { background: #eef1f6; box-shadow: 0 2px 8px rgba(9,30,66,0.12); }
.scb-dot { font-size: 9px; flex-shrink: 0; color: #b7bcc5; }
.scb-dot.st-in_progress { color: #e6a23c; }
.scb-dot.st-done { color: #67c23a; }
.scb-dot.st-reviewing { color: #409eff; }
.scb-dot.st-blocked { color: #f56c6c; }
.scb-prio { font-size: 10px; flex-shrink: 0; }
.scb-av { flex-shrink: 0; }
.scb-title { flex: 1; min-width: 0; font-size: 12px; color: #42526e; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.scb-status { font-size: 11px; color: #8993a4; flex-shrink: 0; }
.tc-result {
  margin: 8px 0; padding: 8px 10px; border-radius: 6px;
  background: #f0f9eb; border-left: 3px solid #67c23a;
}
.tc-result-label { font-size: 11px; color: #67c23a; margin-bottom: 4px; }
.tc-result-text {
  font-size: 12px; color: #5a6b5a; line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.tc-run { font-size: 12px; }
.run-running { color: #e6a23c; }
.run-succeeded { color: #67c23a; }
.run-failed { color: #f56c6c; }
.run-killed { color: #909399; }
.col-empty { text-align: center; color: #c0c4cc; font-size: 13px; padding: 8px 0; }
</style>

<style>
/* 任务对话框：浅色清爽主题，与工作区一致 */
.task-dialog { border-radius: 14px; overflow: hidden; }
.task-dialog .el-dialog__header { margin: 0; padding: 18px 22px; border-bottom: 1px solid #f0f2f5; }
.task-dialog .el-dialog__title { font-weight: 700; font-size: 16px; color: #172b4d; }
.task-dialog .el-dialog__body { padding: 20px 22px; }
.task-dialog .el-form-item__label { color: #5e6c84; font-weight: 600; }
.task-dialog .el-dialog__footer { padding: 14px 22px 20px; border-top: 1px solid #f0f2f5; }
/* Owner 下拉选项：头像 + 昵称 */
.owner-opt { display: flex; align-items: center; gap: 8px; }
.owner-opt-name { font-size: 14px; }
</style>


