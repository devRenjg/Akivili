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

// GFM：支持 **粗体**、# 标题、列表、表格、代码块、自动裸链接、换行等常见 Markdown
marked.setOptions({ gfm: true, breaks: true })

// 外部链接新标签打开（消毒阶段追加，先注册一次 hook）
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('href')) {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
  // 图片懒加载
  if (node.tagName === 'IMG') {
    node.setAttribute('loading', 'lazy')
  }
})

// 渲染后统一用 DOMPurify 消毒（防 Agent/LLM 产出的 XSS），
// 显式放行图片 <img> 与链接 <a>（含 target/rel），并允许 http/https/mailto/data 图片。
// 保留 DOMPurify 默认 URI 白名单（http/https/mailto/tel/相对路径，覆盖绝大多数链接与图片），
// 仅显式放行 target/rel/loading 属性。默认已允许 <img>/<a>，无需额外 ADD_TAGS。
const SANITIZE_OPTS = {
  USE_PROFILES: { html: true },
  ADD_ATTR: ['target', 'rel', 'loading'],
}
const html = computed(() => {
  const raw = props.text || ''
  if (!raw.trim()) return ''
  const parsed = marked.parse(raw)
  return DOMPurify.sanitize(parsed, SANITIZE_OPTS)
})
</script>

<style scoped>
/* 紧凑排版，贴合聊天气泡 / 描述块 */
.md-body { font-size: 14px; line-height: 1.7; color: #303133; word-break: break-word; }
.md-body :deep(> *:first-child) { margin-top: 0; }
.md-body :deep(> *:last-child) { margin-bottom: 0; }
/* 标题：拉开与正文的字号/字重/颜色差建立主次；h1/h2 带底部细分隔线强化章节感 */
.md-body :deep(h1) { font-size: 21px; color: #0f1c33; padding-bottom: 6px; border-bottom: 2px solid #e4e7ed; }
.md-body :deep(h2) { font-size: 17px; color: #172b4d; padding-bottom: 5px; border-bottom: 1px solid #ebeef5; }
.md-body :deep(h3) { font-size: 15px; color: #24324d; }
.md-body :deep(h4) { font-size: 14px; color: #3a4a63; }
.md-body :deep(h1),
.md-body :deep(h2),
.md-body :deep(h3),
.md-body :deep(h4) { font-weight: 700; margin: 18px 0 9px; line-height: 1.35; letter-spacing: .2px; }
/* 首个标题不顶太多空隙 */
.md-body :deep(> h1:first-child),
.md-body :deep(> h2:first-child),
.md-body :deep(> h3:first-child) { margin-top: 2px; }
.md-body :deep(p) { margin: 8px 0; }
.md-body :deep(ul),
.md-body :deep(ol) { margin: 8px 0; padding-left: 22px; }
.md-body :deep(li) { margin: 4px 0; }
.md-body :deep(li)::marker { color: #909399; }
/* 粗体：作为「字段名/关键项」标签，比正文更深更实 */
.md-body :deep(strong) { font-weight: 700; color: #0f1c33; }
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
