<template>
  <el-container class="app-root">
    <el-aside width="200px" class="sidebar">
      <div class="brand">
        <div class="brand-name">✦ Akivili</div>
      </div>
      <div class="identity">
        <template v-if="user">
          <span class="who">👤 {{ user.username }}</span>
          <el-button text size="small" class="auth-btn" @click="doLogout">退出</el-button>
        </template>
        <template v-else>
          <span class="who who-guest">👁 访客（只读）</span>
          <el-button text size="small" class="auth-btn" @click="loginVisible = true">登录</el-button>
        </template>
      </div>
      <el-menu :default-active="activeMenu" :default-openeds="['/projects']" @select="onSelect">
        <el-menu-item index="/dashboard">
          <el-icon><DataBoard /></el-icon><span>主页</span>
        </el-menu-item>
        <el-sub-menu index="/projects">
          <template #title>
            <el-icon><Briefcase /></el-icon><span @click="goProjects">项目空间</span>
          </template>
          <el-menu-item v-for="p in projects" :key="p.id" :index="`/projects/${p.id}?tab=workspace`">
            <span class="proj-dot">›</span><span class="proj-name">{{ p.title }}</span>
          </el-menu-item>
        </el-sub-menu>
        <el-menu-item index="/agents">
          <el-icon><Avatar /></el-icon><span>数字人才库</span>
        </el-menu-item>
        <el-menu-item index="/skills">
          <el-icon><MagicStick /></el-icon><span>Skills</span>
        </el-menu-item>
        <el-menu-item v-if="isAdmin" index="/settings">
          <el-icon><Setting /></el-icon><span>设置</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-main class="main">
      <div class="main-inner">
        <router-view />
      </div>
    </el-main>

    <el-dialog v-model="loginVisible" title="✦ 管理员登录" width="380px" class="akivili-dialog" append-to-body>
      <el-form @submit.prevent="doLogin">
        <el-form-item>
          <el-input v-model="loginForm.username" placeholder="用户名" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="loginForm.password" type="password" show-password placeholder="密码"
                    @keyup.enter="doLogin" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="loginVisible = false">取消</el-button>
        <el-button class="akivili-primary-btn" :loading="logging" @click="doLogin">登录</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<script setup>
import { ref, computed, onMounted, watch, provide } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { projectsApi, authApi } from './api'

const route = useRoute()
const router = useRouter()
const projects = ref([])

// 登录态：全局注入，各页按 role 控制 UI
const user = ref(null)
const isAdmin = computed(() => user.value?.role === 'admin')
provide('currentUser', user)
provide('isAdmin', isAdmin)

const loginVisible = ref(false)
const logging = ref(false)
const loginForm = ref({ username: '', password: '' })

async function loadMe() {
  try { user.value = (await authApi.me()).user } catch { user.value = null }
}

async function doLogin() {
  if (!loginForm.value.username || !loginForm.value.password) return
  logging.value = true
  try {
    const r = await authApi.login(loginForm.value.username, loginForm.value.password)
    user.value = r.user
    ElMessage.success(`欢迎，${r.user.username}`)
    loginVisible.value = false
    loginForm.value = { username: '', password: '' }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '登录失败')
  } finally {
    logging.value = false
  }
}

async function doLogout() {
  await authApi.logout()
  user.value = null
  ElMessage.success('已退出')
  if (route.path === '/settings') router.push('/dashboard')
}

const activeMenu = computed(() => {
  const m = route.path.match(/^\/projects\/(\d+)/)
  if (m) return `/projects/${m[1]}?tab=workspace`  // 高亮对应项目子菜单
  return route.path
})

function onSelect(index) {
  if (index === route.fullPath) return
  router.push(index)
}

function goProjects() {
  if (route.path !== '/projects') router.push('/projects')
}

async function loadProjects() {
  try { projects.value = (await projectsApi.list()).projects } catch { projects.value = [] }
}

