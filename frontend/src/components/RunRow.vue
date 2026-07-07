<template>
  <div class="run-row">
    <!-- 左侧状态图标：hover 显示 Agent 信息 -->
    <el-tooltip :content="agentTip" placement="top" :show-after="200">
      <button class="rr-ico" :class="run.status"
              :disabled="!clickable" @click.stop="clickable && $emit('ctrl')">
        <el-icon v-if="run.status === 'running'"><VideoPause /></el-icon>
        <el-icon v-else-if="run.status === 'failed'"><CircleCloseFilled /></el-icon>
        <el-icon v-else-if="run.status === 'killed'"><RemoveFilled /></el-icon>
        <el-icon v-else><SuccessFilled /></el-icon>
      </button>
    </el-tooltip>

    <!-- 中间：命令缩略版（撑满、截断） -->
    <span class="rr-summary" :title="run.summary || ''">{{ run.summary || agentName }}</span>

    <!-- 右侧：默认显示相对时间；hover 行显示「日志详情」 -->
    <span class="rr-time">{{ time }}</span>
    <span class="rr-detail" title="查看所有命令与运行时详情" @click.stop="$emit('detail')">日志详情</span>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { VideoPause, CircleCloseFilled, RemoveFilled, SuccessFilled } from '@element-plus/icons-vue'
import { displayName } from '../utils/agentDisplay'

const props = defineProps({
  run: { type: Object, required: true },
  agent: { type: Object, default: null },
  time: { type: String, default: '' },
  isAdmin: { type: Boolean, default: false },
})
defineEmits(['ctrl', 'detail'])

const agentName = computed(() => (props.agent ? displayName(props.agent) : props.run.agent_slug || 'Agent'))
// 图标 hover 提示：Agent 名 + 状态
const STATUS_CN = { running: '执行中', succeeded: '已完成', failed: '执行失败', killed: '已终止' }
const agentTip = computed(() => {
  const st = STATUS_CN[props.run.status] || props.run.status
  const act = props.isAdmin && ['running', 'failed', 'killed'].includes(props.run.status)
    ? (props.run.status === 'running' ? ' · 点击终止' : ' · 点击重跑') : ''
  return `${agentName.value} · ${st}${act}`
})
const clickable = computed(() =>
  props.isAdmin && ['running', 'failed', 'killed'].includes(props.run.status))
</script>

<style scoped>
.run-row { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px;
  overflow: hidden; }
.run-row:hover { background: #f5f7fa; }
.rr-ico { flex-shrink: 0; width: 18px; height: 18px; border: none; background: none; padding: 0;
  display: inline-flex; align-items: center; justify-content: center; font-size: 15px; }
.rr-ico.running { color: #f56c6c; cursor: pointer; }
.rr-ico.succeeded { color: #67c23a; }
.rr-ico.failed { color: #f56c6c; cursor: pointer; }
.rr-ico.killed { color: #909399; cursor: pointer; }
.rr-ico:disabled { cursor: default; }
.rr-summary { flex: 1; min-width: 0; font-size: 12px; color: #606266; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; font-family: 'Consolas', monospace; }
.rr-time { flex-shrink: 0; font-size: 11px; color: #c0c4cc; }
/* 「日志详情」默认隐藏，行 hover 时替换时间位置显示 */
.rr-detail { display: none; flex-shrink: 0; font-size: 11px; color: #409eff; cursor: pointer; }
.run-row:hover .rr-time { display: none; }
.run-row:hover .rr-detail { display: inline; }
</style>
