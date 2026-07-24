<template>
  <div class="mention-wrap">
    <textarea
      ref="ta"
      v-model="text"
      class="mention-textarea"
      :rows="rows"
      :placeholder="placeholder"
      @input="onInput"
      @keydown="onKeydown"
      @blur="onBlur"
      @click="onInput"
    ></textarea>
  </div>
  <Teleport to="body">
    <div v-if="showMenu && filtered.length" class="mention-menu" :class="{ 'side-panel': sidePanel }" :style="menuStyle">
      <div v-if="sidePanel" class="mm-head">选择成员</div>
      <div
        v-for="(m, i) in filtered"
        :key="m.slug"
        class="mention-item"
        :class="{ active: i === activeIdx }"
        @mousedown.prevent="pick(m)"
      >
        <AgentAvatar :agent="m" :size="24" />
        <span class="mi-name">{{ dName(m) }}</span>
        <span v-if="m.is_leader" class="mi-leader">👑</span>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { pinyin } from 'pinyin-pro'
import AgentAvatar from './AgentAvatar.vue'
import { displayName } from '../utils/agentDisplay'

// 中文转拼音索引：返回「全拼」和「首字母」两种，供匹配（如 卡芙卡 → kafuka / kfk）
function pyIndex(s) {
  if (!s) return { full: '', initials: '' }
  const hasHan = /[一-龥]/.test(s)
  if (!hasHan) return { full: s.toLowerCase(), initials: s.toLowerCase() }
  const full = pinyin(s, { toneType: 'none', type: 'array' }).join('').toLowerCase()
  const initials = pinyin(s, { pattern: 'first', toneType: 'none', type: 'array' }).join('').toLowerCase()
  return { full, initials }
}

const props = defineProps({
  modelValue: { type: String, default: '' },
  members: { type: Array, default: () => [] },
  rows: { type: Number, default: 10 },
  placeholder: { type: String, default: '输入 @ 可点名项目成员…' },
})
const emit = defineEmits(['update:modelValue'])
function dName(a) { return displayName(a) }

const text = ref(props.modelValue)
watch(() => props.modelValue, (v) => { if (v !== text.value) text.value = v })
watch(text, (v) => emit('update:modelValue', v))

const ta = ref(null)
const showMenu = ref(false)
const sidePanel = ref(false)     // true=作为右侧独立面板（Teleport 到 body、fixed 定位）
const menuStyle = ref({})        // 侧边面板的 fixed 坐标
const query = ref('')
const atPos = ref(-1)
const activeIdx = ref(0)

const MENU_W = 240               // 侧边面板宽度（与 CSS min-width 对齐）
const MENU_MAX_H = 320           // 与 CSS .mention-menu max-height 保持一致
const GAP = 12                   // 面板与输入框的水平间距

// 计算弹层定位：优先「输入框右侧外部、顶部平齐」的独立面板；
// 右侧放不下 → 回退到输入框下方（不足则上方），仍走 fixed，避免被对话框裁切。
function decidePlacement() {
  const el = ta.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  const spaceRight = window.innerWidth - rect.right
  if (spaceRight >= MENU_W + GAP) {
    // 右侧空间充足：贴右侧外部，顶部与输入框平齐；底部不超出视口
    sidePanel.value = true
    const top = Math.min(rect.top, window.innerHeight - MENU_MAX_H - 8)
    menuStyle.value = {
      position: 'fixed',
      left: `${rect.right + GAP}px`,
      top: `${Math.max(8, top)}px`,
      width: `${MENU_W}px`,
    }
    return
  }
  // 右侧不够：回退到下方/上方（仍 fixed，脱离对话框裁切）
  sidePanel.value = false
  const spaceBelow = window.innerHeight - rect.bottom
  const up = spaceBelow < MENU_MAX_H && rect.top > spaceBelow
  menuStyle.value = up
    ? { position: 'fixed', left: `${rect.left}px`, bottom: `${window.innerHeight - rect.top + 4}px`, width: `${Math.max(MENU_W, rect.width)}px` }
    : { position: 'fixed', left: `${rect.left}px`, top: `${rect.bottom + 4}px`, width: `${Math.max(MENU_W, rect.width)}px` }
}

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  const list = props.members || []
  if (!q) return list
  return list.filter((m) => {
    const name = (m.name || '').toLowerCase()
    const nick = (m.nickname || '').toLowerCase()
    const slug = (m.slug || '').toLowerCase()
    // 1) 原文子串（中文/英文）：输入「卡」命中「卡芙卡」
    if (name.includes(q) || nick.includes(q) || slug.includes(q)) return true
    // 2) 拼音：全拼或首字母，昵称优先（如 ka/kafuka/kfk 命中 卡芙卡）
    const py = pyIndex(m.nickname || m.name || '')
    return py.full.includes(q) || py.initials.includes(q)
  })
})

