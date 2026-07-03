<template>
  <span class="agent-avatar" :style="{ width: size + 'px', height: size + 'px', fontSize: (size * 0.62) + 'px' }">
    <img v-if="url" :src="url" :alt="agent?.name" />
    <span v-else class="emoji">{{ agent?.emoji || '🤖' }}</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'
import { avatarUrl } from '../utils/agentDisplay'

const props = defineProps({
  agent: { type: Object, default: null },
  size: { type: Number, default: 24 },
})
const url = computed(() => avatarUrl(props.agent))
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
