<template>
  <div class="agents">
    <div class="header">
      <h2>数字人才库</h2>
      <div class="header-actions">
        <el-button v-if="isAdmin" type="primary" :icon="Plus" @click="createVisible = true">新增人才</el-button>
      </div>
    </div>

    <div class="toolbar">
      <el-input v-model="keyword" placeholder="搜索名称或描述…" clearable class="search"
                :prefix-icon="Search" @input="onSearch" />
      <el-select v-model="division" placeholder="全部分类" clearable class="div-select" @change="load">
        <el-option v-for="d in divisions" :key="d.division"
                   :label="`${d.division || '其他'} (${d.n})`" :value="d.division" />
      </el-select>
      <template v-if="isAdmin && division">
        <el-button size="small" text :icon="Edit" @click="renameDivision">改名</el-button>
        <el-button size="small" text type="danger" :icon="Delete" @click="deleteDivision">删除分类</el-button>
      </template>
      <span class="total">共 {{ count }} 个</span>
    </div>

    <div v-loading="loading" class="grid">
      <el-card v-for="t in templates" :key="t.id" class="agent-card" shadow="hover"
               @click="openDetail(t.id)">
        <div class="card-head">
          <AgentAvatar :agent="t" :size="40" />
          <span class="name">{{ dName(t) }}</span>
        </div>
        <div class="desc">{{ t.description }}</div>
        <div class="card-foot">
          <el-tag size="small" effect="plain">{{ t.division || '其他' }}</el-tag>
          <span v-if="t.project_count > 0" class="proj-count">🗂 {{ t.project_count }} 个项目</span>
          <span class="solved-count" :title="'已完成任务数'">✅ {{ t.solved_tasks || 0 }} 个任务</span>
        </div>
      </el-card>
      <el-empty v-if="!loading && templates.length === 0" description="没有匹配的 Agent" />
    </div>

    <el-drawer v-model="detailVisible" :title="detail?.name || ''" size="50%">
      <div v-if="detail" class="detail">
        <div class="detail-head">
          <AgentAvatar :agent="detail" :size="60" />
          <div>
            <div class="detail-name">{{ dName(detail) }}</div>
            <el-select v-if="isAdmin" v-model="detailDivision" size="small" filterable allow-create
                       clearable default-first-option placeholder="设置分类" class="detail-div-select"
                       @change="changeTalentDivision">
              <el-option v-for="d in divisions" :key="d.division"
                         :label="`${d.division || '其他'} (${d.n})`" :value="d.division" />
            </el-select>
            <el-tag v-else size="small">{{ detail.division || '其他' }}</el-tag>
          </div>
          <el-button v-if="isAdmin" size="small" style="margin-left:auto" @click="openProfile(detail)">
            编辑资料
          </el-button>
        </div>
        <p class="detail-desc">{{ detail.description }}</p>

        <div class="proj-section">
          <div class="proj-label">已加入的项目（{{ joinedProjects.length }}）</div>
          <div v-if="joinedProjects.length" class="proj-tags">
            <el-tag v-for="p in joinedProjects" :key="p.id" type="success" effect="plain"
                    class="joined-tag" @click="gotoWorkspace(p.id)">{{ p.title }} →</el-tag>
          </div>
          <div v-else class="proj-empty">还没加入任何项目</div>
        </div>

        <div v-if="isAdmin" class="join-bar">
          <el-select v-model="joinProjectId" placeholder="邀请加入新项目" class="join-select"
                     :no-data-text="joinableProjects.length ? '' : '没有可加入的新项目（已全部加入或还没建项目）'">
            <el-option v-for="p in joinableProjects" :key="p.id" :label="p.title" :value="p.id" />
          </el-select>
          <el-button type="primary" :icon="Plus" :disabled="!joinProjectId" :loading="joining"
                     @click="joinProject">邀请加入</el-button>
        </div>

        <div class="skill-section">
          <div class="proj-label">集成的 Skills（{{ (detail.skills || []).length }}）</div>
          <div v-if="detail.skills && detail.skills.length" class="skill-tags">
            <el-tag v-for="s in detail.skills" :key="s.slug" type="primary" effect="plain"
                    class="skill-tag" :title="s.description">✦ {{ s.name }}</el-tag>
          </div>
          <div v-else class="proj-empty">未集成任何 Skill</div>
        </div>

        <el-divider>人格定义</el-divider>
        <pre class="body">{{ detail.body }}</pre>
      </div>
    </el-drawer>

    <AgentProfileDialog v-if="profileAgent" v-model="profileVisible" :agent="profileAgent"
                        @saved="onProfileSaved" />
    <CreateTalentDialog v-model="createVisible" @created="onTalentCreated" />
  </div>
