<template>
  <div class="runtime">
    <div class="header">
      <h2>运行时 · 链路可观测</h2>
      <el-button :icon="Refresh" text @click="reload" :loading="loading" v-if="taskId">刷新</el-button>
    </div>

    <el-alert type="info" :closable="false" class="tip"
      title="端到端追一条任务的完整执行链：负责人派活 → 建子任务 → 成员执行 → 汇报 → 收尾。每个 run 的排队/执行/重试/失败流水、耗时、因果来源一次拼出，替代跨表人肉拼时间线。" />

    <div class="toolbar">
      <el-select v-model="projectId" placeholder="选择项目" class="sel" filterable @change="onProject">
        <el-option v-for="p in projects" :key="p.id" :label="p.title" :value="p.id" />
      </el-select>
      <el-select v-model="taskId" placeholder="选择任务（含其子任务链）" class="sel sel-task"
                 filterable clearable :disabled="!projectId" @change="loadLineage">
        <el-option v-for="t in topTasks" :key="t.id" :label="`#${t.id} ${t.title}`" :value="t.id" />
      </el-select>
    </div>

    <div v-if="!taskId" class="placeholder">
      <el-empty description="选择一个任务，查看其端到端执行链路" />
    </div>

    <div v-else v-loading="loading" class="board">
      <!-- 汇总条 -->
      <div class="summary" v-if="data">
        <div class="stat"><span class="num">{{ data.task_count }}</span><span class="lbl">关联任务</span></div>
        <div class="stat"><span class="num">{{ data.run_count }}</span><span class="lbl">执行 run</span></div>
        <div class="stat"><span class="num">{{ fmtDur(data.total_run_seconds) }}</span><span class="lbl">链路总耗时</span></div>
        <div class="stat" :class="{ bad: data.failed_runs.length }">
          <span class="num">{{ data.failed_runs.length }}</span><span class="lbl">失败 run</span>
        </div>
      </div>

      <!-- 时间线 -->
      <div class="timeline" v-if="data && data.chain.length">
        <div v-for="(r, i) in data.chain" :key="r.run_queue_id" class="tl-item">
          <div class="tl-rail">
            <span class="tl-dot" :class="dotClass(r)"></span>
            <span v-if="i < data.chain.length - 1" class="tl-line"></span>
          </div>
          <div class="tl-body">
            <div class="tl-head" @click="toggle(r.run_queue_id)">
              <span class="caret">{{ open[r.run_queue_id] ? '▾' : '▸' }}</span>
              <el-tag v-if="r.is_leader" size="small" type="warning" effect="dark" class="chip">负责人</el-tag>
              <span class="slug">{{ r.agent_slug || '—' }}</span>
              <el-tag size="small" effect="plain" class="chip">{{ triggerCn(r.trigger) }}</el-tag>
              <el-tag size="small" :type="statusType(r)" effect="light" class="chip">{{ statusCn(r) }}</el-tag>
              <el-tag v-if="r.fail_reason" size="small" type="danger" effect="plain" class="chip">{{ failCn(r.fail_reason) }}</el-tag>
              <span class="spacer"></span>
              <span v-if="r.attempts > 1" class="attempts" title="尝试次数">↻ {{ r.attempts }}</span>
              <span class="dur" v-if="r.duration_seconds != null">{{ fmtDur(r.duration_seconds) }}</span>
              <span class="ts">{{ r.enqueued_at }}</span>
            </div>
            <div v-if="open[r.run_queue_id]" class="tl-detail">
              <div class="meta-row">
                <span class="k">task_id</span><span class="v">#{{ r.task_id }}</span>
                <span class="k">run_queue</span><span class="v">#{{ r.run_queue_id }}</span>
                <span class="k">task_run</span>
                <span class="v">
                  <template v-if="r.task_run_id">
                    <a class="lnk" @click="openTranscript(r.task_run_id)">#{{ r.task_run_id }} 查看执行记录</a>
                  </template>
                  <template v-else>—（未产生执行）</template>
                </span>
              </div>
              <div class="meta-row" v-if="r.source_run_id || r.source_message_id">
                <span class="k">因果源</span>
                <span class="v">由 run #{{ r.source_run_id || '?' }} 的发言（msg #{{ r.source_message_id || '?' }}）@ 触发</span>
              </div>
              <div class="meta-row times" v-if="r.started_at || r.ended_at">
                <span class="k">执行窗口</span>
                <span class="v">{{ r.started_at || '—' }} → {{ r.ended_at || '（未结束）' }}</span>
              </div>
              <!-- run_events 调度流水 -->
              <div v-if="r.events.length" class="events">
                <div class="events-title">调度流水</div>
                <div v-for="(e, ei) in r.events" :key="ei" class="ev-row">
                  <span class="ev-dot" :class="e.event"></span>
                  <span class="ev-name">{{ eventCn(e.event) }}</span>
                  <span class="ev-detail" v-if="e.detail && e.detail !== '{}'">{{ e.detail }}</span>
                  <span class="ev-ts">{{ e.ts }}</span>
                </div>
              </div>
              <div v-else class="events-empty">无调度流水（该 run 早于埋点上线，或未入队走此路径）</div>
            </div>
          </div>
        </div>
      </div>
      <el-empty v-else-if="data" description="该任务尚无执行 run" />
    </div>

    <RunTranscriptDialog v-model="transcriptVisible" :run-id="transcriptRunId" />
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { projectsApi, tasksApi } from '../api'
import RunTranscriptDialog from '../components/RunTranscriptDialog.vue'

