<template>
  <div class="task-detail" v-loading="loading">
    <!-- 面包屑 + 操作 -->
    <div class="td-topbar">
      <div class="td-crumb">
        <el-button text :icon="ArrowLeft" @click="goBack">返回</el-button>
        <span class="crumb-sep">/</span>
        <template v-if="task && task.parent_task_id">
          <span class="crumb-link" @click="openTask(task.parent_task_id)">{{ task.parent_title || '父任务' }}</span>
          <span class="crumb-sep">/</span>
        </template>
        <span class="crumb-title">{{ task?.title || '任务' }}</span>
      </div>
      <div class="td-actions">
        <el-button v-if="isAdmin && task" text size="small" @click="openWecomDialog">📮 发送到企微</el-button>
      </div>
    </div>

    <div v-if="task" class="td-body">
      <!-- 主内容区 -->
      <div class="td-main">
        <h1 class="td-title">{{ task.title }}</h1>
        <div v-if="task.description" class="td-desc"><MarkdownView :text="task.description" /></div>

        <!-- 子任务（子任务本身不再显示此区，因不允许多层拆分） -->
        <div v-if="!isSubtask" class="td-section">
          <div class="td-sec-head">
            <span class="td-sec-title">子任务</span>
            <span v-if="subtasks.length" class="td-sub-progress">{{ subDone }}/{{ subtasks.length }}</span>
            <el-button v-if="isAdmin && !isSubtask" text size="small" @click="openAddSub">+ 新增</el-button>
          </div>
          <div v-for="s in subtasks" :key="s.id" class="td-sub-row" @click="openTask(s.id)">
            <span class="sub-dot" :class="`st-${subEffectiveStatus(s)}`"
                  :title="isAdmin ? '点击控制（暂停/重跑）' : ''"
                  @click.stop="isAdmin && onDotClick(s)">●</span>
            <AgentAvatar :agent="subAgent(s)" :size="20" class="sub-av" />
            <span class="sub-title">{{ s.title }}</span>
            <span class="sub-status">{{ statusLabel(subEffectiveStatus(s)) }}</span>
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
              <AgentAvatar v-if="it.author" :agent="it.author" :size="20" class="tl-av" />
              <AgentAvatar v-else-if="it.actor_type === 'user'" :agent="activityUserAvatar(it)" :size="20" class="tl-av" />
              <span v-else class="tl-actor-ic">⚙️</span>
              <span class="tl-text">{{ activityText(it) }}</span>
              <span class="tl-time">{{ shortTime(it.created_at) }}</span>
            </div>
            <!-- 消息：聊天气泡（头像 + 昵称 + 可折叠内容） -->
            <div v-else class="chat-row" :class="{ mine: it.role === 'user' }">
              <AgentAvatar :agent="msgAgent(it)" :size="20" class="chat-av" />
              <div class="chat-main">
                <div class="chat-name" @click="toggleFold(i)">
                  <span class="fold-caret">{{ isFolded(i) ? '▸' : '▾' }}</span>
                  {{ msgName(it) }}<span class="chat-time">{{ shortTime(it.created_at) }}</span>
                </div>
                <div v-show="!isFolded(i)" class="chat-bubble"><MarkdownView :text="it.content" /></div>
                <div v-show="isFolded(i)" class="chat-bubble folded" @click="toggleFold(i)">{{ foldPreview(it.content) }}</div>
              </div>
            </div>
          </template>
          <!-- 流式执行中气泡 -->
          <div v-if="streaming" class="chat-row">
            <AgentAvatar :agent="currentAgent" :size="20" class="chat-av" />
            <div class="chat-main">
              <div class="chat-name">{{ currentAgentName }} <span class="running">⚙️ 执行中…</span></div>
              <div class="chat-bubble">{{ streamText || '思考中…' }}</div>
              <div v-if="toolEvents.length" class="tools">
                <div v-for="(t, ti) in toolEvents" :key="ti" class="tool-item">
                  <div class="tool-line" :class="{ clickable: t.detail }" @click="t.detail && (t.open = !t.open)">
                    <span v-if="t.detail" class="tool-caret">{{ t.open ? '▾' : '▸' }}</span>
                    <span v-else class="tool-caret">▸</span>{{ t.text }}
                  </div>
                  <pre v-if="t.open && t.detail" class="tool-detail">{{ t.detail }}</pre>
                </div>
              </div>
            </div>
          </div>
          <el-empty v-if="timeline.length === 0 && !streaming" :image-size="50" description="还没有活动" />
        </div>

        <!-- 追加指令 -->
        <div v-if="isAdmin" class="td-composer">
          <div class="composer-hint">
            <span>输入 <b>@</b> 可点名团队成员协作（可 @ 多位，多轮会话按需引入不同成员）</span>
            <el-button v-if="streaming" type="danger" size="small" :icon="VideoPause"
                       title="停止此任务执行" @click="doKill">停止</el-button>
          </div>
          <div class="composer-input-wrap">
            <el-input ref="inputEl" v-model="input" type="textarea" :rows="6" :disabled="streaming"
                      resize="none"
                      placeholder="下达指令，输入 @ 点名成员；Enter 发送 / Shift+Enter 换行"
                      @input="onInput" @keydown="onComposerKeydown" />
            <!-- @ 补全浮层 -->
            <div v-if="mentionOpen && mentionList.length" class="mention-pop" :style="mentionPopStyle">
              <div v-for="(a, mi) in mentionList" :key="a.slug"
                   class="mention-item" :class="{ active: mi === mentionIdx }"
                   @mousedown.prevent="pickMention(a)">
                <AgentAvatar :agent="a" :size="22" class="mention-av" />
                <span class="mention-name">{{ dName(a) }}</span>
                <span v-if="a.is_leader" class="mention-badge">👑 负责人</span>
                <span v-else class="mention-role">{{ a.name }}</span>
              </div>
            </div>
          </div>
          <div class="composer-foot">
            <span v-if="mentionedNames.length" class="mentioned-chips">
              将唤醒：<el-tag v-for="n in mentionedNames" :key="n" size="small" effect="plain" class="mchip">@{{ n }}</el-tag>
            </span>
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
          <div class="side-block-title">
            执行日志
            <a class="lineage-link" title="端到端链路时间线" @click="goLineage">链路 ↗</a>
          </div>
          <!-- 执行进度：父任务或子任务还有 Agent 在跑/排队时显示 -->
          <div v-if="progress.active" class="exec-progress">
            <div class="exec-progress-head">
              <span class="exec-spinner">⚙️</span>
              <span v-if="progress.sub_total > 0">执行中 · 子任务 {{ progress.sub_done }}/{{ progress.sub_total }} 完成</span>
              <span v-else>执行中</span>
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
            子任务 {{ progress.sub_done }}/{{ progress.sub_total }} 完成{{ subHintSuffix }}
          </div>
          <!-- 进行中的运行：始终展示 -->
          <RunRow v-for="r in activeRuns" :key="r.id" :run="r" :agent="runAgent(r)"
                  :time="relTime(r.started_at)" :is-admin="isAdmin"
                  @ctrl="onRunDot(r)" @detail="openTranscript(r)" />
          <!-- 历史运行：折叠，点开展开全部 -->
          <template v-if="pastRuns.length">
            <div class="run-hist-toggle" @click="showPastRuns = !showPastRuns">
              <span class="rh-caret">{{ showPastRuns ? '▾' : '▸' }}</span>
              {{ showPastRuns ? '隐藏历史运行' : '显示历史运行' }}（{{ pastRuns.length }}）
            </div>
            <template v-if="showPastRuns">
              <RunRow v-for="r in pastRuns" :key="r.id" :run="r" :agent="runAgent(r)"
                      :time="relTime(r.ended_at || r.started_at)" :is-admin="isAdmin"
                      @ctrl="onRunDot(r)" @detail="openTranscript(r)" />
            </template>
          </template>
          <div v-if="runs.length === 0" class="side-empty">还没有执行记录</div>
        </div>
      </div>
    </div>

    <!-- 新增子任务（与工作区新建任务一致的弹框） -->
    <el-dialog v-model="addSubVisible" title="✦ 新增子任务" width="600px" class="task-dialog" append-to-body>
      <el-form label-position="top">
        <el-form-item label="子任务标题" required>
          <el-input v-model="subForm.title" placeholder="要做什么，如 实现登录接口" />
        </el-form-item>
        <el-form-item label="负责人 Owner">
          <el-select v-model="subForm.assignee_slug" placeholder="指定一位负责人" clearable style="width:100%">
            <el-option v-for="a in team" :key="a.id" :value="a.slug" :label="dName(a)">
              <span class="owner-opt">
                <AgentAvatar :agent="a" :size="22" />
                <span class="owner-opt-name">{{ dName(a) }}{{ a.is_leader ? ' 👑' : '' }}</span>
              </span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="任务描述（输入 @ 可点名项目成员）">
          <MentionTextarea v-model="subForm.description" :members="team" :rows="8"
                           placeholder="详细描述子任务目标、背景与要求…输入 @ 点名负责的数字人才" />
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="subForm.priority" style="width:160px">
            <el-option v-for="p in PRIORITY_OPTS" :key="p" :value="p" :label="PRIORITY_LABEL[p]" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addSubVisible = false">取消</el-button>
        <el-button class="akivili-primary-btn" @click="doAddSub">创建</el-button>
      </template>
    </el-dialog>

    <!-- 发送到企微：编辑副标题/正文后一键推送群机器人 -->
    <el-dialog v-model="wecomVisible" title="📮 发送到企微群" width="640px" class="task-dialog" append-to-body>
      <el-form label-position="top">
        <el-form-item label="标题（推送首行，自动用任务标题）">
          <el-input :model-value="task?.title || ''" disabled />
        </el-form-item>
        <el-form-item label="补充说明（可选，夹在标题与正文之间）">
          <el-input v-model="wecomForm.subtitle" type="textarea" :rows="2"
                    placeholder="如：本周直播研发重点关注如下，请相关同学对接" />
        </el-form-item>
        <el-form-item label="正文（默认取本任务最新交付，可编辑）">
          <el-input v-model="wecomForm.body" type="textarea" :rows="10"
                    placeholder="推送到群里的正文内容" />
        </el-form-item>
        <div class="wecom-hint">推送尾部会自动附「详情请点击：任务卡片链接」。企微单条上限 4096 字节，超长自动截断。</div>
      </el-form>
      <template #footer>
        <el-button @click="wecomVisible = false">取消</el-button>
        <el-button class="akivili-primary-btn" :loading="wecomSending" @click="doPushWecom">发送到企微</el-button>
      </template>
    </el-dialog>

    <!-- 日志详情：所有命令与运行时详细信息 -->
    <RunTranscriptDialog v-model="transcriptVisible" :run-id="transcriptRunId"
                         :agent-name="transcriptAgentName" />
  </div>
