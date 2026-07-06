<template>
  <div class="dashboard">
    <div class="hero">
      <div class="stars"></div>
      <div class="hero-content">
        <div class="hero-title">✦ Akivili <span class="hero-cn">阿基维利</span></div>
        <div class="hero-slogan">愿此行，终抵群星！</div>
        <div class="hero-desc">构建属于你自己的星穹列车，从这里出发，亲手去开拓每一个目标！</div>
      </div>
      <el-button v-if="isAdmin" class="hero-btn" type="primary" :icon="Plus" @click="createVisible = true">新建项目</el-button>
    </div>

    <div class="header">
      <h2>主页</h2>
    </div>

    <div class="stats">
      <el-card shadow="never" class="stat"><div class="num">{{ projects.length }}</div><div class="label">项目</div></el-card>
      <el-card shadow="never" class="stat"><div class="num">{{ totalAgents }}</div><div class="label">已配 Agent</div></el-card>
      <el-card shadow="never" class="stat"><div class="num">{{ activeCount }}</div><div class="label">进行中</div></el-card>
    </div>

    <h3 class="section-title">项目</h3>
    <div v-loading="loading" class="grid">
      <el-card v-for="p in projects" :key="p.id" class="proj-card" shadow="hover"
               @click="$router.push(`/projects/${p.id}?tab=workspace`)">
        <div class="proj-head">
          <span class="proj-title">{{ p.title }}</span>
          <el-tag size="small" :type="p.status === 'active' ? 'success' : 'info'">
            {{ p.status === 'active' ? '进行中' : '已归档' }}
          </el-tag>
        </div>
        <a v-if="p.git_url" class="proj-git" :href="p.git_url" target="_blank" rel="noopener"
           :title="p.git_url" @click.stop>🔗 {{ gitLabel(p.git_url) }}</a>
        <div class="proj-desc">{{ p.description || '（无描述）' }}</div>
        <div class="proj-foot">
          <span>👥 {{ p.agent_count }} 个 Agent</span>
        </div>
      </el-card>
      <el-empty v-if="!loading && projects.length === 0" description="还没有项目，点右上角「新建项目」开始" />
    </div>

    <el-dialog v-model="createVisible" title="✦ 新建项目" width="520px"
               class="akivili-dialog" append-to-body>
      <el-form label-width="92px" label-position="top">
        <el-form-item label="项目标题" required>
          <el-input v-model="form.title" placeholder="项目自起标题，如 官网改版" />
        </el-form-item>
        <el-form-item label="本地文件夹" required>
          <div class="path-row">
            <el-input v-model="form.local_path" readonly
                      placeholder="点右侧「浏览」选择一个已存在的文件夹" />
            <el-button class="akivili-ghost" :icon="FolderOpened" @click="pickerVisible = true">浏览</el-button>
          </div>
        </el-form-item>
        <el-form-item label="仓库链接（可选，展示用）">
          <el-input v-model="form.git_url" placeholder="如 https://github.com/xxx/yyy" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button class="akivili-primary-btn" @click="doCreate" :loading="creating">出发</el-button>
      </template>
    </el-dialog>

    <DirectoryPicker v-model="pickerVisible" @picked="onPicked" />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, inject } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, FolderOpened } from '@element-plus/icons-vue'
import { projectsApi } from '../api'
import DirectoryPicker from '../components/DirectoryPicker.vue'

const isAdmin = inject('isAdmin')
const projects = ref([])
const loading = ref(false)
const createVisible = ref(false)
const creating = ref(false)
const pickerVisible = ref(false)
const form = ref({ title: '', local_path: '', description: '', git_url: '' })

