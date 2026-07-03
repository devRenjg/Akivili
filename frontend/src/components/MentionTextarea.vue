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
    <div v-if="showMenu && filtered.length" class="mention-menu">
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
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import AgentAvatar from './AgentAvatar.vue'
import { displayName } from '../utils/agentDisplay'

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
const query = ref('')
const atPos = ref(-1)
const activeIdx = ref(0)

const filtered = computed(() => {
  const q = query.value.toLowerCase()
  const list = props.members || []
  if (!q) return list
  return list.filter((m) =>
    (m.name || '').toLowerCase().includes(q) || (m.slug || '').toLowerCase().includes(q))
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
.mention-menu {
  position: absolute; z-index: 3000; left: 0; top: 100%; margin-top: 4px;
  min-width: 220px; max-height: 240px; overflow-y: auto;
  background: #fff; border: 1px solid #e4e7ed; border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); padding: 4px;
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