</template>

<script setup>
import { ref, computed, nextTick, inject, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, VideoPause } from '@element-plus/icons-vue'
import { tasksApi, runsApi, projectAgentsApi } from '../api'
import { displayName } from '../utils/agentDisplay'
import AgentAvatar from '../components/AgentAvatar.vue'
import MentionTextarea from '../components/MentionTextarea.vue'
import RunTranscriptDialog from '../components/RunTranscriptDialog.vue'
import MarkdownView from '../components/MarkdownView.vue'
import RunRow from '../components/RunRow.vue'

const route = useRoute()
const router = useRouter()
// 用 let：点击子任务导航到另一个 /tasks/:taskId 时，Vue Router 复用同一组件、不重跑 onMounted，
// 需在 watch 里更新这两个 id 并重新加载（否则点子任务只换 URL、页面不刷新，表现为"进不去"）。
let pid = Number(route.params.id)
let taskId = Number(route.params.taskId)
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
const showPastRuns = ref(false)
const input = ref('')
const inputEl = ref(null)
// @mention 补全浮层状态
const mentionOpen = ref(false)
const mentionIdx = ref(0)
const mentionQuery = ref('')
const mentionAnchor = ref(0)   // '@' 在 input 中的位置
const streaming = ref(false)
const streamText = ref('')
const toolEvents = ref([])
const currentRunId = ref(null)
const addSubVisible = ref(false)
const subForm = ref({ title: '', assignee_slug: '', description: '', priority: 'none' })
// 发送到企微弹框
const wecomVisible = ref(false)
const wecomSending = ref(false)
const wecomForm = ref({ subtitle: '', body: '' })
// 日志详情弹框
const transcriptVisible = ref(false)
const transcriptRunId = ref(null)
const transcriptAgentName = ref('')
// 当前任务是否本身就是子任务（有 parent_task_id）→ 不允许再建子任务
const isSubtask = computed(() => !!task.value?.parent_task_id)
const tlEl = ref(null)
const atBottom = ref(true)
const progress = ref({ running: [], queued: [], sub_total: 0, sub_done: 0, active: false,
  parent_status: '', summarized: false })
