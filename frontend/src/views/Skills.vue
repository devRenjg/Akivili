<template>
  <div class="skills">
    <div class="header">
      <h2>Skills 库</h2>
      <div v-if="isAdmin">
        <el-button :icon="Plus" @click="openCreate">新建 Skill</el-button>
        <el-button :icon="Refresh" @click="rescan" :loading="scanning">重新扫描</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" class="tip"
      title="Skill 是一段能力说明 / 规范 / 操作要领（纯文本，存于 skills/<slug>.md）。Agent 可在项目里勾选启用，运行时注入其能力上下文。可直接往 skills 目录丢 .md，或在此新建。" />

    <div class="toolbar">
      <el-input v-model="keyword" placeholder="搜索 Skill…" clearable class="search"
                :prefix-icon="Search" @input="onSearch" />
      <span class="total">共 {{ count }} 个</span>
    </div>

    <div v-loading="loading" class="grid">
      <el-card v-for="s in list" :key="s.id" class="skill-card" shadow="hover" @click="openDetail(s.id)">
        <div class="sc-head">
          <span class="emoji">{{ s.is_dir ? '📦' : '✦' }}</span>
          <span class="name">{{ s.name }}</span>
          <el-tag v-if="s.is_dir" size="small" type="success" effect="plain">能力包</el-tag>
        </div>
        <div class="desc">{{ s.description || '（无描述）' }}</div>
        <div class="sc-foot">
          <el-tag size="small" effect="plain">{{ s.slug }}</el-tag>
          <span class="dl-count">⬇ {{ s.download_count || 0 }}</span>
          <el-button v-if="s.downloadable !== 0" text size="small" :icon="Download" @click.stop="doDownload(s)">
            下载{{ s.is_dir ? ' zip' : ' .md' }}
          </el-button>
          <el-tag v-else size="small" type="info" effect="plain" title="该能力仅供 Agent 集成，不提供下载">🔒 仅集成</el-tag>
        </div>
      </el-card>
      <el-empty v-if="!loading && list.length === 0" description="暂无 Skill" />
    </div>

    <el-drawer v-model="detailVisible" :title="detail?.name || ''" size="50%">
      <div v-if="detail" class="detail">
        <div class="detail-top">
          <div class="detail-top-left">
            <el-tag size="small" effect="plain" class="slug-tag">{{ detail.slug }}</el-tag>
            <el-tag v-if="detail.is_dir" size="small" type="success" effect="plain">📦 能力包（含脚本/参考文件）</el-tag>
          </div>
          <el-button v-if="detail.downloadable !== 0" text size="small" :icon="Download" @click="doDownload(detail)">
            下载{{ detail.is_dir ? ' zip' : ' .md' }}
          </el-button>
          <el-tag v-else size="small" type="info" effect="plain" title="该能力仅供 Agent 集成，不提供下载">🔒 仅供 Agent 集成</el-tag>
        </div>
        <p class="detail-desc">{{ detail.description }}</p>
        <div v-if="isAdmin" class="dl-logs">
          <div class="dl-logs-head">
            <span>下载记录（共 {{ dlTotal }} 次）</span>
            <el-button text size="small" @click="loadLogs">刷新</el-button>
          </div>
          <div v-if="dlLogs.length" class="dl-log-list">
            <div v-for="(l, i) in dlLogs" :key="i" class="dl-log-row">
              <span class="dl-ip">{{ l.ip || '未知IP' }}</span>
              <span class="dl-ts">{{ l.ts }}</span>
            </div>
          </div>
          <div v-else class="dl-empty">还没有下载记录</div>
        </div>
        <el-alert v-if="detail.is_dir" type="info" :closable="false" class="dir-tip"
          title="这是一个目录型能力包：下载的 zip 含 SKILL.md 与配套 scripts / references，解压后即完整可用。下方为 SKILL.md 正文。" />
        <el-divider>{{ detail.is_dir ? 'SKILL.md 正文' : '能力指令' }}</el-divider>
        <pre class="body">{{ detail.body }}</pre>
      </div>
    </el-drawer>

    <el-dialog v-model="createVisible" title="✦ 新建 Skill" width="600px">
      <el-form label-width="80px">
        <el-form-item label="slug" required>
          <el-input v-model="form.slug" placeholder="英文标识，如 code-review-checklist" />
        </el-form-item>
        <el-form-item label="名称" required><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="form.description" /></el-form-item>
        <el-form-item label="能力正文">
          <el-input v-model="form.body" type="textarea" :rows="10"
                    placeholder="描述这个能力的说明、规范、操作要领…" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" @click="doCreate" :loading="creating">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, inject } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Refresh, Search, Download } from '@element-plus/icons-vue'
