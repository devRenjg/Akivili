<template>
  <div v-loading="loading" class="detail">
    <div class="topbar">
      <el-button :icon="ArrowLeft" @click="$router.push('/dashboard')">返回</el-button>
      <h2 v-if="project">{{ project.title }}</h2>
      <el-tag v-if="project" :type="project.status === 'active' ? 'success' : 'info'">
        {{ project.status === 'active' ? '进行中' : '已归档' }}
      </el-tag>
    </div>

    <el-tabs v-model="activeTab" class="proj-tabs">
      <el-tab-pane label="概览与团队" name="overview">
    <div class="team-head">
      <h3>团队</h3>
      <span class="team-hint">在「数字人才库」里邀请人才加入</span>
    </div>

    <div class="grid">
      <el-card v-for="a in visibleTeam" :key="a.id" class="agent-card" shadow="hover"
               :class="{ 'is-leader': a.is_leader }">
        <el-button v-if="isAdmin" class="ac-remove" :icon="Close" circle size="small"
                   title="移除" @click="removeAgent(a)" />
        <div class="ac-head">
          <AgentAvatar :agent="a" :size="40" />
          <span class="name">{{ dName(a) }}</span>
        </div>
        <div class="ac-meta">
          <el-tag v-if="a.is_leader" size="small" type="warning" effect="dark" class="leader-tag">👑 总负责人</el-tag>
          <el-tag size="small" effect="plain">通用人才</el-tag>
          <span class="solved-count" title="已完成任务数">✅ {{ a.solved_tasks || 0 }}</span>
        </div>

        <div class="ac-config">
          <div class="cfg-row">
            <span class="cfg-label">模型</span>
            <el-select v-if="isAdmin" :model-value="cfg[a.slug]?.provider_id || ''" size="small" class="cfg-model"
                       placeholder="未接入" @change="(v) => setModel(a, v)"
                       :no-data-text="providers.length ? '' : '先去设置页配置供应商'">
              <el-option v-for="p in providers" :key="p.id" :label="p.name || p.id" :value="p.id" />
            </el-select>
            <span v-else class="cfg-readonly">{{ providerName(cfg[a.slug]?.provider_id) }}</span>
          </div>
          <div class="cfg-row">
            <span class="cfg-label">Skills</span>
            <el-button v-if="isAdmin" text size="small" @click="openSkills(a)">
              已启用 {{ cfg[a.slug]?.skill_slugs?.length || 0 }} 个 →
            </el-button>
            <span v-else class="cfg-readonly">已启用 {{ cfg[a.slug]?.skill_slugs?.length || 0 }} 个</span>
          </div>
        </div>

        <div class="ac-actions">
          <el-button v-if="isAdmin" text size="small" @click="openProfile(a)">资料</el-button>
          <el-button v-if="isAdmin && !a.is_leader" text size="small" @click="makeLeader(a)">设为负责人</el-button>
          <el-button text size="small" @click="openPersona(a)">{{ isAdmin ? '改造人格' : '查看人格' }}</el-button>
          <el-button v-if="isAdmin" text size="small" @click="openMemory(a)">记忆</el-button>
        </div>
      </el-card>
      <el-empty v-if="!loading && visibleTeam.length === 0" description="还没有人才加入，去「数字人才库」邀请人才加入" />
    </div>
      </el-tab-pane>

      <el-tab-pane label="工作区" name="workspace" lazy>
        <Workspace v-if="project" :embed="true" :pid-prop="pid" :team-prop="team" />
      </el-tab-pane>
    </el-tabs>


    <!-- Skills 勾选 -->
    <el-dialog v-model="skillsVisible" width="600px">
      <template #header>
        <span class="dlg-title"><AgentAvatar :agent="editing" :size="22" /> 配置 Skills · {{ dName(editing) }}</span>
      </template>
      <el-checkbox-group v-model="skillSelection">
        <div v-for="s in allSkills" :key="s.slug" class="skill-opt">
          <el-checkbox :value="s.slug">
            <span class="so-name">✦ {{ s.name }}</span>
            <span class="so-desc">{{ s.description }}</span>
          </el-checkbox>
        </div>
      </el-checkbox-group>
      <el-empty v-if="allSkills.length === 0" :image-size="60"
                description="Skills 库为空，先去「Skills」页新建或导入" />
      <template #footer>
        <el-button @click="skillsVisible = false">取消</el-button>
        <el-button type="primary" @click="saveSkills">保存</el-button>
      </template>
    </el-dialog>


    <!-- 改造人格 -->
    <el-drawer v-model="personaVisible" size="55%">
      <template #header>
        <span class="dlg-title"><AgentAvatar :agent="editing" :size="22" /> {{ isAdmin ? '改造' : '查看' }}人格 · {{ dName(editing) }}</span>
      </template>
      <el-input v-model="personaText" type="textarea" :rows="22" :readonly="!isAdmin" />
      <div v-if="isAdmin" class="drawer-foot">
        <el-button type="primary" @click="savePersona">保存</el-button>
      </div>
    </el-drawer>

    <!-- 记忆 -->
    <el-drawer v-model="memoryVisible" size="55%">
      <template #header>
        <span class="dlg-title"><AgentAvatar :agent="editing" :size="22" /> 记忆 · {{ dName(editing) }}</span>
      </template>
      <el-alert type="info" :closable="false" class="mem-tip"
        title="这是该 Agent 跨项目共用的持久记忆（memory/<slug>.md）。Agent 开工先读、收工写回。" />
      <el-input v-model="memoryText" type="textarea" :rows="20" placeholder="（暂无记忆）" :readonly="!isAdmin" />
      <div v-if="isAdmin" class="drawer-foot">
        <el-button type="primary" @click="saveMemory">保存</el-button>
      </div>
    </el-drawer>

    <AgentProfileDialog v-if="profileAgent" v-model="profileVisible" :agent="profileAgent"
                        @saved="onProfileSaved" />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, inject } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Close } from '@element-plus/icons-vue'