// 子任务进度提示后缀：全完成后按"是否已汇总/是否已验收"给准确措辞
const subHintSuffix = computed(() => {
  const p = progress.value
  if (p.sub_done !== p.sub_total) return ''            // 尚未全完成
  if (p.parent_status === 'done') return ' · 已完成'
  if (p.summarized) return ' · 负责人已汇总，待人工验收'
  return ' · 待负责人汇总收尾'
})
let pollTimer = null

const subDone = computed(() => subtasks.value.filter((s) => s.status === 'done').length)
// 正在流式执行的主受理人（send 时设为第一个被 @ 的成员）
const primarySlug = ref('')
const currentAgent = computed(() => team.value.find((x) => x.slug === primarySlug.value) || null)
const currentAgentName = computed(() => {
  const a = currentAgent.value
  return a ? dName(a) : '成员'
})

// —— @mention 补全 ——
// @ 用名：优先昵称，无则角色名（与后端 parse_and_enqueue_mentions 的匹配口径一致，
// 且插入 @昵称 比 @「昵称（角色）」清爽）
function mentionName(a) { return (a.nickname || '').trim() || a.name || a.slug || '' }
// 候选：当前项目团队成员，按查询词过滤（匹配昵称或角色名，忽略大小写）
const mentionList = computed(() => {
  const q = mentionQuery.value.toLowerCase()
  const list = team.value.filter((a) => {
    if (!q) return true
    const nick = mentionName(a).toLowerCase()
    const role = (a.name || '').toLowerCase()
    return nick.includes(q) || role.includes(q)
  })
  return list.slice(0, 8)
})
// 浮层定位在输入框上方（简单固定在输入区顶部左侧，避免测量光标像素）
const mentionPopStyle = computed(() => ({}))
// 已在文本里 @ 到的成员名（用于底部“将唤醒”提示）
const mentionedNames = computed(() => parseMentions(input.value).map((a) => mentionName(a)))

