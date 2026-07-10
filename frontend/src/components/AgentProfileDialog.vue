<template>
  <el-dialog v-model="visible" :title="`✦ 编辑资料 · ${agent?.name || ''}`" width="480px"
             class="akivili-dialog" append-to-body @open="onOpen">
    <el-form label-position="top">
      <el-form-item label="昵称（显示为「昵称（名字）」，留空则只显名字）">
        <el-input v-model="nickname" maxlength="40" placeholder="如 小星" />
      </el-form-item>
      <el-form-item label="头像（来自 icon 文件夹）">
        <div class="icon-grid">
          <div class="icon-cell" :class="{ active: avatar === '' }" @click="pickIcon('')">
            <span class="none-emoji">{{ agent?.emoji || '🤖' }}</span>
            <span class="cell-label">默认</span>
          </div>
          <div v-for="ic in availableIcons" :key="ic" class="icon-cell" :class="{ active: avatar === ic }"
               @click="pickIcon(ic)">
            <img :src="iconUrl(ic)" :alt="ic" />
            <span class="cell-label">{{ ic.replace(/\.[^.]+$/, '') }}</span>
          </div>
        </div>
        <div class="icon-hint">已被其他人才占用的头像不显示。往项目根目录的 icon/ 文件夹放图后点「刷新图库」可见新图。</div>
        <el-button text size="small" @click="loadIcons">刷新图库</el-button>
      </el-form-item>
      <el-form-item label="集成 Skills（按身份跨项目共享）">
        <el-select v-model="skillSelection" multiple filterable placeholder="选择要集成的 Skills"
                   style="width:100%">
          <el-option v-for="s in allSkills" :key="s.slug" :label="s.name" :value="s.slug" />
        </el-select>
        <div class="icon-hint">集成后该人才加入任何项目都自带这些 Skills（写入其记忆使用说明）。</div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button class="akivili-primary-btn" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { agentConfigApi, iconsApi, skillsApi } from '../api'

const props = defineProps({
  modelValue: Boolean,
  agent: { type: Object, default: null },   // 需含 slug/name/emoji
})
const emit = defineEmits(['update:modelValue', 'saved'])

const visible = ref(props.modelValue)
watch(() => props.modelValue, (v) => { visible.value = v; if (v) onOpen() })
watch(visible, (v) => emit('update:modelValue', v))
onMounted(() => { if (visible.value) onOpen() })

const nickname = ref('')
const avatar = ref('')
const icons = ref([])
const takenAvatars = ref([])
const takenNicknames = ref([])
const allSkills = ref([])
const skillSelection = ref([])
let originalSkills = []
const saving = ref(false)

const iconUrl = (n) => iconsApi.url(n)

// 可选头像：排除已被别的 Agent 占用的（自己当前用的仍显示）
const availableIcons = computed(() =>
  icons.value.filter((ic) => !takenAvatars.value.includes(ic) || ic === avatar.value))

function baseName(fileName) {
  return fileName.replace(/\.[^.]+$/, '')
}

// Skills 是否变更（与集合无关顺序）——只在变更时才调 setSkills，避免无谓的记忆同步
function skillsChanged() {
  const a = new Set(originalSkills)
  const b = skillSelection.value
  if (a.size !== b.length) return true
  return b.some((s) => !a.has(s))
}

async function onOpen() {
  await Promise.all([loadIcons(), loadTaken(), loadSkills()])
  try {
    const cfg = await agentConfigApi.get(props.agent.slug)
    nickname.value = cfg.nickname || ''
    avatar.value = cfg.avatar || ''
    originalSkills = cfg.skill_slugs || []
    skillSelection.value = [...originalSkills]
  } catch { nickname.value = ''; avatar.value = ''; originalSkills = []; skillSelection.value = [] }
}
async function loadSkills() {
  try { allSkills.value = (await skillsApi.list()).skills } catch { allSkills.value = [] }
}
async function loadIcons() {
  try { icons.value = (await iconsApi.list()).icons } catch { icons.value = [] }
}
async function loadTaken() {
  try {
    const t = await agentConfigApi.taken(props.agent.slug)
    takenAvatars.value = t.avatars || []
    takenNicknames.value = t.nicknames || []
  } catch { takenAvatars.value = []; takenNicknames.value = [] }
}

function pickIcon(ic) {
  const prevDefault = ic === '' ? '' : baseName(ic)
  // 选图时：昵称为空、或昵称正是上一张图的名字 → 自动填成新图名（用户可再改）
  if (ic && (!nickname.value.trim() || (avatar.value && nickname.value.trim() === baseName(avatar.value)))) {
    nickname.value = prevDefault
  }
  avatar.value = ic
}

async function save() {
  const nick = nickname.value.trim()
  if (nick && takenNicknames.value.includes(nick)) {
    return ElMessage.warning(`昵称「${nick}」已被占用，请换一个`)
  }
  saving.value = true
  try {
    await agentConfigApi.setProfile(props.agent.slug, nick, avatar.value)
    if (skillsChanged()) {
      await agentConfigApi.setSkills(props.agent.slug, skillSelection.value)
    }
    ElMessage.success('已保存')
    visible.value = false
    emit('saved')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.icon-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }
.icon-cell {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  padding: 8px 4px; border: 2px solid transparent; border-radius: 8px; cursor: pointer;
  background: rgba(255,255,255,0.05);
}
.icon-cell:hover { background: rgba(255,255,255,0.12); }
.icon-cell.active { border-color: #ffd97d; }
.icon-cell img { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; }
.none-emoji { font-size: 32px; line-height: 40px; }
.cell-label { font-size: 11px; color: #c2cbef; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.icon-hint { font-size: 12px; color: #909399; margin: 6px 0; }
</style>