// 路由变化时刷新项目列表（新建/删除项目后侧栏同步）
watch(() => route.path, loadProjects)
onMounted(() => { loadMe(); loadProjects() })
</script>

<style>
body { margin: 0; }
.app-root { height: 100vh; }
.sidebar { background: #1f2937; color: #fff; }
.brand { padding: 18px 20px; }
.brand-name { font-size: 19px; font-weight: 700; color: #fff; letter-spacing: 0.5px; }
.identity { display: flex; align-items: center; justify-content: space-between; padding: 0 20px 12px; }
.who { font-size: 12px; color: #cbd5e1; }
.who-guest { color: #94a3b8; }
.auth-btn { color: #ffd97d !important; padding: 0 !important; }
.sidebar .el-menu { background: transparent; border-right: none; }
.sidebar .el-menu-item,
.sidebar .el-sub-menu__title { color: #cbd5e1; }
/* hover：深色背景 + 亮白文字，避免浅色 hover 吞掉文字 */
.sidebar .el-menu-item:hover,
.sidebar .el-sub-menu__title:hover {
  background: #2b3a4f !important; color: #ffffff !important;
}
/* 选中：品牌金色高亮，对比强烈 */
.sidebar .el-menu-item.is-active {
  color: #2d1b08 !important; font-weight: 600;
  background: linear-gradient(90deg, #ffd97d, #ffe9b0) !important;
}
.sidebar .el-menu-item.is-active:hover {
  color: #2d1b08 !important;
  background: linear-gradient(90deg, #ffe199, #fff1c9) !important;
}
.sidebar .el-sub-menu.is-active > .el-sub-menu__title { color: #ffd97d !important; }
.sidebar .el-sub-menu .el-menu-item { min-width: 0; padding-right: 12px; }
.proj-dot { color: #6b7280; margin-right: 6px; }
.sidebar .el-menu-item.is-active .proj-dot { color: #2d1b08; }
.proj-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.main { background: #f5f7fa; padding: 24px; }
.main-inner { max-width: 1440px; margin: 0 auto; }

/* 全局：Akivili 金色主按钮 + 深空对话框（各页通用） */
.akivili-primary-btn {
  border: none !important; color: #2d1b08 !important; font-weight: 600;
  background: linear-gradient(90deg, #ffd97d, #ffe9b0) !important;
}
.akivili-primary-btn:hover { background: linear-gradient(90deg, #ffe199, #fff1c9) !important; }
.akivili-primary-btn.is-disabled, .akivili-primary-btn.is-disabled:hover {
  background: rgba(255, 217, 125, 0.35) !important; color: rgba(45, 27, 8, 0.5) !important;
}
.akivili-dialog {
  background: linear-gradient(150deg, #0d1330 0%, #1a2350 55%, #2d1b4e 100%);
  border: 1px solid rgba(160, 175, 230, 0.25); border-radius: 14px;
}
.akivili-dialog .el-dialog__title { color: #fff; font-weight: 700; }
.akivili-dialog .el-dialog__headerbtn .el-dialog__close { color: #aab4d4; }
.akivili-dialog .el-form-item__label { color: #c2cbef; }
.akivili-dialog .el-input__wrapper, .akivili-dialog .el-textarea__inner {
  background: rgba(255,255,255,0.06); box-shadow: 0 0 0 1px rgba(160,175,230,0.25) inset;
}
.akivili-dialog .el-input__inner, .akivili-dialog .el-textarea__inner { color: #f0f3ff; }
.akivili-dialog .el-dialog__footer .el-button {
  background: transparent; color: #c2cbef; border-color: rgba(160,175,230,0.35);
}
.akivili-ghost {
  background: transparent !important; color: #c2cbef !important;
  border-color: rgba(160, 175, 230, 0.4) !important;
}
.akivili-ghost:hover { color: #fff !important; border-color: #aab4d4 !important; }
</style>