// 从文本解析被 @ 的团队成员（匹配昵称或角色名，去重）
function parseMentions(text) {
  if (!text || !text.includes('@')) return []
  const hits = []
  const seen = new Set()
  // 按名字长度降序，避免短名误命中长名的一部分
  const sorted = [...team.value].sort((a, b) => mentionName(b).length - mentionName(a).length)
  const tokens = text.match(/@[^\s@，,。、]+/g) || []
  for (const tk of tokens) {
    const name = tk.slice(1)
    for (const a of sorted) {
      const nick = mentionName(a)
      const role = a.name || ''
      if ((name.startsWith(nick) || nick.startsWith(name) ||
           name.startsWith(role) || role.startsWith(name)) && !seen.has(a.slug)) {
        seen.add(a.slug); hits.push(a); break
      }
    }
  }
  return hits
}

function onInput() {
  const el = inputEl.value?.textarea || inputEl.value?.$el?.querySelector('textarea')
  const pos = el ? el.selectionStart : input.value.length
  // 找光标前最近的 '@'，其后到光标之间无空白 → 处于 @ 补全态
  const before = input.value.slice(0, pos)
  const at = before.lastIndexOf('@')
  if (at >= 0 && !/[\s，,。、]/.test(before.slice(at + 1))) {
    mentionAnchor.value = at
    mentionQuery.value = before.slice(at + 1)
    mentionOpen.value = true
    mentionIdx.value = 0
  } else {
    mentionOpen.value = false
  }
}

