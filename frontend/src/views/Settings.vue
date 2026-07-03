<template>
  <div class="settings">
    <el-result v-if="!isAdmin" icon="warning" title="需要管理员权限"
               sub-title="设置仅管理员可见，请用管理员账号登录" />
    <template v-else>
    <div class="header">
      <h2>大模型 / CLI 供应商配置</h2>
      <div>
        <el-button @click="addProvider" :icon="Plus">新增供应商</el-button>
        <el-button type="primary" @click="save" :loading="saving">保存</el-button>
      </div>
    </div>

    <el-alert
      title="API 类型用于 Agentic 调用（Deepseek/OpenAI/Anthropic 等）；CLI 类型（Claude Code / Codex）由后端调用本地命令行执行器跑 Agent。密钥仅保存在本地，列表中以脱敏形式显示。"
      type="info" :closable="false" class="tip" />

    <el-empty v-if="providers.length === 0" description="还没有配置供应商，点右上角「新增供应商」开始" />

    <el-card v-for="(p, idx) in providers" :key="p.id || idx" class="provider-card" shadow="never">
      <div class="card-row">
        <el-radio v-model="defaultId" :label="p.id" :disabled="!p.id">默认</el-radio>
        <el-input v-model="p.name" placeholder="名称，如 Deepseek 主力" class="name-input" />
        <el-select v-model="p.type" class="type-select" @change="onTypeChange(p)">
          <el-option label="API（OpenAI/Anthropic 格式）" value="api" />
          <el-option label="Claude Code CLI" value="claude-cli" />
          <el-option label="Codex CLI" value="codex-cli" />
        </el-select>
        <el-button :icon="Delete" @click="removeProvider(idx)" circle />
      </div>

      <!-- API 类型字段 -->
      <template v-if="p.type === 'api'">
        <div class="card-row">
          <el-select v-model="apiPreset[p.id || idx]" placeholder="快速预设" class="preset-select"
                     @change="(v) => applyPreset(p, v)" clearable>
            <el-option v-for="ps in API_PRESETS" :key="ps.name" :label="ps.name" :value="ps.name" />
          </el-select>
          <el-input v-model="p.base_url" placeholder="base_url，如 https://api.deepseek.com" />
        </div>
        <div class="card-row">
          <el-input v-model="p.api_key" type="password" show-password placeholder="api_key" />
          <el-input v-model="p.model" placeholder="模型，如 deepseek-chat" class="model-input" />
          <el-select v-model="p.api_format" class="format-select">
            <el-option label="openai" value="openai" />
            <el-option label="anthropic" value="anthropic" />
          </el-select>
        </div>
      </template>

      <!-- CLI 类型字段 -->
      <template v-else>
        <div class="card-row">
          <el-input v-model="p.model" placeholder="模型别名（可选），如 opus / gpt-5" class="model-input" />
          <el-input v-model="p.executable" placeholder="可执行文件路径（可选，留空按 PATH 探测）" />
        </div>
      </template>

      <div class="card-row test-row">
        <el-button size="small" @click="test(p)" :loading="testing[p.id]">测试连通</el-button>
        <span v-if="testResult[p.id]" :class="testResult[p.id].ok ? 'ok' : 'fail'">
          {{ testResult[p.id].ok ? '✓ ' : '✗ ' }}{{ testResult[p.id].detail }}
        </span>
        <span v-else-if="!p.id" class="hint">（保存后才能测试）</span>
      </div>
    </el-card>
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, inject } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Delete } from '@element-plus/icons-vue'
import { settingsApi } from '../api'

const API_PRESETS = [
  { name: 'Deepseek', base_url: 'https://api.deepseek.com', model: 'deepseek-chat', api_format: 'openai' },
  { name: 'OpenAI', base_url: 'https://api.openai.com', model: 'gpt-4o', api_format: 'openai' },
  { name: 'Anthropic', base_url: 'https://api.anthropic.com', model: 'claude-sonnet-4-6', api_format: 'anthropic' },
  { name: '通义千问', base_url: 'https://dashscope.aliyuncs.com/compatible-mode', model: 'qwen-plus', api_format: 'openai' },
  { name: '智谱', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4', api_format: 'openai' },
  { name: 'Moonshot', base_url: 'https://api.moonshot.cn', model: 'moonshot-v1-8k', api_format: 'openai' },
  { name: 'Ollama 本地', base_url: 'http://localhost:11434', model: 'llama3', api_format: 'openai' },
]

const isAdmin = inject('isAdmin')
const providers = ref([])
const defaultId = ref('')
const saving = ref(false)
const testing = reactive({})
const testResult = reactive({})
const apiPreset = reactive({})

async function load() {
  const data = await settingsApi.get()
  providers.value = data.providers || []
  defaultId.value = data.default_provider_id || ''
}

function addProvider() {
  providers.value.push({
    id: '', name: '', type: 'api', api_key: '', base_url: '', model: '', api_format: 'openai', executable: '',
  })
}

function removeProvider(idx) {
  providers.value.splice(idx, 1)
}

function onTypeChange(p) {
  if (p.type !== 'api') p.api_format = 'openai'
}

function applyPreset(p, name) {
  const ps = API_PRESETS.find((x) => x.name === name)
  if (!ps) return
  p.base_url = ps.base_url
  p.model = ps.model
  p.api_format = ps.api_format
}

async function save() {
  saving.value = true
  try {
    await settingsApi.save({ providers: providers.value, default_provider_id: defaultId.value })
    ElMessage.success('已保存')
    await load()
  } catch (e) {
    ElMessage.error('保存失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    saving.value = false
  }
}

async function test(p) {
  if (!p.id) {
    ElMessage.warning('请先保存，再测试连通')
    return
  }
  testing[p.id] = true
  try {
    testResult[p.id] = await settingsApi.test(p.id)
  } catch (e) {
    testResult[p.id] = { ok: false, detail: e?.response?.data?.detail || e.message }
  } finally {
    testing[p.id] = false
  }
}

onMounted(load)
</script>

<style scoped>
.settings { max-width: 900px; margin: 0 auto; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.tip { margin-bottom: 16px; }
.provider-card { margin-bottom: 14px; border: 1px solid #e4e7ed; }
.card-row { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; }
.card-row:last-child { margin-bottom: 0; }
.name-input { width: 220px; }
.type-select { width: 230px; }
.preset-select { width: 160px; }
.model-input { width: 220px; }
.format-select { width: 130px; }
.test-row { margin-top: 4px; }
.ok { color: #67c23a; font-size: 13px; }
.fail { color: #f56c6c; font-size: 13px; }
.hint { color: #909399; font-size: 13px; }
</style>