import { projectsApi, projectAgentsApi, memoryApi,
         settingsApi, skillsApi, agentConfigApi } from '../api'
import Workspace from './Workspace.vue'
import AgentAvatar from '../components/AgentAvatar.vue'
import AgentProfileDialog from '../components/AgentProfileDialog.vue'
import { displayName } from '../utils/agentDisplay'

const route = useRoute()
const pid = Number(route.params.id)
const activeTab = ref(route.query.tab === 'workspace' ? 'workspace' : 'overview')
const isAdmin = inject('isAdmin')

function providerName(pidStr) {
  if (!pidStr) return '未接入'
  const p = providers.value.find((x) => x.id === pidStr)
  return p ? (p.name || p.id) : '未接入'
}

const project = ref(null)
const team = ref([])
const loading = ref(false)
// 项目区只展示从人才库来的通用人才，不再显示历史自建 Agent（template_id 为空）
const visibleTeam = computed(() => team.value.filter((a) => a.template_id))

// Agent 配置（模型 + skills），按 slug 缓存
const cfg = ref({})            // { slug: { provider_id, skill_slugs } }
const providers = ref([])      // 已配置供应商
const allSkills = ref([])      // 全局 skill 库
const skillsVisible = ref(false)
const skillSelection = ref([])

// 资料（昵称/头像）编辑
const profileVisible = ref(false)
const profileAgent = ref(null)
function dName(a) { return displayName(a) }
function openProfile(a) { profileAgent.value = a; profileVisible.value = true }
async function onProfileSaved() { await load() }

const personaVisible = ref(false)
const personaText = ref('')
const editing = ref(null)

const memoryVisible = ref(false)
const memoryText = ref('')

async function load() {
  loading.value = true
  try {
    project.value = await projectsApi.get(pid)
    team.value = (await projectAgentsApi.list(pid)).agents
    await loadConfigs()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e.message)
  } finally {
    loading.value = false
  }
}

async function loadAux() {
  try { providers.value = (await settingsApi.get()).providers } catch { providers.value = [] }
  try { allSkills.value = (await skillsApi.list()).skills } catch { allSkills.value = [] }
}

async function loadConfigs() {
  // 每个 Agent 按 slug 拉取接入模型与已启用 skills
  const map = {}
  await Promise.all(team.value.map(async (a) => {
    map[a.slug] = await agentConfigApi.get(a.slug)
  }))
  cfg.value = map
}

// —— 接入模型 ——
async function setModel(a, providerId) {
  await agentConfigApi.setModel(a.slug, providerId)
  cfg.value[a.slug] = { ...cfg.value[a.slug], provider_id: providerId }
  const p = providers.value.find((x) => x.id === providerId)
  ElMessage.success(`「${a.name}」接入模型：${p?.name || providerId}`)
}