function pickMention(a) {
  const name = mentionName(a)
  const pos = mentionAnchor.value
  const el = inputEl.value?.textarea || inputEl.value?.$el?.querySelector('textarea')
  const caret = el ? el.selectionStart : input.value.length
  const head = input.value.slice(0, pos)
  const tail = input.value.slice(caret)
  input.value = `${head}@${name} ${tail}`
  mentionOpen.value = false
  nextTick(() => {
    const t = inputEl.value?.textarea || inputEl.value?.$el?.querySelector('textarea')
    if (t) { const np = (head + '@' + name + ' ').length; t.focus(); t.setSelectionRange(np, np) }
  })
}

function onComposerKeydown(e) {
  if (mentionOpen.value && mentionList.value.length) {
    if (e.key === 'ArrowDown') { e.preventDefault(); mentionIdx.value = (mentionIdx.value + 1) % mentionList.value.length; return }
    if (e.key === 'ArrowUp') { e.preventDefault(); mentionIdx.value = (mentionIdx.value - 1 + mentionList.value.length) % mentionList.value.length; return }
    if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pickMention(mentionList.value[mentionIdx.value]); return }
    if (e.key === 'Escape') { mentionOpen.value = false; return }
  }
  // 普通 Enter 发送（Shift+Enter 换行）
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
}

// 聊天气泡：解析一条消息的发言人。user 消息用后端返回的实际发送者名（user_name，
// 即当时发消息的人/任务创建者），而非当前查看者登录名；缺省回退「用户」。
function msgAgent(it) {
  if (it.role === 'user') {
    const uname = it.user_name || userName.value || '用户'
    // 按用户名猜同名头像 <用户名>.png（icon 目录里有则显示，没有则 AgentAvatar 回退 emoji）
    return { name: uname, emoji: '👤', avatar: uname ? `${uname}.png` : '' }
  }
  return it.author || team.value.find((x) => x.slug === it.author_slug) || { name: 'Agent', emoji: '🤖' }
}
// 活动行里 user 操作者的头像：按操作者名猜同名 <名>.png，回退 👤
function activityUserAvatar(it) {
  const uname = it.actor_display || it.actor_name || userName.value || '用户'
  return { name: uname, emoji: '👤', avatar: uname ? `${uname}.png` : '' }
}
function msgName(it) {
  if (it.role === 'user') return it.user_name || userName.value || '用户'
  const a = it.author || team.value.find((x) => x.slug === it.author_slug)
  return a ? displayName(a) : '成员'
}

