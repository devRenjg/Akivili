<template>
  <span class="agent-avatar" :style="{ width: size + 'px', height: size + 'px', fontSize: (size * 0.62) + 'px' }">
    <img v-if="url && !broken" :src="url" :alt="agent?.name" @error="broken = true" />
    <span v-else class="emoji">{{ agent?.emoji || '🤖' }}</span>
  </span>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { avatarUrl } from '../utils/agentDisplay'

const props = defineProps({
  agent: { type: Object, default: null },
  size: { type: Number, default: 24 },
})
const url = computed(() => avatarUrl(props.agent))
// 头像图加载失败（如按用户名猜的 <name>.png 不存在）→ 回退 emoji
const broken = ref(false)
watch(url, () => { broken.value = false })
</script>

<style scoped>
.agent-avatar {
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 50%; overflow: hidden; vertical-align: middle; flex-shrink: 0;
  background: #f0f2f5;
}
.agent-avatar img { width: 100%; height: 100%; object-fit: cover; }
.emoji { line-height: 1; }
</style>
