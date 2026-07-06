<template>
  <div class="md-body" v-html="html"></div>
</template>

<script setup>
import { computed } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const props = defineProps({
  text: { type: String, default: '' },
})

// GFM：支持 **粗体**、# 标题、列表、表格、代码块、换行等常见 Markdown
marked.setOptions({ gfm: true, breaks: true })

// 渲染后统一用 DOMPurify 消毒，防止 Agent/LLM 产出的内容夹带 XSS
const html = computed(() => {
  const raw = props.text || ''
  if (!raw.trim()) return ''
  const parsed = marked.parse(raw)
  return DOMPurify.sanitize(parsed, { USE_PROFILES: { html: true } })
})

// 外部链接新标签打开
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('href')) {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})
</script>

<style scoped>
/* 紧凑排版，贴合聊天气泡 / 描述块 */
.md-body { font-size: 14px; line-height: 1.7; color: #303133; word-break: break-word; }
.md-body :deep(> *:first-child) { margin-top: 0; }
.md-body :deep(> *:last-child) { margin-bottom: 0; }
.md-body :deep(h1) { font-size: 20px; }
.md-body :deep(h2) { font-size: 17px; }
.md-body :deep(h3) { font-size: 15px; }
.md-body :deep(h1),
.md-body :deep(h2),
.md-body :deep(h3),
.md-body :deep(h4) { font-weight: 700; margin: 14px 0 8px; line-height: 1.4; }
.md-body :deep(p) { margin: 8px 0; }
.md-body :deep(ul),
.md-body :deep(ol) { margin: 8px 0; padding-left: 22px; }
.md-body :deep(li) { margin: 3px 0; }
.md-body :deep(strong) { font-weight: 700; color: #1d2129; }
.md-body :deep(em) { font-style: italic; }
.md-body :deep(a) { color: #409eff; text-decoration: none; }
.md-body :deep(a:hover) { text-decoration: underline; }
.md-body :deep(code) { background: #f0f2f5; padding: 1px 5px; border-radius: 4px;
  font-family: 'Consolas', monospace; font-size: 12.5px; color: #c7254e; }
.md-body :deep(pre) { background: #1e1e1e; color: #d4d4d4; padding: 12px 14px; border-radius: 8px;
  overflow-x: auto; margin: 10px 0; }
.md-body :deep(pre code) { background: none; padding: 0; color: inherit; font-size: 12.5px; }
.md-body :deep(blockquote) { margin: 8px 0; padding: 4px 14px; border-left: 3px solid #dcdfe6;
  color: #606266; background: #fafafa; }
.md-body :deep(table) { border-collapse: collapse; margin: 10px 0; font-size: 13px; }
.md-body :deep(th),
.md-body :deep(td) { border: 1px solid #ebeef5; padding: 6px 10px; }
.md-body :deep(th) { background: #f5f7fa; font-weight: 600; }
.md-body :deep(hr) { border: none; border-top: 1px solid #ebeef5; margin: 14px 0; }
.md-body :deep(img) { max-width: 100%; border-radius: 6px; }
</style>