function dName(a) { return displayName(a) }
function statusLabel(s) { return STATUS_LABEL[s] || s }
// 子任务有效状态：若其在 run_queue 里还有 running/queued 的 run（正在执行/重跑），
// 一律按「进行中」展示，而非其残留的 done/失败旧状态。
function subEffectiveStatus(s) {
  const running = progress.value.running || []
  const queued = progress.value.queued || []
  if (running.some((r) => r.task_id === s.id) || queued.some((r) => r.task_id === s.id)) {
    return 'in_progress'
  }
  return s.status
}
function priorityLabel(p) { return PRIORITY_LABEL[p] || p }
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
// 执行历史：进行中始终展示，历史折叠
const activeRuns = computed(() => runs.value.filter((r) => r.status === 'running'))
const pastRuns = computed(() => runs.value.filter((r) => r.status !== 'running'))
function runAgent(r) { return team.value.find((x) => x.slug === r.agent_slug) || null }
// 相对时间：刚刚 / N 分钟前 / N 小时前 / N 天前（ts 为北京时间 'YYYY-MM-DD HH:MM:SS'）
function relTime(ts) {
  if (!ts) return ''
  const t = new Date(ts.replace(' ', 'T')).getTime()
  if (isNaN(t)) return ''
  const diff = Date.now() - t
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  return `${d} 天前`
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

// 跳运行时链路视图：子任务归到父任务链（链路以顶层任务为根拉全子任务）
function goLineage() {
  const rootId = task.value?.parent_task_id || taskId
  router.push({ path: '/runtime', query: { project: pid, task: rootId } })
}

// 子任务状态点：进行中→暂停(kill该任务在跑的run)；失败/阻塞→重跑(唤醒owner)
// 用有效状态：正在跑/重跑（含 done 后被重新触发）一律按进行中处理，可暂停。
async function onDotClick(s) {
  const st = subEffectiveStatus(s)
  if (st === 'in_progress') {
    await pauseTask(s.id)
  } else if (st === 'failed' || st === 'blocked' || st === 'backlog') {
    await rerunTask(s.id)
  } else {
    ElMessage.info(`子任务当前为「${statusLabel(st)}」，无需操作`)
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
  try {
    // 乐观更新：重跑瞬间本地立即把该子任务 + 当前父任务视图置「进行中」，
    // 不等 3 秒轮询聚合，消除「先显已完成、隔几秒才变进行中」的滞后窗口。
    optimisticReactivate(tid)
    await runsApi.autoDispatch(tid)
    ElMessage.success('已重新触发执行')
    await refreshLite()
    startPolling()   // 重跑后开始轮询，子任务状态实时刷新为「进行中」直至完成
  } catch (e) { ElMessage.error(e?.response?.data?.detail || '重跑失败') }
}
// 本地乐观置「进行中」：命中的子任务改 status，且若当前打开的就是父任务则父任务也即时翻牌
function optimisticReactivate(tid) {
  const s = subtasks.value.find((x) => x.id === tid)
  if (s && (s.status === 'done' || s.status === 'reviewing')) s.status = 'in_progress'
  if (task.value && (task.value.status === 'done' || task.value.status === 'reviewing')) {
    task.value.status = 'in_progress'
  }
}
// 执行日志区 run 状态点
async function onRunDot(r) {
  if (r.status === 'running') { await runsApi.kill(r.id); ElMessage.info('已发送终止信号') }
  else if (r.status === 'failed' || r.status === 'killed') { await rerunTask(r.task_id || taskId) }
}
async function onPauseAgent(r) {
  // 进度面板里的运行项：按 task_id 找其 running run 并 kill
  await pauseTask(r.task_id || taskId)
}
async function refreshLite() {
  await Promise.all([loadSubtasks(), loadRuns(), loadProgress()])
}

// 返回：子任务→回父任务；顶层任务→回工作区
function goBack() {
  if (task.value?.parent_task_id) openTask(task.value.parent_task_id)
  else router.push(`/projects/${pid}?tab=workspace`)
}

async function loadAll() {
  loading.value = true
  try {
    team.value = (await projectAgentsApi.list(pid)).agents
    // 用单任务接口取详情（顶层/子任务通用；看板 list 只含顶层任务，取不到子任务）
    task.value = await tasksApi.get(pid, taskId)
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
function openAddSub() {
  subForm.value = { title: '', assignee_slug: '', description: '', priority: 'none' }
  addSubVisible.value = true
}
async function doAddSub() {
  if (!subForm.value.title.trim()) return ElMessage.warning('请填写子任务标题')
  try {
    await tasksApi.createSubtask(pid, taskId, {
      title: subForm.value.title.trim(),
      assignee_slug: subForm.value.assignee_slug || '',
      description: subForm.value.description || '',
      priority: subForm.value.priority || 'none',
    })
    addSubVisible.value = false
    await Promise.all([loadSubtasks(), loadTimeline(), loadProgress()])
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e.message)
  }
}
function openWecomDialog() {
  // 预填正文：取时间线里最新一条 assistant 交付（找不到则留空，用户手动填）
  let latest = ''
  for (let i = timeline.value.length - 1; i >= 0; i--) {
    const it = timeline.value[i]
    if (it.kind === 'message' && it.role === 'assistant' && (it.content || '').trim()) {
      latest = it.content.trim()
      break
    }
  }
  wecomForm.value = { subtitle: '', body: latest }
  wecomVisible.value = true
}
async function doPushWecom() {
  wecomSending.value = true
  try {
    await tasksApi.pushWecom(taskId, {
      subtitle: wecomForm.value.subtitle || '',
      body: wecomForm.value.body || '',
    })
    wecomVisible.value = false
    ElMessage.success('已发送到企微群')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e.message)
  } finally {
    wecomSending.value = false
  }
}
function openTranscript(r) {
  transcriptRunId.value = r.id
  transcriptAgentName.value = agentDisplayBySlug(r.agent_slug)
  transcriptVisible.value = true
}
// 把流式 tool 事件转成 {text, detail, open}：text 是一行摘要，detail 是完整 input（点击展开）
function makeToolEvent(ev) {
  const inp = ev.tool_input || {}
  const name = ev.tool || '工具'
  const key = inp.command || inp.file_path || inp.path || inp.pattern || inp.query || inp.prompt || inp.description || inp.url
  let text = ev.text
  if (key) { const s = String(key).replace(/\s+/g, ' '); text = `${name}: ${s.length > 100 ? s.slice(0, 100) + '…' : s}` }
  const detail = Object.keys(inp).length ? JSON.stringify(inp, null, 2) : ''
  return { text, detail, open: false }
}
async function send() {
  if (!input.value.trim() || streaming.value) return
  mentionOpen.value = false
  // 解析被 @ 的成员：第一个作为流式主受理人，其余由后端解析入队（协同队列串行执行）
  const mentioned = parseMentions(input.value)
  // 主受理人：优先取第一个被 @ 的成员；都没 @ 则回退任务负责人（后端 assignee_slug 兜底）
  const primary = mentioned[0]?.slug || task.value?.assignee_slug || ''
  if (!primary) return ElMessage.warning('请用 @ 点名至少一位成员，或先为任务设置负责人')
  primarySlug.value = primary
  const prompt = input.value.trim()
  input.value = ''; streaming.value = true; streamText.value = ''; toolEvents.value = []; currentRunId.value = null
  if (mentioned.length > 1) {
    ElMessage.info(`已点名 ${mentioned.length} 位成员，其余成员将在协同队列中依次参与`)
  }
  await loadTimeline(); atBottom.value = true; scrollToBottom(true)
  try {
    await runsApi.dispatch(taskId, prompt, primary, (ev) => {
      if (ev.type === 'system' && ev.meta?.run_id) currentRunId.value = ev.meta.run_id
      else if (ev.type === 'text') { streamText.value += ev.text; scrollToBottom() }
      else if (ev.type === 'tool') { toolEvents.value.push(makeToolEvent(ev)); scrollToBottom() }
      else if (ev.type === 'tool_result') {
        const o = ev.tool_output || ''
        if (o.trim()) toolEvents.value.push({ text: `↳ ${ev.tool || '结果'}`, detail: o, open: false })
      }
      else if (ev.type === 'error') { ElMessage.error(ev.text); toolEvents.value.push({ text: '❌ ' + ev.text, detail: '', open: false }) }
    })
  } catch (e) { ElMessage.error('执行失败：' + e.message) }
  finally {
    streamText.value = ''; streaming.value = false; currentRunId.value = null
    await Promise.all([loadTimeline(), loadRuns(), loadProgress()]); scrollToBottom()
    // 额外 @ 的成员已入协同队列，开轮询让其执行进度即时刷新
    startPolling()
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

// 路由 taskId 变化（点击子任务/父任务面包屑跳转到另一张卡）→ 更新 id、停旧轮询、重新加载
watch(() => route.params.taskId, (nv) => {
  const nid = Number(nv)
  if (!nid || nid === taskId) return
  stopPolling()
  pid = Number(route.params.id)
  taskId = nid
  loadAll()
})

onMounted(loadAll)
onUnmounted(stopPolling)
</script>

<style scoped>
.task-detail { max-width: 1440px; margin: 0 auto; }
.td-topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.td-crumb { display: flex; align-items: center; gap: 8px; }
.td-actions { display: flex; align-items: center; gap: 8px; }
.wecom-hint { font-size: 12px; color: #909399; line-height: 1.5; margin-top: 4px; }
.crumb-sep { color: #c0c4cc; }
.crumb-title { font-weight: 600; color: #303133; }
.crumb-link { color: #409eff; cursor: pointer; }
.crumb-link:hover { text-decoration: underline; }
.td-body { display: flex; gap: 24px; align-items: flex-start; }
.td-main { flex: 1; min-width: 0; }
.td-side { width: 300px; flex-shrink: 0; }
.td-title { font-size: 24px; font-weight: 700; margin: 0 0 12px; }
.td-desc { background: #f5f7fa; border-radius: 8px; padding: 14px 16px; font-size: 14px;
  line-height: 1.7; color: #303133; margin-bottom: 20px; }
.td-desc :deep(.md-body) { font-size: 14px; }
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
.tl-actor-ic { width: 20px; text-align: center; flex-shrink: 0; font-size: 13px; }
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
/* 含 Markdown 渲染时由 MarkdownView 自行排版，取消父级 pre-wrap 以免多余空白 */
.chat-bubble:has(.md-body) { white-space: normal; }
/* 气泡内正文（含 Markdown）字号与气泡一致 14px；头像/昵称保持小 */
.chat-bubble :deep(.md-body) { font-size: 14px; }
.chat-row.mine .chat-bubble { background: #ecf3ff; }
.running { color: #e6a23c; margin-left: 6px; }
.tools { margin-top: 6px; }
.tool-item { margin-bottom: 2px; }
.tool-line { font-size: 12px; color: #909399; font-family: monospace; word-break: break-all; }
.tool-line.clickable { cursor: pointer; }
.tool-line.clickable:hover { color: #606266; }
.tool-caret { display: inline-block; width: 12px; color: #c0c4cc; }
.tool-detail { margin: 2px 0 6px 12px; padding: 8px 10px; background: #1e1e1e; color: #d4d4d4;
  border-radius: 6px; font-size: 11px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
  font-family: 'Consolas', monospace; max-height: 240px; overflow: auto; }
.td-composer { border-top: 1px solid #ebeef5; padding-top: 14px; margin-top: 18px; }
.composer-hint { display: flex; align-items: center; justify-content: space-between;
  font-size: 12.5px; color: #909399; margin-bottom: 8px; }
.composer-hint b { color: #409eff; }
.composer-input-wrap { position: relative; }
/* 输入框放大：更高、字号更舒展 */
.composer-input-wrap :deep(.el-textarea__inner) {
  font-size: 14.5px; line-height: 1.7; padding: 12px 14px; min-height: 132px;
  border-radius: 10px;
}
/* @ 补全浮层：悬浮在输入框上方 */
.mention-pop {
  position: absolute; left: 8px; bottom: calc(100% + 6px); z-index: 20;
  min-width: 240px; max-height: 300px; overflow-y: auto;
  background: #fff; border: 1px solid #e4e7ed; border-radius: 10px;
  box-shadow: 0 6px 24px rgba(15, 28, 51, .16); padding: 5px;
}
.mention-item { display: flex; align-items: center; gap: 9px; padding: 7px 10px;
  border-radius: 7px; cursor: pointer; }
.mention-item.active,
.mention-item:hover { background: #eef4ff; }
.mention-av { flex-shrink: 0; }
.mention-name { font-weight: 600; font-size: 14px; color: #24324d; }
.mention-badge { font-size: 12px; color: #b8860b; margin-left: auto; }
.mention-role { font-size: 12px; color: #909399; margin-left: auto;
  max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.composer-foot { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-top: 10px; }
.mentioned-chips { margin-right: auto; font-size: 12.5px; color: #909399; }
.mentioned-chips .mchip { margin-left: 5px; }
.readonly-hint { border-top: 1px solid #ebeef5; padding: 16px 0 4px; text-align: center; color: #909399; font-size: 13px; }
.side-block { background: #fafbfc; border: 1px solid #ebeef5; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
.side-block-title { font-size: 14px; font-weight: 600; color: #606266; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; }
.side-block-title .lineage-link { font-size: 12px; font-weight: 500; color: #409eff; cursor: pointer; }
.side-block-title .lineage-link:hover { text-decoration: underline; }
.side-row { display: flex; align-items: center; margin-bottom: 12px; }
.side-row:last-child { margin-bottom: 0; }
.side-label { width: 56px; font-size: 13px; color: #909399; flex-shrink: 0; }
.side-ctrl { flex: 1; }
.side-val { font-size: 13px; color: #303133; }
/* 历史运行折叠开关 */
.run-hist-toggle { display: flex; align-items: center; gap: 4px; font-size: 12px; color: #909399;
  cursor: pointer; padding: 6px 8px; border-radius: 6px; }
.run-hist-toggle:hover { background: #f5f7fa; color: #606266; }
.rh-caret { width: 12px; color: #c0c4cc; }
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
.owner-opt { display: flex; align-items: center; gap: 8px; }
.owner-opt-name { font-size: 14px; }
</style>


