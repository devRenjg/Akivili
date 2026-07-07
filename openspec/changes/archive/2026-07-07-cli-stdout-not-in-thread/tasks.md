## 1. 会话正文来源分流

- [x] 1.1 `runner.execute_dispatch`：final_text 落库处按 `provider.type` 分流——CLI（claude/codex）不落会话消息、API 落
- [x] 1.2 保留 `_RUN_CTX[run_id]["stream_text"] = final_text`（收工写记忆 stdout 兜底不受影响）
- [x] 1.3 确认 stdout 仍全量进 run_logs（`_log(run_id,"stdout",...)` 路径不变）
- [x] 1.4 确认 collab @mention 解析读自身 collected final_text、不依赖会话消息，不受影响

## 2. CLI 未走 jian 交付时打标记（不拿 stdout 兜底）

- [x] 2.1 新增 `runner._has_jian_deliverable`：查本轮 jian comment 消息 + jian subtask/status 活动（兼顾只委派不发言的 Leader，防误判）
- [x] 2.2 CLI 成功但无交付 → `log_activity("commented","system", note="⚠️ …未通过 jian…")`，走既有活动渲染
- [x] 2.3 API 后端不打此标记（stdout 即产出）

## 3. 历史噪声清理

- [x] 3.1 全库精确识别 stdout-mirror 噪声（内容==该 run stdout 拼接 + 同会话同 agent + CLI 供应商，三重判据）
- [x] 3.2 删前备份 `jianagency.db`；每条删前校验同会话同 agent 另有 jian 交付（正文不空）；按精确 msg id 删 9 条，复核残留 0

## 4. 验证

- [x] 4.1 `TestReport/run_stdout_display_probe.py` 8/8（CLI 不落正文但进日志 / 无 jian 打标记 / jian 交付保留且不打标记 / API 照落且不打标记）
- [x] 4.2 回归：QA 套件 28/30、并发探针、子任务探针与改动前基线逐项一致（既有 2 项协同排序失败为 harness 漂移、与本改动无关）；reflect 6/6

## 5. 文档

- [x] 5.1 README 升 v0.14.1，补版本记录
- [x] 5.2 归档 change 到 `openspec/changes/archive/`，同步 `openspec/specs/agent-execution/spec.md`（`openspec validate --specs` 12/12）