function onInput() {
  const el = ta.value
  if (!el) return
  const val = el.value          // 直接读 DOM 值，避免 v-model 时序问题
  const caret = el.selectionStart
  const before = val.slice(0, caret)
  const m = before.match(/@([^\s@]*)$/)
  if (m) {
    atPos.value = caret - m[0].length
    query.value = m[1]
    activeIdx.value = 0
    decidePlacement()            // 每次实时定位，跟随输入框位置
    showMenu.value = true
  } else {
    showMenu.value = false
  }
}

function onKeydown(e) {
  if (!showMenu.value || !filtered.value.length) return
  if (e.key === 'ArrowDown') {
    e.preventDefault(); activeIdx.value = (activeIdx.value + 1) % filtered.value.length
  } else if (e.key === 'ArrowUp') {
    e.preventDefault(); activeIdx.value = (activeIdx.value - 1 + filtered.value.length) % filtered.value.length
  } else if (e.key === 'Enter') {
    e.preventDefault(); pick(filtered.value[activeIdx.value])
  } else if (e.key === 'Escape') {
    showMenu.value = false
  }
}

function pick(m) {
  if (!m) return
  const el = ta.value
  const val = el ? el.value : text.value
  const caret = el ? el.selectionStart : val.length
  const after = val.slice(caret)
  const head = val.slice(0, atPos.value)
  // 插入统一格式：有昵称「@昵称（名字）」，否则「@名字」——后端两者都能解析
  const token = (m.nickname && m.nickname.trim()) ? `@${m.nickname}（${m.name}）` : `@${m.name}`
  text.value = `${head}${token} ${after}`
  showMenu.value = false
  nextTick(() => {
    const pos = (head + token + ' ').length
    if (el) { el.focus(); el.setSelectionRange(pos, pos) }
  })
}

function onBlur() {
  setTimeout(() => { showMenu.value = false }, 150)
}

// 弹层为 fixed 定位，滚动/缩放时需跟随输入框重新定位
function reposition() { if (showMenu.value) decidePlacement() }
watch(showMenu, (open) => {
  if (open) {
    window.addEventListener('scroll', reposition, true)   // capture=true 捕获对话框内滚动
    window.addEventListener('resize', reposition)
  } else {
    window.removeEventListener('scroll', reposition, true)
    window.removeEventListener('resize', reposition)
  }
})
onBeforeUnmount(() => {
  window.removeEventListener('scroll', reposition, true)
  window.removeEventListener('resize', reposition)
})
</script>

<style scoped>
.mention-wrap { position: relative; width: 100%; }
.mention-textarea {
  width: 100%; box-sizing: border-box; resize: vertical;
  min-height: 200px; padding: 10px 12px; font-size: 14px; line-height: 1.6;
  font-family: inherit; color: #303133;
  border: 1px solid #dcdfe6; border-radius: 6px; outline: none;
}
.mention-textarea:focus { border-color: #409eff; }
/* 弹层已 Teleport 到 body 并由 inline style 做 fixed 定位；此处仅负责视觉 */
.mention-menu {
  z-index: 3000; min-width: 220px; max-height: 320px; overflow-y: auto;
  background: #fff; border: 1px solid #e4e7ed; border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); padding: 4px;
}
/* 右侧独立面板：与输入框顶部平齐、悬于对话框外侧 */
.mention-menu.side-panel {
  box-shadow: 0 6px 24px rgba(0,0,0,0.16);
  border-color: #d9ecff;
}
.mm-head {
  padding: 6px 10px 8px; font-size: 12px; color: #909399;
  border-bottom: 1px solid #f0f2f5; margin-bottom: 4px;
}
.mention-item {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border-radius: 6px; cursor: pointer; font-size: 14px; color: #303133;
}
.mention-item:hover, .mention-item.active { background: #f0f6ff; }
.mi-emoji { font-size: 16px; }
.mi-name { flex: 1; }
.mi-leader { font-size: 12px; }
</style>