</template>

<script setup>
import { ref, onMounted, inject } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Plus, Edit, Delete } from '@element-plus/icons-vue'
import { agentsApi, projectsApi, projectAgentsApi } from '../api'
import AgentAvatar from '../components/AgentAvatar.vue'
import AgentProfileDialog from '../components/AgentProfileDialog.vue'
import CreateTalentDialog from '../components/CreateTalentDialog.vue'
import { displayName } from '../utils/agentDisplay'

const isAdmin = inject('isAdmin')
const profileVisible = ref(false)
const profileAgent = ref(null)
function dName(a) { return displayName(a) }
function openProfile(a) { profileAgent.value = { slug: a.slug, name: a.name, emoji: a.emoji }; profileVisible.value = true }
async function onProfileSaved() { await load(); if (detail.value) detail.value = await agentsApi.detail(detail.value.id) }
const templates = ref([])
const divisions = ref([])
const count = ref(0)
const keyword = ref('')
const division = ref('')
const loading = ref(false)
const createVisible = ref(false)
const detailVisible = ref(false)
const detail = ref(null)
const detailDivision = ref('')

async function onTalentCreated() { await Promise.all([load(), loadDivisions()]) }

const projects = ref([])
const joinedProjects = ref([])
const joinableProjects = ref([])
const router = useRouter()
function gotoWorkspace(pid) {
  router.push(`/projects/${pid}?tab=workspace`)
}
const joinProjectId = ref(null)
const joining = ref(false)

let searchTimer = null

