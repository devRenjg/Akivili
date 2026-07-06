<template>
  <el-dialog v-model="visible" class="transcript-dialog" append-to-body
             :width="'90%'" top="4vh" :show-close="false" @open="onOpen">
    <template #header>
      <div class="tr-header">
        <div class="tr-title">
          <span class="tr-bot">🤖</span>
          <span class="tr-name">{{ agentName || meta.agent_slug || '执行日志' }}</span>
          <span class="tr-status" :class="`trs-${meta.status}`">{{ statusLabel(meta.status) }}</span>
        </div>
        <div class="tr-actions">
          <el-select v-if="toolOptions.length" v-model="selectedTools" multiple collapse-tags
                     collapse-tags-tooltip placeholder="筛选" size="small" class="tr-filter">
            <el-option v-for="o in toolOptions" :key="o.value" :value="o.value" :label="o.label" />
          </el-select>
          <el-button size="small" text @click="toggleSort">
            {{ sortDir === 'asc' ? '⬇ 时间正序' : '⬆ 最新在上' }}
          </el-button>
          <el-button size="small" text @click="copyAll">{{ copied ? '✓ 已复制' : '⧉ 复制全部' }}</el-button>
          <el-button size="small" text @click="visible = false">✕</el-button>
        </div>
      </div>
    </template>

    <div v-loading="loading" class="tr-body">
      <!-- 元数据 chips -->
      <div class="tr-meta">
        <span v-if="meta.provider_id" class="tr-chip">🧠 {{ meta.provider_id }}</span>
        <span v-if="duration" class="tr-chip">⏱ {{ duration }}</span>
        <span v-if="toolCount" class="tr-chip">🔧 {{ toolCount }} 次工具调用</span>
        <span class="tr-chip">
          {{ selectedTools.length ? `${filtered.length}/${items.length}` : items.length }} 条事件
        </span>
        <span v-if="meta.started_at" class="tr-chip">{{ shortTime(meta.started_at) }}</span>
      </div>

      <!-- 彩色 timeline 进度条 -->
      <div v-if="displayItems.length" class="tr-bar">
        <button v-for="(seg, i) in segments" :key="i" class="tr-seg" :class="`seg-${seg.color}`"
                :style="{ width: Math.max(seg.pct, 0.5) + '%' }"
                :title="`${seg.label}${seg.count > 1 ? ' +' + (seg.count - 1) : ''}`"
                @click="scrollTo(seg.seq)" />
      </div>

      <!-- 事件列表 -->
      <div class="tr-list">
        <div v-for="it in displayItems" :key="it.seq" :ref="(el) => setRef(it.seq, el)"
             class="tr-row" :class="{ sel: selectedSeq === it.seq }">
          <div class="tr-row-head" :class="{ clickable: hasDetail(it) }" @click="hasDetail(it) && toggle(it.seq)">
            <span class="tr-badge" :class="`badge-${colorOf(it)}`">
              <span v-if="hasDetail(it)" class="tr-caret">{{ open[it.seq] ? '▾' : '▸' }}</span>
              {{ labelOf(it) }}
            </span>
            <span class="tr-summary" :class="{ err: it.channel === 'stderr' }">{{ summaryOf(it) || '（空）' }}</span>
            <span class="tr-seq">#{{ it.seq }}</span>
          </div>
          <div v-if="open[it.seq] && hasDetail(it)" class="tr-detail">
            <pre class="tr-pre" :class="{ err: it.channel === 'stderr' }">{{ detailOf(it) }}</pre>
          </div>
        </div>
        <el-empty v-if="!loading && displayItems.length === 0" :image-size="50" description="暂无日志" />
      </div>
    </div>
  </el-dialog>
</template>

<script setup>
import { ref, computed, reactive } from 'vue'
import { runsApi } from '../api'
import { redactSecrets } from '../utils/redact'

