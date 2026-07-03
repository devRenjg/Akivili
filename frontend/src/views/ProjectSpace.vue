<template>
  <div class="project-space">
    <div class="header">
      <h2>项目空间</h2>
      <el-button v-if="isAdmin" class="akivili-primary-btn" :icon="Plus" @click="createVisible = true">新建项目</el-button>
    </div>

    <div v-loading="loading" class="grid">
      <el-card v-for="p in projects" :key="p.id" class="proj-card" shadow="hover"
               @click="$router.push(`/projects/${p.id}?tab=workspace`)">
        <div class="proj-head">
          <span class="proj-title">{{ p.title }}</span>
          <el-tag size="small" :type="p.status === 'active' ? 'success' : 'info'">
            {{ p.status === 'active' ? '进行中' : '已归档' }}
          </el-tag>
        </div>
        <div class="proj-path" :title="p.local_path">📁 {{ p.local_path }}</div>
        <div class="proj-desc">{{ p.description || '（无描述）' }}</div>
        <div class="proj-foot">
          <span>👥 {{ p.agent_count }} 个成员</span>
          <el-button text size="small" @click.stop="$router.push(`/projects/${p.id}`)">概览/团队 →</el-button>
        </div>
      </el-card>
      <el-empty v-if="!loading && projects.length === 0" description="还没有项目，点右上角「新建项目」开始" />
    </div>

    <el-dialog v-model="createVisible" title="✦ 新建项目" width="520px" class="akivili-dialog" append-to-body>
      <el-form label-position="top">
        <el-form-item label="项目标题" required>
          <el-input v-model="form.title" placeholder="项目自起标题，如 官网改版" />
        </el-form-item>
        <el-form-item label="本地文件夹" required>
          <div class="path-row">
            <el-input v-model="form.local_path" readonly placeholder="点右侧「浏览」选择一个已存在的文件夹" />
            <el-button class="akivili-ghost" :icon="FolderOpened" @click="pickerVisible = true">浏览</el-button>
          </div>
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

    <DirectoryPicker v-model="pickerVisible" @picked="(p) => (form.local_path = p)" />
  </div>
</template>

<script setup>
import { ref, onMounted, inject } from 'vue'
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
const form = ref({ title: '', local_path: '', description: '' })

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
    return ElMessage.warning('标题与本地文件夹必填')
  }
  creating.value = true
  try {
    await projectsApi.create(form.value)
    ElMessage.success('已创建')
    createVisible.value = false
    form.value = { title: '', local_path: '', description: '' }
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
.project-space { width: 100%; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; }
.header h2 { margin: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.proj-card { cursor: pointer; }
.proj-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.proj-title { font-weight: 600; font-size: 16px; }
.proj-path { color: #909399; font-size: 12px; margin-bottom: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.proj-desc { color: #606266; font-size: 13px; min-height: 20px; margin-bottom: 10px; }
.proj-foot { display: flex; justify-content: space-between; align-items: center; color: #606266; font-size: 13px; border-top: 1px solid #f0f0f0; padding-top: 8px; }
.path-row { display: flex; gap: 8px; width: 100%; }
.path-row .el-input { flex: 1; min-width: 0; }
</style>