function onPicked(path) {
  form.value.local_path = path
}
// 仓库链接精简显示：去协议、留 host/owner/repo
function gitLabel(url) {
  return (url || '').replace(/^https?:\/\//, '').replace(/\.git$/, '').replace(/\/$/, '')
}

const totalAgents = computed(() => projects.value.reduce((s, p) => s + (p.agent_count || 0), 0))
const activeCount = computed(() => projects.value.filter((p) => p.status === 'active').length)

async function load() {
  loading.value = true
  try {
    projects.value = (await projectsApi.list()).projects
  } finally {
    loading.value = false
  }
}

async function doCreate() {
  if (!form.value.title.trim() || !form.value.local_path.trim()) {
    ElMessage.warning('标题与本地文件夹必填')
    return
  }
  creating.value = true
  try {
    await projectsApi.create(form.value)
    ElMessage.success('已创建')
    createVisible.value = false
    form.value = { title: '', local_path: '', description: '', git_url: '' }
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
.dashboard { width: 100%; }
.hero {
  position: relative; overflow: hidden;
  border-radius: 14px; padding: 36px 40px; margin-bottom: 24px;
  background: linear-gradient(120deg, #0b1026 0%, #1a2350 45%, #2d1b4e 100%);
  display: flex; align-items: center; justify-content: space-between;
}
.stars {
  position: absolute; inset: 0; pointer-events: none;
  background-image:
    radial-gradient(1.5px 1.5px at 20% 30%, #fff, transparent),
    radial-gradient(1px 1px at 60% 20%, #cbd5ff, transparent),
    radial-gradient(1.5px 1.5px at 80% 60%, #fff, transparent),
    radial-gradient(1px 1px at 35% 70%, #a5b4fc, transparent),
    radial-gradient(1px 1px at 90% 35%, #fff, transparent),
    radial-gradient(1.5px 1.5px at 50% 50%, #e0e7ff, transparent),
    radial-gradient(1px 1px at 15% 85%, #fff, transparent),
    radial-gradient(1px 1px at 75% 80%, #c7d2fe, transparent);
  opacity: 0.7;
}
.hero-content { position: relative; z-index: 1; }
.hero-title { font-size: 26px; font-weight: 700; color: #fff; letter-spacing: 1px; }
.hero-cn { font-size: 16px; font-weight: 400; color: #b9c2e6; margin-left: 8px; letter-spacing: 4px; }
.hero-slogan {
  font-size: 22px; font-weight: 600; margin-top: 14px; letter-spacing: 2px;
  background: linear-gradient(90deg, #ffd97d, #fff1c9, #ffd97d);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
.hero-desc { color: #aab4d4; font-size: 13px; margin-top: 10px; }
.hero-btn {
  position: relative; z-index: 1;
  border: none; color: #2d1b08; font-weight: 600;
  background: linear-gradient(90deg, #ffd97d, #ffe9b0);
}
.hero-btn:hover { background: linear-gradient(90deg, #ffe199, #fff1c9); color: #2d1b08; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.stats { display: flex; gap: 14px; margin-bottom: 20px; }
.stat { flex: 1; text-align: center; }
.stat .num { font-size: 28px; font-weight: 700; color: #303133; }
.stat .label { color: #909399; font-size: 13px; margin-top: 4px; }
.section-title { margin: 0 0 12px; font-size: 15px; color: #606266; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.proj-card { cursor: pointer; }
.proj-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.proj-title { font-weight: 600; font-size: 16px; }
.proj-git { display: block; color: #409eff; font-size: 12px; margin-bottom: 8px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; text-decoration: none; }
.proj-git:hover { text-decoration: underline; }
.proj-desc { color: #606266; font-size: 13px; min-height: 20px; margin-bottom: 10px; }
.proj-foot { color: #606266; font-size: 13px; border-top: 1px solid #f0f0f0; padding-top: 8px; }
.path-row { display: flex; gap: 8px; width: 100%; }
.path-row .el-input { flex: 1; min-width: 0; }
</style>

<!-- 新建项目对话框：深空主题（dialog 渲染到 body，需非 scoped 全局样式） -->
<style>
.akivili-dialog {
  background: linear-gradient(150deg, #0d1330 0%, #1a2350 55%, #2d1b4e 100%);
  border: 1px solid rgba(160, 175, 230, 0.25);
  border-radius: 14px;
  box-shadow: 0 12px 48px rgba(10, 14, 40, 0.6);
}
.akivili-dialog .el-dialog__title {
  color: #fff; font-weight: 700; letter-spacing: 1px;
}
.akivili-dialog .el-dialog__headerbtn .el-dialog__close { color: #aab4d4; }
.akivili-dialog .el-dialog__headerbtn:hover .el-dialog__close { color: #ffd97d; }
.akivili-dialog .el-form-item__label { color: #c2cbef; }
.akivili-dialog .el-input__wrapper,
.akivili-dialog .el-textarea__inner {
  background: rgba(255, 255, 255, 0.06);
  box-shadow: 0 0 0 1px rgba(160, 175, 230, 0.25) inset;
}
.akivili-dialog .el-input__wrapper.is-focus {
  box-shadow: 0 0 0 1px #ffd97d inset;
}
.akivili-dialog .el-input__inner,
.akivili-dialog .el-textarea__inner { color: #f0f3ff; }
.akivili-dialog .el-input__inner::placeholder,
.akivili-dialog .el-textarea__inner::placeholder { color: #8893bf; }
.akivili-dialog .el-dialog__footer .el-button {
  background: transparent; color: #c2cbef; border-color: rgba(160, 175, 230, 0.35);
}
.akivili-dialog .el-dialog__footer .el-button:hover { color: #fff; border-color: #aab4d4; }
.akivili-primary-btn {
  border: none !important; color: #2d1b08 !important; font-weight: 600;
  background: linear-gradient(90deg, #ffd97d, #ffe9b0) !important;
}
.akivili-primary-btn:hover { background: linear-gradient(90deg, #ffe199, #fff1c9) !important; }
.akivili-primary-btn.is-disabled,
.akivili-primary-btn.is-disabled:hover {
  background: rgba(255, 217, 125, 0.35) !important; color: rgba(45, 27, 8, 0.5) !important;
}
.akivili-ghost {
  background: transparent !important; color: #c2cbef !important;
  border-color: rgba(160, 175, 230, 0.4) !important;
}
.akivili-ghost:hover { color: #fff !important; border-color: #aab4d4 !important; }
</style>