const props = defineProps({
  modelValue: Boolean,
  runId: { type: [Number, null], default: null },
  agentName: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const loading = ref(false)
const meta = ref({})
const items = ref([])
const open = reactive({})
const selectedSeq = ref(null)
const selectedTools = ref([])
const sortDir = ref('asc')
const copied = ref(false)
const rowRefs = new Map()

const STATUS_LABEL = { running: '⚙️ 执行中', succeeded: '✓ 完成', failed: '✗ 失败', killed: '■ 终止' }
function statusLabel(s) { return STATUS_LABEL[s] || s || '' }
function shortTime(t) { return (t || '').slice(5, 16) }

async function onOpen() {
  if (!props.runId) return
  loading.value = true
  items.value = []
  meta.value = {}
  Object.keys(open).forEach((k) => delete open[k])
  try {
    const data = await runsApi.transcript(props.runId)
    meta.value = data.meta || {}
    items.value = data.items || []
  } catch { /* ignore */ } finally { loading.value = false }
}

function setRef(seq, el) { if (el) rowRefs.set(seq, el); else rowRefs.delete(seq) }
function toggle(seq) { open[seq] = !open[seq] }
function toggleSort() { sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc' }

// ── 事件分类 → 颜色 / 标签 ──
function colorOf(it) {
  if (it.channel === 'tool') return 'tool'
  if (it.channel === 'tool_result') return 'result'
  if (it.channel === 'thinking') return 'thinking'
  if (it.channel === 'stderr') return 'error'
  if (it.channel === 'system') return 'system'
  return 'agent'
}
function labelOf(it) {
  if (it.channel === 'tool') return it.tool || '工具'
  if (it.channel === 'tool_result') return it.tool || '结果'
  if (it.channel === 'thinking') return '思考'
  if (it.channel === 'stderr') return '错误'
  if (it.channel === 'system') return '系统'
  return '发言'
}
function summaryOf(it) {
  if (it.channel === 'tool') {
    const inp = it.tool_input || {}
    const key = inp.command || inp.file_path || inp.path || inp.pattern || inp.query || inp.prompt || inp.description || inp.url
    if (key) { const s = String(key).replace(/\s+/g, ' '); return s.length > 140 ? s.slice(0, 140) + '…' : s }
    return it.content || ''
  }
  if (it.channel === 'tool_result') { const o = it.tool_output || ''; return o.slice(0, 140) }
  const c = it.content || ''
  const firstLine = c.split('\n').find((l) => l.trim()) || c
  return firstLine.length > 140 ? firstLine.slice(0, 140) + '…' : firstLine
}
function hasDetail(it) {
  if (it.channel === 'tool') return it.tool_input && Object.keys(it.tool_input).length > 0
  if (it.channel === 'tool_result') return !!it.tool_output
  return (it.content || '').length > 0
}
function detailOf(it) {
  if (it.channel === 'tool') return redactSecrets(JSON.stringify(it.tool_input || {}, null, 2))
  if (it.channel === 'tool_result') {
    const o = it.tool_output || ''
    return o.length > 8000 ? redactSecrets(o.slice(0, 8000)) + '\n… (已截断)' : redactSecrets(o)
  }
  return redactSecrets(it.content || '')
}

// ── 筛选 / 排序 ──
const toolOptions = computed(() => {
  const seen = new Map()
  for (const it of items.value) {
    if (it.channel === 'tool' || it.channel === 'tool_result') {
      const v = `tool:${it.tool || ''}`
      if (!seen.has(v)) seen.set(v, `${labelOf(it)}`)
    } else if (!seen.has(it.channel)) {
      seen.set(it.channel, labelOf(it))
    }
  }
  return Array.from(seen.entries()).map(([value, label]) => ({ value, label }))
})
function keyOf(it) {
  return (it.channel === 'tool' || it.channel === 'tool_result') ? `tool:${it.tool || ''}` : it.channel
}
const filtered = computed(() => {
  if (!selectedTools.value.length) return items.value
  const set = new Set(selectedTools.value)
  return items.value.filter((it) => set.has(keyOf(it)))
})
const displayItems = computed(() =>
  sortDir.value === 'desc' ? [...filtered.value].reverse() : filtered.value)

const toolCount = computed(() => items.value.filter((i) => i.channel === 'tool').length)
const duration = computed(() => {
  const { started_at: s, ended_at: e } = meta.value
  if (!s || !e) return ''
  const ms = new Date(e.replace(' ', 'T')) - new Date(s.replace(' ', 'T'))
  if (isNaN(ms) || ms < 0) return ''
  const sec = Math.floor(ms / 1000)
  return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m ${sec % 60}s`
})

// ── 彩色进度条分段（相邻同色合并）──
const segments = computed(() => {
  const arr = displayItems.value
  const segs = []
  let cur = null
  arr.forEach((it) => {
    const color = colorOf(it)
    if (!cur || cur.color !== color) {
      cur = { color, count: 1, seq: it.seq, label: labelOf(it) }
      segs.push(cur)
    } else cur.count += 1
  })
  const total = arr.length || 1
  segs.forEach((s) => { s.pct = (s.count / total) * 100 })
  return segs
})

function scrollTo(seq) {
  selectedSeq.value = seq
  rowRefs.get(seq)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function copyAll() {
  const text = displayItems.value.map((it) => `[${labelOf(it)}] ${summaryOf(it)}`).join('\n')
  navigator.clipboard?.writeText(text).then(() => {
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  })
}
</script>

<style scoped>
/* 明亮主题弹窗：头部浅灰底、深色文字，保证标题栏清晰可读 */
.transcript-dialog :deep(.el-dialog) { border-radius: 12px; overflow: hidden; }
.transcript-dialog :deep(.el-dialog__header) {
  margin: 0; padding: 14px 20px; background: #f5f7fa; border-bottom: 1px solid #e4e7ed;
}
.transcript-dialog :deep(.el-dialog__body) { padding: 16px 20px; }
.tr-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.tr-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
.tr-bot { font-size: 18px; }
.tr-name { font-weight: 700; font-size: 16px; color: #1d2129; }
.tr-status { font-size: 12px; font-weight: 600; padding: 2px 10px; border-radius: 10px;
  background: #e9ebf0; color: #4e5969; }
.trs-running { color: #e6a23c; background: #fdf6ec; }
.trs-succeeded { color: #67c23a; background: #f0f9eb; }
.trs-failed { color: #f56c6c; background: #fef0f0; }
.trs-killed { color: #909399; }
.tr-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.tr-actions :deep(.el-button) { color: #4e5969; }
.tr-actions :deep(.el-button:hover) { color: #2563eb; }
.tr-filter { width: 160px; }
.tr-body { height: 78vh; display: flex; flex-direction: column; }
.tr-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.tr-chip { font-size: 12px; color: #4e5969; background: #f0f2f5; border: 1px solid #e4e7ed;
  border-radius: 6px; padding: 2px 9px; }
.tr-bar { display: flex; gap: 2px; height: 18px; border-radius: 4px; overflow: hidden; margin-bottom: 12px; }
.tr-seg { height: 100%; border: none; cursor: pointer; min-width: 3px; transition: opacity .15s; }
.tr-seg:hover { opacity: .75; }
.seg-agent { background: #95d475; } .seg-thinking { background: #b794f4; }
.seg-tool { background: #79bbff; } .seg-result { background: #c8c9cc; }
.seg-error { background: #f89898; } .seg-system { background: #e6c07b; }
.tr-list { flex: 1; overflow-y: auto; border: 1px solid #ebeef5; border-radius: 8px; }
.tr-row { border-bottom: 1px solid #f5f5f5; }
.tr-row.sel { background: #ecf5ff; }
.tr-row-head { display: flex; align-items: flex-start; gap: 8px; padding: 8px 12px; }
.tr-row-head.clickable { cursor: pointer; }
.tr-row-head.clickable:hover { background: #f5f7fa; }
.tr-badge { flex-shrink: 0; min-width: 64px; text-align: center; font-size: 11px; font-weight: 600;
  padding: 2px 6px; border-radius: 4px; margin-top: 1px; }
.tr-caret { color: #c0c4cc; margin-right: 2px; }
.badge-agent { background: #f0f9eb; color: #529b2e; }
.badge-thinking { background: #f3e8ff; color: #7c3aed; }
.badge-tool { background: #ecf5ff; color: #2563eb; }
.badge-result { background: #f4f4f5; color: #909399; }
.badge-error { background: #fef0f0; color: #f56c6c; }
.badge-system { background: #fdf6ec; color: #b88230; }
.tr-summary { flex: 1; font-size: 12.5px; color: #4e5969; min-width: 0; word-break: break-word;
  font-family: 'Consolas', monospace; line-height: 1.5; }
.tr-summary.err { color: #f56c6c; }
.tr-seq { flex-shrink: 0; font-size: 10px; color: #c0c4cc; margin-top: 2px; }
.tr-detail { padding: 0 12px 10px 84px; }
.tr-pre { max-height: 320px; overflow: auto; margin: 0; padding: 10px 12px; background: #1e1e1e;
  color: #d4d4d4; border-radius: 6px; font-size: 11px; line-height: 1.55; white-space: pre-wrap;
  word-break: break-all; font-family: 'Consolas', monospace; }
.tr-pre.err { color: #f48771; }
</style>
