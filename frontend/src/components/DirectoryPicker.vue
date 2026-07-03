<template>
  <el-dialog v-model="visible" title="✦ 选择本地文件夹" width="560px"
             class="akivili-dialog" append-to-body @open="onOpen">
    <div class="picker">
      <div class="current">
        <span class="cur-label">当前：</span>
        <span class="cur-path">{{ cur.path || '（选择一个盘符）' }}</span>
      </div>

      <div v-loading="loading" class="dir-list">
        <div v-if="!cur.is_root" class="dir-item up" @click="go(cur.parent)">
          <el-icon><Top /></el-icon><span>.. 上一级</span>
        </div>
        <div v-for="d in cur.dirs" :key="d.path" class="dir-item"
             :class="{ selected: selected === d.path }"
             @click="select(d)" @dblclick="go(d.path)">
          <el-icon><Folder /></el-icon><span>{{ d.name }}</span>
        </div>
        <el-empty v-if="!loading && cur.dirs.length === 0" :image-size="60" description="该目录下没有子文件夹" />
      </div>
      <div class="hint">单击选中，双击进入下一层；可直接选中当前层的某个文件夹，或进入后选「用此文件夹」。</div>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button v-if="!cur.is_root" class="akivili-ghost" @click="useCurrent">用当前文件夹</el-button>
      <el-button class="akivili-primary-btn" :disabled="!selected" @click="confirmSelected">用选中的</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref } from 'vue'
import { Folder, Top } from '@element-plus/icons-vue'
import { fsApi } from '../api'

const props = defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue', 'picked'])

const visible = ref(false)
const cur = ref({ path: '', parent: '', is_root: true, dirs: [] })
const selected = ref('')
const loading = ref(false)

// 与父组件的 v-model 同步
import { watch } from 'vue'
watch(() => props.modelValue, (v) => { visible.value = v })
watch(visible, (v) => emit('update:modelValue', v))

async function go(path) {
  loading.value = true
  selected.value = ''
  try {
    cur.value = await fsApi.list(path)
  } finally {
    loading.value = false
  }
}

function onOpen() {
  go('')  // 从盘符开始
}

function select(d) {
  selected.value = d.path
}

function confirmSelected() {
  emit('picked', selected.value)
  visible.value = false
}

function useCurrent() {
  emit('picked', cur.value.path)
  visible.value = false
}
</script>

<style scoped>
.current { margin-bottom: 10px; font-size: 13px; }
.cur-label { color: #8893bf; }
.cur-path { color: #ffd97d; font-weight: 600; word-break: break-all; }
.dir-list {
  max-height: 340px; overflow-y: auto;
  border: 1px solid rgba(160, 175, 230, 0.25); border-radius: 8px;
  background: rgba(255, 255, 255, 0.04); padding: 6px;
}
.dir-item {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; border-radius: 6px; cursor: pointer;
  color: #d7ddf5; font-size: 14px;
}
.dir-item:hover { background: rgba(160, 175, 230, 0.14); }
.dir-item.selected { background: rgba(255, 217, 125, 0.18); color: #ffe9b0; }
.dir-item.up { color: #8893bf; }
.hint { margin-top: 10px; font-size: 12px; color: #8893bf; }
</style>