const route = useRoute()
const router = useRouter()

const projects = ref([])
const projectId = ref(null)
const topTasks = ref([])
const taskId = ref(null)
const data = ref(null)
const loading = ref(false)
const open = reactive({})

const transcriptVisible = ref(false)
const transcriptRunId = ref(null)

const TRIGGER_CN = { assign: '指派', mention: '@提及', auto: '自动调度', leader: '负责人', collaborate: '协同' }
const STATUS_CN = { running: '执行中', succeeded: '已完成', failed: '失败', killed: '已终止', queued: '排队中', done: '已完成' }
const FAIL_CN = {
  timeout_idle: '静默超时', timeout_wall: '硬墙钟超时', exception: '执行抛错',
  error_no_output: '有报错无产出', task_or_agent_missing: '任务/成员缺失',
}
const EVENT_CN = { enqueued: '入队', claimed: '领取', retry: '重试', done: '完成', failed: '失败' }

function triggerCn(t) { return TRIGGER_CN[t] || t || '—' }
function failCn(f) { return FAIL_CN[f] || f }
function eventCn(e) { return EVENT_CN[e] || e }

// run 的展示态：优先用实际执行态，回落队列态
function effStatus(r) { return r.run_status || r.queue_status }
function statusCn(r) { const s = effStatus(r); return STATUS_CN[s] || s || '—' }
function statusType(r) {
  const s = effStatus(r)
  if (s === 'failed' || s === 'killed') return 'danger'
  if (s === 'running' || s === 'queued') return 'warning'
  return 'success'
}
function dotClass(r) {
  const s = effStatus(r)
  if (s === 'failed' || s === 'killed') return 'bad'
  if (s === 'running' || s === 'queued') return 'live'
  return 'ok'
}