async function load() {
  loading.value = true
  try {
    const data = await agentsApi.list({ q: keyword.value, division: division.value })
    templates.value = data.templates
    count.value = data.count
  } catch (e) {
    ElMessage.error('加载失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

async function loadDivisions() {
  const data = await agentsApi.divisions()
  divisions.value = data.divisions
}

function onSearch() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(load, 250)
}

async function openDetail(id) {
  detail.value = await agentsApi.detail(id)
  detailDivision.value = detail.value.division || ''
  joinProjectId.value = null
  detailVisible.value = true
  await loadTemplateProjects(id)
}

// 改某人才的分类（输入新名即新增分类）
async function changeTalentDivision(val) {
  if (!detail.value) return
  try {
    await agentsApi.setDivision(detail.value.id, val || '')
    detail.value.division = val || ''
    ElMessage.success('分类已更新')
    await Promise.all([load(), loadDivisions()])
  } catch (e) {
    ElMessage.error('更新失败：' + (e?.response?.data?.detail || e.message))
    detailDivision.value = detail.value.division || ''
  }
}

// 改写当前筛选选中的分类名（批量）
async function renameDivision() {
  const old = division.value
  if (!old) return
  try {
    const { value } = await ElMessageBox.prompt(`把分类「${old}」改名为：`, '改写分类', {
      inputValue: old, confirmButtonText: '确定', cancelButtonText: '取消',
      inputValidator: (v) => (v && v.trim() ? true : '新分类名不能为空'),
    })
    const r = await agentsApi.renameDivision(old, value.trim())
    ElMessage.success(`已改名，影响 ${r.affected} 个人才`)
    division.value = value.trim()
    await Promise.all([load(), loadDivisions()])
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('改名失败：' + (e?.response?.data?.detail || e.message))
  }
}

// 删除当前筛选选中的分类（该分类下人才归「其他」）
async function deleteDivision() {
  const old = division.value
  if (!old) return
  try {
    await ElMessageBox.confirm(
      `删除分类「${old}」？该分类下的人才不会被删除，只是归入「其他」。`, '删除分类',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
    const r = await agentsApi.deleteDivision(old)
    ElMessage.success(`已删除分类，${r.affected} 个人才归入「其他」`)
    division.value = ''
    await Promise.all([load(), loadDivisions()])
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('删除失败：' + (e?.response?.data?.detail || e.message))
  }
}

async function loadTemplateProjects(id) {
  try {
    const r = await agentsApi.projects(id)
    joinedProjects.value = r.joined
    joinableProjects.value = r.joinable
  } catch {
    joinedProjects.value = []; joinableProjects.value = []
  }
}

async function loadProjects() {
  try {
    projects.value = (await projectsApi.list()).projects
  } catch {
    projects.value = []
  }
}

async function joinProject() {
  if (!joinProjectId.value || !detail.value) return
  joining.value = true
  try {
    await projectAgentsApi.import(joinProjectId.value, detail.value.id)
    const proj = joinableProjects.value.find((p) => p.id === joinProjectId.value)
    ElMessage.success(`已邀请「${detail.value.name}」加入：${proj?.title || ''}`)
    joinProjectId.value = null
    await Promise.all([loadTemplateProjects(detail.value.id), load()])
  } catch (e) {
    ElMessage.error('加入失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    joining.value = false
  }
}


onMounted(() => {
  load()
  loadDivisions()
  loadProjects()
})
</script>

<style scoped>
.agents { width: 100%; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.toolbar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
.search { width: 320px; }
.div-select { width: 200px; }
.total { color: #909399; font-size: 13px; margin-left: auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }
.agent-card { cursor: pointer; }
.card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.emoji { font-size: 22px; }
.name { font-weight: 600; font-size: 17px; }
.header-actions { display: flex; gap: 8px; }
.detail-div-select { width: 200px; margin-top: 2px; }
/* 底部三项文字缩小 + 各自 nowrap，统一间隔、左对齐（与上方文本区左缘齐）：一行放下不折行 */
.card-foot { display: flex; align-items: center; justify-content: flex-start; gap: 10px; }
.card-foot :deep(.el-tag) { font-size: 11px; }
.proj-count { font-size: 11px; color: #e6a23c; white-space: nowrap; }
.solved-count { font-size: 11px; color: #67c23a; white-space: nowrap; }
.desc {
  color: #606266; font-size: 13px; line-height: 1.5; margin-bottom: 10px;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.detail-head { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
.emoji-lg { font-size: 40px; }
.detail-name { font-size: 24px; font-weight: 600; margin-bottom: 6px; }
.proj-section { margin: 14px 0; }
.proj-label { font-size: 13px; color: #606266; margin-bottom: 8px; font-weight: 600; }
.proj-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.joined-tag { cursor: pointer; }
.joined-tag:hover { opacity: 0.8; }
.proj-empty { font-size: 13px; color: #c0c4cc; }
.skill-section { margin: 14px 0; }
.skill-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.skill-tag { cursor: default; }
.detail-desc { color: #606266; line-height: 1.6; }
.join-bar {
  display: flex; gap: 10px; align-items: center;
  margin-top: 16px; padding: 14px; border-radius: 8px;
  background: #f5f7fa; border: 1px solid #ebeef5;
}
.join-select { flex: 1; }
.body {
  white-space: pre-wrap; word-break: break-word; font-family: inherit;
  font-size: 13px; line-height: 1.7; color: #303133;
  background: #f8f9fb; padding: 16px; border-radius: 6px;
}
</style>