// —— Skills 勾选 ——
function openSkills(a) {
  editing.value = a
  skillSelection.value = [...(cfg.value[a.slug]?.skill_slugs || [])]
  skillsVisible.value = true
}
async function saveSkills() {
  await agentConfigApi.setSkills(editing.value.slug, skillSelection.value)
  cfg.value[editing.value.slug] = {
    ...cfg.value[editing.value.slug], skill_slugs: [...skillSelection.value],
  }
  ElMessage.success('Skills 已保存')
  skillsVisible.value = false
}

// —— 改造人格 ——
function openPersona(a) {
  editing.value = a
  personaText.value = a.persona || ''
  personaVisible.value = true
}
async function savePersona() {
  await projectAgentsApi.update(pid, editing.value.id, { persona: personaText.value })
  ElMessage.success('已保存')
  personaVisible.value = false
  await load()
}

// —— 记忆 ——
function agentSlug(a) {
  // 项目内 Agent 的 slug 由后端分配：导入的继承模版 slug（同一 Agent 跨项目共用记忆），自建为 custom-<项目>-<名称>
  return a.slug
}
async function openMemory(a) {
  editing.value = a
  memoryText.value = (await memoryApi.read(agentSlug(a))).content
  memoryVisible.value = true
}
async function saveMemory() {
  await memoryApi.write(agentSlug(editing.value), memoryText.value)
  ElMessage.success('记忆已保存')
  memoryVisible.value = false
}

// —— 移除 ——
async function removeAgent(a) {
  try {
    await ElMessageBox.confirm(
      `确定将「${a.name}」从团队中移除吗？此操作不可撤销。`,
      '移除成员',
      { type: 'warning', confirmButtonText: '确定移除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return  // 用户取消
  }
  await projectAgentsApi.remove(pid, a.id)
  ElMessage.success('已移除')
  await load()
}

async function makeLeader(a) {
  await projectAgentsApi.setLeader(pid, a.id)
  ElMessage.success(`「${a.name}」已设为团队总负责人`)
  await load()
}

onMounted(() => {
  loadAux()
  load()
})
</script>

<style scoped>
.detail { width: 100%; }
.topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.topbar h2 { margin: 0; flex: 1; }
.team-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.team-head h3 { margin: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.agent-card { position: relative; }
.ac-remove {
  position: absolute; top: 8px; right: 8px; z-index: 2;
  width: 24px; height: 24px; color: #c0c4cc; border-color: transparent; background: transparent;
}
.ac-remove:hover { color: #fff; background: #f56c6c; border-color: #f56c6c; }
.ac-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; padding-right: 26px; }
.ac-head .emoji { font-size: 20px; }
.ac-head .name { font-weight: 600; font-size: 16px; }
.dlg-title { display: inline-flex; align-items: center; gap: 8px; font-weight: 600; }
.ac-meta { margin-bottom: 10px; display: flex; flex-wrap: wrap; gap: 4px; }
.ac-actions { display: flex; flex-wrap: wrap; gap: 4px; border-top: 1px solid #f0f0f0; padding-top: 8px; }
.ac-config { margin: 8px 0; padding: 8px 0; border-top: 1px solid #f5f5f5; }
.cfg-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.agent-card.is-leader { border: 1px solid #f0c000; box-shadow: 0 0 0 2px rgba(240,192,0,0.12); }
.leader-tag { margin-right: 4px; }
.solved-count { font-size: 12px; color: #67c23a; margin-left: auto; align-self: center; }
.cfg-row:last-child { margin-bottom: 0; }
.cfg-label { font-size: 12px; color: #909399; width: 42px; flex-shrink: 0; }
.cfg-model { flex: 1; }
.cfg-readonly { font-size: 13px; color: #606266; }
.share-note {
  margin: 18px 0 0; padding: 12px 14px; border-radius: 8px;
  background: #f0f6ff; color: #5a6b8c; font-size: 13px; line-height: 1.6;
}
.skill-opt { padding: 8px 0; border-bottom: 1px solid #f5f5f5; }
.so-name { font-weight: 600; margin-right: 8px; }
.so-desc { color: #909399; font-size: 12px; }
.team-hint { color: #909399; font-size: 12px; }
.drawer-foot { margin-top: 14px; text-align: right; }
.mem-tip { margin-bottom: 12px; }
</style>