function fmtDur(sec) {
  if (sec == null) return '—'
  if (sec < 1) return '<1s'
  if (sec < 60) return `${Math.round(sec)}s`
  const m = Math.floor(sec / 60), s = Math.round(sec % 60)
  if (m < 60) return `${m}m${s ? ' ' + s + 's' : ''}`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

function toggle(id) { open[id] = !open[id] }
function openTranscript(runId) { transcriptRunId.value = runId; transcriptVisible.value = true }

async function loadProjects() {
  try { projects.value = (await projectsApi.list()).projects } catch { projects.value = [] }
}

async function onProject() {
  taskId.value = null
  data.value = null
  await loadTopTasks()
  syncQuery()
}

async function loadTopTasks() {
  topTasks.value = []
  if (!projectId.value) return
  try {
    const r = await tasksApi.list(projectId.value)
    // 只列顶层任务（父任务），子任务自动含在链路里
    topTasks.value = (r.tasks || []).filter((t) => !t.parent_task_id)
  } catch { topTasks.value = [] }
}

async function loadLineage() {
  data.value = null
  Object.keys(open).forEach((k) => delete open[k])
  if (!taskId.value) { syncQuery(); return }
  loading.value = true
  try {
    data.value = await tasksApi.lineage(taskId.value)
  } catch (e) {
    data.value = null
  } finally {
    loading.value = false
  }
  syncQuery()
}

async function reload() {
  await loadTopTasks()
  await loadLineage()
}

// URL 同步：便于从任务详情页深链跳入、或分享某条链路
function syncQuery() {
  const q = {}
  if (projectId.value) q.project = projectId.value
  if (taskId.value) q.task = taskId.value
  router.replace({ path: '/runtime', query: q })
}

onMounted(async () => {
  await loadProjects()
  const qp = route.query.project ? Number(route.query.project) : null
  const qt = route.query.task ? Number(route.query.task) : null
  if (qp && projects.value.some((p) => p.id === qp)) {
    projectId.value = qp
    await loadTopTasks()
    if (qt && topTasks.value.some((t) => t.id === qt)) {
      taskId.value = qt
      await loadLineage()
    }
  }
})
</script>

<style scoped>
.runtime { width: 100%; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.tip { margin-bottom: 16px; }
.toolbar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
.sel { width: 240px; }
.sel-task { width: 380px; }
.placeholder { margin-top: 40px; }

/* 汇总条 */
.summary { display: flex; gap: 14px; margin-bottom: 20px; }
.stat {
  flex: 1; background: #f7f8fa; border-radius: 10px; padding: 14px 16px;
  display: flex; flex-direction: column; gap: 4px;
}
.stat .num { font-size: 22px; font-weight: 700; color: #303133; line-height: 1.1; }
.stat .lbl { font-size: 12px; color: #909399; }
.stat.bad .num { color: #f56c6c; }

/* 时间线 */
.timeline { padding-left: 4px; }
.tl-item { display: flex; gap: 12px; }
.tl-rail { display: flex; flex-direction: column; align-items: center; flex-shrink: 0; width: 16px; }
.tl-dot {
  width: 12px; height: 12px; border-radius: 50%; margin-top: 6px;
  border: 2px solid #fff; box-shadow: 0 0 0 1.5px #dcdfe6;
}
.tl-dot.ok { background: #67c23a; box-shadow: 0 0 0 1.5px #67c23a; }
.tl-dot.bad { background: #f56c6c; box-shadow: 0 0 0 1.5px #f56c6c; }
.tl-dot.live { background: #e6a23c; box-shadow: 0 0 0 1.5px #e6a23c; }
.tl-line { flex: 1; width: 2px; background: #ebeef5; margin: 4px 0; min-height: 12px; }
.tl-body { flex: 1; min-width: 0; padding-bottom: 14px; }

.tl-head {
  display: flex; align-items: center; gap: 8px; cursor: pointer;
  padding: 8px 10px; border-radius: 8px; background: #fff;
  border: 1px solid #ebeef5; transition: background .15s;
}
.tl-head:hover { background: #fafbfc; }
.tl-head .caret { color: #c0c4cc; font-size: 12px; flex-shrink: 0; }
.tl-head .slug { font-weight: 600; font-size: 14px; color: #303133; }
.tl-head .chip { flex-shrink: 0; }
.tl-head .spacer { flex: 1; }
.tl-head .attempts { font-size: 12px; color: #e6a23c; flex-shrink: 0; }
.tl-head .dur { font-size: 12px; color: #606266; font-variant-numeric: tabular-nums; flex-shrink: 0; }
.tl-head .ts { font-size: 11px; color: #c0c4cc; flex-shrink: 0; }

.tl-detail {
  margin: 6px 0 0 20px; padding: 12px 14px;
  background: #f8f9fb; border-radius: 8px; font-size: 12px;
}
.meta-row { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 10px; margin-bottom: 8px; }
.meta-row .k { color: #909399; flex-shrink: 0; }
.meta-row .v { color: #303133; margin-right: 12px; }
.meta-row.times .v { font-variant-numeric: tabular-nums; }
.lnk { color: #409eff; cursor: pointer; }
.lnk:hover { text-decoration: underline; }

.events { margin-top: 4px; border-top: 1px dashed #e4e7ed; padding-top: 10px; }
.events-title { color: #909399; font-weight: 600; margin-bottom: 6px; }
.ev-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
.ev-dot { width: 6px; height: 6px; border-radius: 50%; background: #c0c4cc; flex-shrink: 0; }
.ev-dot.enqueued { background: #909399; }
.ev-dot.claimed { background: #409eff; }
.ev-dot.retry { background: #e6a23c; }
.ev-dot.done { background: #67c23a; }
.ev-dot.failed { background: #f56c6c; }
.ev-name { color: #606266; min-width: 48px; flex-shrink: 0; }
.ev-detail { color: #97a0af; font-family: monospace; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ev-ts { color: #c0c4cc; font-size: 11px; margin-left: auto; flex-shrink: 0; }
.events-empty { color: #c0c4cc; margin-top: 4px; }
</style>


