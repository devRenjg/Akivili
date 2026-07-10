<template>
  <el-dialog v-model="visible" title="✦ 新增数字人才" width="640px"
             class="akivili-dialog" append-to-body @open="onOpen">
    <el-form label-position="top">
      <el-form-item label="名字（必填）">
        <el-input v-model="form.name" maxlength="40" placeholder="如 数据分析师" />
      </el-form-item>
      <el-form-item label="昵称（显示为「昵称（名字）」，留空则只显名字）">
        <el-input v-model="form.nickname" maxlength="40" placeholder="如 小析" />
      </el-form-item>
      <el-form-item label="一句话描述">
        <el-input v-model="form.description" type="textarea" :rows="2"
                  maxlength="200" placeholder="这个人才擅长什么" />
      </el-form-item>
      <el-form-item label="分类（可选已有分类，或输入新名即新增分类）">
        <el-select v-model="form.division" filterable allow-create clearable default-first-option
                   placeholder="如 engineering / 数据" style="width:100%">
          <el-option v-for="d in knownDivisions" :key="d.division" :label="`${d.division || '其他'} (${d.n})`"
                     :value="d.division" />
        </el-select>
      </el-form-item>
      <el-form-item label="接入模型（可选，之后也能在项目内配）">
        <el-select v-model="form.provider_id" clearable placeholder="选择大模型供应商" style="width:100%">
          <el-option v-for="p in providers" :key="p.id" :label="p.name || p.id" :value="p.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="绑定 Skills（按身份跨项目共享）">
        <el-select v-model="form.skill_slugs" multiple filterable placeholder="选择要绑定的 Skills"
                   style="width:100%">
          <el-option v-for="s in allSkills" :key="s.slug" :label="s.name" :value="s.slug" />
        </el-select>
        <div class="ct-hint">绑定后，该人才加入任何项目都自带这些 Skills（写入其记忆使用说明）。</div>
      </el-form-item>
      <el-form-item label="头像（来自 icon 文件夹）">
        <div class="icon-grid">
          <div class="icon-cell" :class="{ active: form.avatar === '' }" @click="form.avatar = ''">
            <span class="none-emoji">🤖</span><span class="cell-label">默认</span>
          </div>
          <div v-for="ic in icons" :key="ic" class="icon-cell" :class="{ active: form.avatar === ic }"
               @click="form.avatar = ic">
            <img :src="iconUrl(ic)" :alt="ic" /><span class="cell-label">{{ ic.replace(/\.[^.]+$/, '') }}</span>
          </div>
        </div>
      </el-form-item>
      <el-form-item label="人格定义（可选，Agent 的系统人格正文）">
        <el-input v-model="form.body" type="textarea" :rows="5"
                  placeholder="描述这个人才的角色、专长、工作方式…" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button class="akivili-primary-btn" :loading="saving" @click="save">创建人才</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { agentsApi, skillsApi, iconsApi, settingsApi } from '../api'

const props = defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue', 'created'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const saving = ref(false)
const allSkills = ref([])
const icons = ref([])
const providers = ref([])
const knownDivisions = ref([])

function emptyForm() {
  return {
    name: '', nickname: '', description: '', division: '',
    provider_id: '', skill_slugs: [], avatar: '', body: '',
  }
}
const form = ref(emptyForm())

function iconUrl(name) { return iconsApi.url(name) }

async function onOpen() {
  form.value = emptyForm()
  try {
    const [sk, ic, st, dv] = await Promise.all([
      skillsApi.list(), iconsApi.list(), settingsApi.get(), agentsApi.divisions(),
    ])
    allSkills.value = sk.skills || []
    icons.value = ic.icons || []
    providers.value = st.providers || []
    knownDivisions.value = dv.divisions || []
  } catch (e) {
    ElMessage.error('加载选项失败：' + (e?.response?.data?.detail || e.message))
  }
}

async function save() {
  if (!form.value.name.trim()) { ElMessage.warning('请填写名字'); return }
  saving.value = true
  try {
    const r = await agentsApi.create({
      name: form.value.name.trim(),
      nickname: form.value.nickname.trim(),
      description: form.value.description.trim(),
      division: (form.value.division || '').trim(),
      provider_id: form.value.provider_id || '',
      skill_slugs: form.value.skill_slugs,
      avatar: form.value.avatar,
      body: form.value.body,
    })
    ElMessage.success(`已创建人才「${form.value.name.trim()}」`)
    visible.value = false
    emit('created', r)
  } catch (e) {
    ElMessage.error('创建失败：' + (e?.response?.data?.detail || e.message))
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.ct-hint { font-size: 12px; color: #909399; margin-top: 4px; }
.icon-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.icon-cell { width: 60px; height: 68px; border: 1px solid var(--el-border-color); border-radius: 8px;
  display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: pointer; gap: 2px; }
.icon-cell.active { border-color: var(--el-color-primary); box-shadow: 0 0 0 2px var(--el-color-primary-light-7); }
.icon-cell img { width: 34px; height: 34px; border-radius: 6px; object-fit: cover; }
.none-emoji { font-size: 26px; }
.cell-label { font-size: 10px; color: #909399; max-width: 56px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