import { skillsApi } from '../api'

const isAdmin = inject('isAdmin')
const list = ref([])
const count = ref(0)
const keyword = ref('')
const loading = ref(false)
const scanning = ref(false)
const detailVisible = ref(false)
const detail = ref(null)
const dlLogs = ref([])
const dlTotal = ref(0)
const createVisible = ref(false)
const creating = ref(false)
const form = ref({ slug: '', name: '', description: '', body: '' })
let searchTimer = null

async function load() {
  loading.value = true
  try {
    const data = await skillsApi.list({ q: keyword.value })
    list.value = data.skills
    count.value = data.count
  } finally {
    loading.value = false
  }
}

function onSearch() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(load, 250)
}

async function openDetail(id) {
  detail.value = await skillsApi.detail(id)
  detailVisible.value = true
  dlLogs.value = []; dlTotal.value = 0
  if (isAdmin.value) loadLogs()
}
async function loadLogs() {
  if (!detail.value) return
  try {
    const r = await skillsApi.downloadLogs(detail.value.id)
    dlLogs.value = r.logs; dlTotal.value = r.total
  } catch { dlLogs.value = []; dlTotal.value = 0 }
}

// 下载走后端：目录型下 zip、单文件下 .md（后端已处理打包）
function doDownload(s) {
  const a = document.createElement('a')
  a.href = skillsApi.downloadUrl(s.id)
  a.click()
}

async function rescan() {
  scanning.value = true
  try {
    const r = await skillsApi.rescan()
    ElMessage.success(`扫描完成：新增 ${r.inserted}，更新 ${r.updated}`)
    await load()
  } finally {
    scanning.value = false
  }
}

function openCreate() {
  form.value = { slug: '', name: '', description: '', body: '' }
  createVisible.value = true
}

async function doCreate() {
  if (!form.value.slug.trim() || !form.value.name.trim()) {
    return ElMessage.warning('slug 与名称必填')
  }
  creating.value = true
  try {
    await skillsApi.create(form.value)
    ElMessage.success('已创建')
    createVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e.message)
  } finally {
    creating.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.skills { width: 100%; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.tip { margin-bottom: 16px; }
.toolbar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
.search { width: 320px; }
.total { color: #909399; font-size: 13px; margin-left: auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }
.skill-card { cursor: pointer; }
.sc-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.sc-head .emoji { font-size: 18px; color: #ffb02e; }
.sc-head .name { font-weight: 600; font-size: 15px; }
.desc {
  color: #606266; font-size: 13px; line-height: 1.5; margin-bottom: 10px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.sc-foot { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
.dl-count { font-size: 12px; color: #909399; margin-left: auto; }
.dl-logs { margin: 14px 0; padding: 12px 14px; background: #f7f8fa; border-radius: 8px; }
.dl-logs-head { display: flex; align-items: center; justify-content: space-between; font-size: 13px; font-weight: 600; color: #606266; margin-bottom: 8px; }
.dl-log-list { max-height: 180px; overflow-y: auto; }
.dl-log-row { display: flex; justify-content: space-between; font-size: 12px; color: #5e6c84; padding: 4px 0; border-bottom: 1px solid #eef0f3; }
.dl-ip { font-family: monospace; }
.dl-ts { color: #97a0af; }
.dl-empty { font-size: 12px; color: #c0c4cc; }
.detail-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.detail-top-left { display: flex; align-items: center; gap: 8px; }
.detail-desc { color: #606266; line-height: 1.6; }
.slug-tag { flex-shrink: 0; }
.body {
  white-space: pre-wrap; word-break: break-word; font-family: inherit;
  font-size: 13px; line-height: 1.7; color: #303133;
  background: #f8f9fb; padding: 16px; border-radius: 6px; margin-top: 8px;
}
</style>

