#!/usr/bin/env python3
"""spec_consistency_probe —— 三份 change 规范一致性 gate（Review 第八轮 P1-F + 第九轮加固）。

对 platform-graceful-restart / agent-session-resume / platform-concurrency-scaling
三份 change 的正文做两类静态检查，任一失败即 exit 1，可直接接 CI gate：

  1. 禁止词扫描（forbidden）：旧模型词汇只允许出现在「明确解释其为何被删除/禁止」
     的语境里；每条规则带自己的白名单片段（allow_snippets），SHALL NOT 用「整行
     出现任一标记词就放行」的粗白名单（第九轮：整行放行会漏检同一行里真正的旧口径）。
  2. 结构规则扫描（structural）：跨文档必须成立的强约束——reclaim/交棒 child 前置
     必须是 AND 而非 OR、orphaned 不得直接映射 queued、NULL run 不得创建/复用
     agent_sessions。命中即违规。

第九轮加固点：
  - 任一目标路径不存在 / 文件读取失败 → 直接 exit 1（不再 warning 跳过）。
  - 白名单从「整行匹配任一标记词」改为「每条规则匹配具体旧口径片段」。
  - 新增结构规则（AND-not-OR / orphaned-not-queued / null-run-no-session）。
  - 附带脚本自身单元测试：`python3 spec_consistency_probe.py --self-test`。

第十轮加固点：
  - 结构规则支持 scope='segment'：在句段（按 。；;!？| 切分）内匹配，覆盖长任务行里
    「orphaned … execution 回 queued」这类跨大段文字的旧口径;`回` 用 (?<!不) 排除
    「不回 queued」正确否定;白名单在句段内判定。
  - 新增三元一致性规则：gate 已释放/CLI 已起 时 source 必为 running，写 claimed→orphaned
    即违规;protocol_incompatible 已起 CLI 分支不得回 queued。
  - --self-test 增补真实长任务行、正确三分支长行、gate/source 三元、不可读文件（读取
    失败分支）共 14 例。

第十一轮加固点（三类人工恢复语义 gate）：
  - attempt 层出现 recovery_blocked（只属 execution）即违规。
  - 终态 recovery_blocked 出现出边 →superseded 即违规（人工恢复只建 child、父终态不变）。
  - manual_recovery_token 被误当父级唯一约束即违规（父子基数须 UNIQUE(superseded_from)）。
  - --self-test 增补这三类正负样本。

第十二轮加固点（P1-D：删宽泛白名单退化 + 两条新结构规则）：
  - **删除全局宽泛 STRUCTURAL_ALLOW**（纠正/自相矛盾/删除/不走/永久不变/终态不可逆
    等 sentiment 词）——它们会让「同段含任一 sentiment 词就放行」再次绕过真违规
    （第十一轮我为放行自己新写的解释文本而加，反而开了后门）。
  - 结构规则改为**每条自带精确 allow**（只认与该规则直接对应的否定表述，如
    orphaned→queued 规则只认「SHALL NOT 回 queued」类），与 FORBIDDEN 对齐。
  - 引入显式历史标记 `HISTORICAL_INVALID:`——需要成段引用旧（已废）模型时，该段
    SHALL 以此前缀标注，只有带标记的段对结构规则豁免;杜绝用自然语言 sentiment 词
    隐式豁免。
  - 新增两条结构规则：① NULL task 可执行/固定 full replay（第十二轮 P0-A 已删）;
    ② 父 terminal 后向父事件流追加 manual_recovery（第十二轮 P0-B 已禁）。
  - --self-test 增补 Review 的 5 条绕过负样本 + HISTORICAL_INVALID 标记正负样本 +
    两条新规则正负样本。

第十三轮加固点（P1-E：HISTORICAL_INVALID marker 严格作用域）：
  - 旧实现只判「marker 是否出现在句段中」→ 出现即整段豁免，可被嵌入现行错误规范绕过
    （marker 无引号直跟现行错误 / marker 非段首 / HTML 注释包 marker + 段内现行错误）。
  - 收紧为：marker 必须在句段起始（允许前导空白与 markdown 列表/引用/强调符号），
    后跟受引号（「」『』""''）包裹的历史原文;**只有引号内内容豁免**，引号外现行文字
    重新进入扫描;引用块内含现行规范性措辞（SHALL/MUST/当前实现/现行/要求/必须）则
    判定为「包装现行错误」不豁免。
  - --self-test 增补 P1-E 正负样本，共 40 例。

第十九轮加固点（P1-6：修 6 条 false-negative + 2 条 P0 守卫）：
  - R18-1 verb 组补「命中/直接 replay」、child 限定补「已有」;比较字段随 P1-2 改
    canonical_payload_hash，allow 删裸「SHALL NOT」只绑分流分支短语。
  - R18-2 allow 删裸「SHALL NOT」——否则同句针对别的主语的 SHALL NOT 会放过
    「按最新 execution 终态」真违规;只绑全叶子/优先级否定短语。
  - R18-4 扩匹配「增加/重置」斜杠形、独立「新预算值」「新 budget」「不同新预算值」。
  - 新增 R19-P0-1：running NULL/null migration 确认退出/清理→abandoned 肯定式
    （不要求 attempt 关键词，补 R17-1 漏检）;R19-P0-2：recovery_blocked 父
    「重新排队/回 queued/回队」肯定式（现有 orphaned→queued 规则不覆盖该主语句型）。
  - --self-test 增补 reviewer 6 条 false-negative 负样本 + 两条 P0 规则正负样本。

用法：
    python3 spec_consistency_probe.py             # 扫描默认三份 change
    python3 spec_consistency_probe.py <root> ...  # 扫描指定文件/目录
    python3 spec_consistency_probe.py --self-test # 运行脚本自身单元测试

退出码：0 = 干净 / 自测通过；1 = 发现违规 / 路径缺失 / 读取失败 / 自测失败。
"""
import re
import sys
from pathlib import Path

# ── 禁止词规则 ───────────────────────────────────────────────────────────────
# 每条：pattern=旧口径正则；reason=原因；allow=该规则专属白名单片段（命中这些
# 片段之一才放行，而非整行有任一通用标记词就放行）。allow 为空表示无豁免语境。
FORBIDDEN = [
    dict(pattern=r"(retryable|普通\s*retry|瞬时\s*retry)[^\n]{0,40}(走|转|建|→|->|直接)[^\n]{0,12}recovery child",
         reason="普通 retry 必须走同 execution 新 attempt，不得走 recovery child（recovery child 仅供 supersede/交棒）",
         allow=["SHALL NOT", "不得走 recovery child", "绝不走 recovery child", "不建 recovery child",
                "仅供 supersede", "只用于 supersede", "只用于交棒", "修上一轮", "两套模型"]),
    dict(pattern=r"retry[^\n]{0,20}回队[^\n]{0,20}owner[^\n]{0,6}不\s*retire",
         reason="retry 回队后 owner 必须退休（epoch+1）",
         allow=["SHALL", "必退休", "必须退休", "否则"]),
    dict(pattern=r"无\s*session[^\n]{0,20}落\s*`?failed`?",
         reason="无 session 的 run 不得落 failed，应走 full_replay recovery child",
         allow=["SHALL NOT", "不得", "删除", "删旧", "不再", "而非", "统一", "旧的", "旧「"]),
    dict(pattern=r"NULL\s*conversation[^\n]{0,30}(退化|复用)[^\n]{0,10}task[^\n]{0,6}session",
         reason="NULL conversation task 不可执行（三层硬门），不建/复用 agent_sessions",
         allow=["SHALL NOT", "不可实现", "无法实现", "不建", "不复用", "而非", "不可执行"]),
    dict(pattern=r"(无\s*safe\s*ingestion|做不到\s*context-only)[^\n]{0,40}摘要[^\n]{0,20}(自动完成|正常业务\s*turn)",
         reason="无 safe ingestion 时应转人工，摘要不得冒充自动完成消费",
         allow=["SHALL NOT", "转人工", "不得", "而非", "不算投递"]),
    dict(pattern=r"winning_attempt_id",
         reason="attempt 指针已改名 final_attempt_id（final 非 winning）",
         allow=["final 而非 winning", "final 非 winning", "改名", "命名用", "SHALL NOT"]),
    dict(pattern=r"获胜\s*attempt",
         reason="术语已统一为「定局 attempt」",
         allow=["定局", "改", "统一", "SHALL NOT"]),
    dict(pattern=r"task_runs\.lease_expires_at",
         reason="task_runs 无 lease 字段；lease = run_queue.claim_lease_until / worker_state.lease_expires_at",
         allow=["不含", "无 lease", "契约违规", "视为违规", "SHALL NOT", "无此字段", "禁止", "不存在字段", "不读不存在"]),
    dict(pattern=r"prestart_failed",
         reason="prestart_failed 已归一为 failed + failure_stage=prestart",
         allow=["归一", "取消", "不再", "改", "SHALL NOT", "方案 B", "方案B"]),
    dict(pattern=r"committed_batch_end",
         reason="水位已改名 batch_scan_end",
         allow=["改名", "推翻", "batch_scan_end", "不再", "SHALL NOT", "旧"]),
    dict(pattern=r"jian[^\n]{0,30}只\s*(按|校验|看)\s*generation",
         reason="jian 平台写须 attempt 级 fencing（generation+instance+attempt/execution/current pointer）",
         allow=["SHALL NOT", "不足", "挡不住", "升级", "只看 generation 挡", "只校验 generation"]),
    dict(pattern=r"start_new_session[^\n]{0,30}(冒充|等价|替代)[^\n]{0,20}(suspended|launch\s*gate|启动闸门|containment)",
         reason="start_new_session 不得冒充 CAS 前启动闸门",
         allow=["SHALL NOT", "不冒充", "不得", "不能", "非 `start_new_session", "[非 start_new_session", "非 start_new_session"]),
]

# ── 结构规则（跨文档强约束，命中即违规，无白名单） ───────────────────────────
# 每条：pattern=违规写法正则；reason=原因；scope='line'(默认整行) 或 'segment'(句段)。
# scope='segment'（第十轮）：在句段内匹配——句段按分隔符 。；;!？ 与 markdown 表格
# 竖线 | 切分，使「orphaned … 回 queued」这类长任务行里同一句段内的旧口径也能被
# 捕获，而不被 20 字符窗口漏掉;同时避免跨句段误报（相邻两句各自合法却被连读）。
# 这些是「必须成立/必须不出现」的硬约束。
# 第十二轮 P1-D：删全局 sentiment 白名单。每条结构规则自带精确 allow（只认与该
# 规则直接对应的否定表述）;需成段引用旧模型时，该段 SHALL 用显式 HISTORICAL_INVALID:
# 前缀标注（见 HISTORICAL_INVALID_MARKER），只有带标记的段豁免——杜绝「同段含任一
# sentiment 词就放行」再次开后门。
STRUCTURAL = [
    # 交棒/reclaim child 前置若写成「确认停 或 fencing」OR 口径即违规（须 AND）。
    dict(pattern=r"(已确认停|确认停止|旧执行已确认停)\s*或\s*(已被\s*)?fencing",
         reason="交棒/reclaim child 前置必须是 AND（fencing AND 进程树确认退出），不得用 OR",
         scope="line",
         allow=["SHALL NOT 用 OR", "不得用 OR", "而非 OR", "SHALL NOT 写", "不得写"]),
    dict(pattern=r"或先(完成|做)\s*(generation\s*)?fencing",
         reason="child 前置不得写「或先完成 generation fencing」OR 口径（仅 fencing 挡不住残留 CLI 副作用）",
         scope="line",
         allow=["SHALL NOT", "不得", "而非", "收紧"]),
    # orphaned 不得直接映射/回到 queued（会导致新 Worker 重领 → 双执行）。句段级匹配。
    # `回` 前用 (?<!不) 排除「不回 queued」这类正确否定表述;`不得回/SHALL NOT 回` 由 allow 兜底。
    dict(pattern=r"orphaned[^。；;!？|]*?((?<!不)回|→|->|映射到?|落)\s*`?queued`?",
         reason="orphaned（未确认死亡）不得回 queued，须 recovery_blocked(process_not_confirmed_dead)",
         scope="segment",
         allow=["SHALL NOT 回 queued", "SHALL NOT 回 `queued`", "不得回 queued", "不得回 `queued`",
                "SHALL NOT 回队", "不回 queued"]),
    dict(pattern=r"`?orphaned`?\s*(与|和)\s*`?abandoned`?[^。；;!？|]*?(都|均)[^。；;!？|]*?(映射|(?<!不)回|落)[^。；;!？|]*?`?queued`?",
         reason="orphaned 与 abandoned 不得都映射为 queued（前者进程未确认退出，不安全）",
         scope="segment",
         allow=["SHALL NOT", "不得", "不都"]),
    # protocol_incompatible 已起 CLI 分支不得回 queued（句段级覆盖长任务行）。
    dict(pattern=r"protocol[_\s]?incompatible[^。；;!？|]*?(已起|CLI 已启动|gate 已释放)[^。；;!？|]*?((?<!不)回|→|->)\s*`?queued`?",
         reason="protocol_incompatible 已起 CLI/gate 已释放分支不得回 queued（应 orphaned+recovery_blocked，防双执行）",
         scope="segment",
         allow=["SHALL NOT 回 queued", "SHALL NOT 回 `queued`", "不得回 queued", "SHALL NOT 回队"]),
    # 三元一致性：gate 已释放/CLI 已起的分支若把 source 写成 claimed→orphaned 即违规
    # （gate 释放在 CAS 转 running 之后，source 必为 running），第十轮 P1-1。
    dict(pattern=r"(gate 已释放|CLI 已起|CLI 已启动)[^。；;!？|]*?`?claimed`?\s*(→|->)\s*`?orphaned`?",
         reason="gate 已释放/CLI 已起时 source 必为 running（running→orphaned），不得写 claimed→orphaned",
         scope="segment",
         allow=["SHALL NOT 写 claimed", "不得写 claimed", "source 必为 running", "非 claimed"]),
    dict(pattern=r"`?claimed`?\s*(→|->)\s*`?orphaned`?[^。；;!？|]*?(gate 已释放|CLI 已起|CLI 已启动)",
         reason="gate 已释放/CLI 已起时 source 必为 running（running→orphaned），不得写 claimed→orphaned",
         scope="segment",
         allow=["SHALL NOT 写 claimed", "不得写 claimed", "source 必为 running", "非 claimed"]),
    # 第十一轮 P1-D：三类人工恢复语义 gate。
    # (1) attempt 层出现未定义状态 recovery_blocked（它只属 execution）。句段级匹配
    #     「attempt … recovery_blocked」同段共现。
    # R7 收紧（第十二轮）：只在「attempt 层/状态/终态」紧邻（≤4 字）recovery_blocked 时
    # 才判违规，杜绝「attempt/execution 终态、…、落 recovery_blocked」这类跨大段的误报。
    dict(pattern=r"attempt\s*(层\s*)?(状态|终态|status)[^。；;!？|]{0,4}`?recovery_blocked",
         reason="recovery_blocked 只属 execution，SHALL NOT 出现在 attempt 层状态/终态",
         scope="segment",
         allow=["SHALL NOT", "只属 execution", "不属 attempt", "不是 attempt"]),
    # R8 收紧（第十二轮）：只在 recovery_blocked **直接**「是/为/作为 attempt」时判违规，
    # 杜绝「recovery_blocked 是 execution 终态，不是 attempt 状态」这类正确表述被 bridge 误报。
    dict(pattern=r"`?recovery_blocked`?\s*(是|为|作为)\s*`?attempt",
         reason="recovery_blocked 只属 execution，SHALL NOT 定义为 attempt 状态/终态",
         scope="segment",
         allow=["SHALL NOT", "只属 execution", "不属 attempt", "不是 attempt"]),
    # (2) recovery_blocked 出现出边 →superseded（终态不可逆，人工恢复只建 child 不改父）。
    dict(pattern=r"`?recovery_blocked`?\s*(→|->)\s*`?superseded`?",
         reason="终态 recovery_blocked 不得有出边 →superseded（人工恢复只建 child、父终态不变）",
         scope="segment",
         allow=["SHALL NOT", "不得", "终态不可逆", "父终态不变", "只建 child"]),
    dict(pattern=r"父[^。；;!？|]*?`?recovery_blocked`?\s*(→|->|改写?为|变(成|为))\s*`?superseded`?",
         reason="父 recovery_blocked 终态不可逆，不得改写为 superseded（人工恢复只建 child）",
         scope="segment",
         allow=["SHALL NOT", "不得", "终态不可逆", "永久不变", "只建 child"]),
    # (3) manual_recovery_token 被误当父级唯一约束（父子基数必须 UNIQUE(superseded_from)）。
    dict(pattern=r"UNIQUE\s*\([^)]*manual_recovery_token[^)]*\)[^。；;!？|]*?(保证|防|挡)[^。；;!？|]*?(一父|父.*child|至多.*child|双续|重复 child)",
         reason="父子基数须 UNIQUE(superseded_from)，manual_recovery_token 只做请求幂等、不承担父子唯一",
         scope="segment",
         allow=["SHALL NOT", "不得", "而非", "不承担", "只做请求幂等", "挡不住"]),
    # 第十二轮 P1-D 新增结构规则 ①：NULL task 可执行/固定 full replay（P0-A 已删该口径）。
    # 收紧：NULL/空 conversation/task 紧邻（≤8 字）肯定式「可执行/固定 full replay/走 task
    # 兜底执行」才判违规;「不可执行/不设可执行/非可执行」等否定由 (?<!不)(?<!设) 前视 + allow 排除。
    dict(pattern=r"(NULL|空)\s*(conversation|task)[^。；;!？|]{0,8}(固定\s*full[_\s]?replay|走\s*task\s*兜底[^。；;!？|]{0,4}执行|退化[^。；;!？|]{0,6}执行|(?<!不)(?<!非)(?<!设)可执行(?!必))",
         reason="NULL conversation 的 task 不可执行（三层硬门），无「固定 full replay」可执行口径（第十二轮 P0-A）",
         scope="segment",
         allow=["SHALL NOT", "不可执行", "不得", "而非", "已删", "删旧", "非可执行", "不代表可执行",
                "不设可执行", "仅防", "只作", "只防", "拒绝执行", "存量脏数据", "必须拥有非 NULL"]),
    # 第十二轮 P1-D 新增结构规则 ②：父 terminal 后向父事件流追加 manual_recovery（P0-B 已禁）。
    # 收紧：需出现「父流/父事件流 + 追加/append/写/发 + manual_recovery」的肯定式才判违规;
    # 「manual_recovery 走 child 流/独立审计/不追加父流」等正确表述由 allow 排除。
    dict(pattern=r"(父\s*(事件)?流|父\s*execution\s*事件流)[^。；;!？|]{0,20}(追加|append|写入?|发)[^。；;!？|]{0,12}manual_recovery",
         reason="父 terminal 后事件流已封闭，SHALL NOT 向父流追加 manual_recovery（走 child 流首事件/审计表，第十二轮 P0-B）",
         scope="segment",
         allow=["SHALL NOT", "不得", "不追加", "而非", "封闭", "child 流", "child 事件流", "独立审计"]),
    # 第十五轮 P1-1 新增结构规则 ③：自动 supersede/reclaim 写序里写了「父 superseded → child queued」
    # 却漏掉 child recovery_resumed(source=reclaim)。收紧：同一句段内出现「superseded」+「child/子」+
    # 「queued」相邻描述写序（→/->/箭头连接）但**整段不含 recovery_resumed** 才判违规;
    # 正确写序（含 recovery_resumed）与「不含/无 recovery_resumed 的否定说明」由 allow / 负向前视排除。
    dict(pattern=r"superseded[^。；;!？|]*?(→|->)[^。；;!？|]*?(child|子)[^。；;!？|]*?queued(?![^。；;!？|]*recovery_resumed)(?<!recovery_resumed)",
         reason="自动 supersede/reclaim 建 child 的写序 SHALL 含 child recovery_resumed(source=reclaim)，在父 superseded 与 child queued 之间（第十五轮 P1-1）",
         scope="segment",
         allow=["SHALL NOT", "不得", "而非", "recovery_resumed", "无 per-execution", "独立序列", "重置游标",
                "全局 id", "全局 Last-Event-ID", "较大", "无损", "断线", "重连"]),
    # 第十五轮 P1-1 新增结构规则 ④：SSE event payload 用了 recovery_source（应为 source）。
    # recovery_source 只应出现在 HTTP 响应体;出现「SSE/event/事件 + payload/字段 + recovery_source」判违规。
    dict(pattern=r"(SSE|event|事件)[^。；;!？|]{0,12}(payload|字段|载荷)[^。；;!？|]{0,12}recovery_source",
         reason="SSE event payload 字段用 source（manual|reclaim），recovery_source 仅 HTTP 响应体字段（第十五轮 P1-1）",
         scope="segment",
         allow=["SHALL NOT", "不得", "而非", "非 recovery_source", "仅 HTTP", "HTTP 响应"]),
    # ── 第十七轮新增结构规则（7 条，守护本轮 P0/P1/P2 修复不回归） ──
    # R17-1（P0）：running NULL / null migration 确认退出后 attempt 恒 orphaned，SHALL NOT 改写为 abandoned。
    dict(pattern=r"(running\s*NULL|null[_\s]?conversation[_\s]?migration|process_cleanup_state\s*=?\s*confirmed|已确认(完整)?(进程树)?退出)[^。；;!？|]*?attempt[^。；;!？|]{0,10}(落|→|->|改写?为|变(成|为))\s*`?abandoned`?",
         reason="running NULL/null migration 确认退出后 attempt 恒 orphaned（第十七轮 P0 终态不可逆），SHALL NOT 改写为 abandoned",
         scope="segment",
         allow=["SHALL NOT", "恒 orphaned", "恒 `orphaned`", "不改写", "永久保持", "永久 orphaned", "claimed", "不再随"]),
    # R17-2（P1-B）：orphaned 的 blocked_reason 按来源子类分，SHALL NOT 恒/一律映射 process_not_confirmed_dead。
    dict(pattern=r"orphaned[^。；;!？|]*?(恒|一律|都|统一|均)[^。；;!？|]*?process_not_confirmed_dead",
         reason="orphaned 的 blocked_reason 按来源子类分（unsafe→process_not_confirmed_dead / running NULL→null_conversation_migration），SHALL NOT 恒映射 process_not_confirmed_dead（第十七轮 P1-B）",
         scope="segment",
         allow=["SHALL NOT", "不得", "按来源子类", "子类", "两子类", "而非", "不假定"]),
    # R17-3（P1-C）：final=NULL 唯一例外 SHALL 引用持久列 terminal_source_status，SHALL NOT 引用运行期 source_status。
    dict(pattern=r"final[^。；;!？|]{0,16}NULL[^。；;!？|]*?(?<!terminal_)source_status\s*=?\s*queued",
         reason="final=NULL 唯一例外 SHALL 引用持久不可变列 terminal_source_status，SHALL NOT 引用运行期 source_status（第十七轮 P1-C）",
         scope="segment",
         allow=["terminal_source_status", "SHALL NOT", "不可变", "非运行期", "固化"]),
    # R17-4（P1-A）：null_conversation_migration 父的 resolved 派生认 migration_from_execution_id，非 superseded_from。
    dict(pattern=r"null[_\s]?conversation[_\s]?migration[^。；;!？|]*?(resolved|resumed|已恢复|移出待办|派生)[^。；;!？|]*?superseded_from",
         reason="null_conversation_migration 父的 resolved 派生认 migration_from_execution_id（非 superseded_from），否则永久 unresolved（第十七轮 P1-A）",
         scope="segment",
         allow=["migration_from", "SHALL NOT", "而非 superseded_from", "不挂 superseded_from", "不认 superseded_from", "同时认"]),
    # R17-5（P1-D）：不同 token 输家 SHALL 按 payload 分流（一致映射 existing / 不同 payload 返 409），不得无条件映射到赢家。
    dict(pattern=r"不同\s*token[^。；;!？|]*?(读回|返回|映射到?|→|->)[^。；;!？|]*?(existing|已存在|赢家)[^。；;!？|]*?(child|successor)",
         reason="不同 token 输家 SHALL 按 canonical_payload_hash 分流——一致才映射 existing child/successor、不一致返 409 already_resolved（第十七轮 P1-D / 第十九轮 P1-2），SHALL NOT 无条件映射到赢家",
         scope="segment",
         # allow 绑定「按 payload 分流」的具体否定/分支短语。SHALL NOT 收 idempotent_replay——它在违规与
         # 正确两形都出现无法区分;也 SHALL NOT 只凭出现 SHALL NOT 就整段放行（第十九轮 P1-6）
         allow=["payload 一致", "payload 不同", "canonical_payload_hash", "409", "already_resolved", "分两种", "按 payload", "同意图"]),
    # R17-6（P1-E）：人工补预算只保留 grant_delta，SHALL NOT 用「写新预算值/覆盖 remaining」旧口径。
    dict(pattern=r"(新预算值|新预算)[^。；;!？|]{0,8}(写入|覆盖|设置|设为|填入)",
         reason="人工补预算只保留 grant_delta（budget_remaining += grant_delta），SHALL NOT 用「写新预算值/覆盖 remaining」旧口径（绝对覆盖走独立 admin override，第十六轮 P1-E#3 / 第十七轮 P1-E）",
         scope="segment",
         allow=["grant_delta", "SHALL NOT", "admin override", "绝对覆盖", "独立 admin", "旧口径"]),
    # R17-7（P2-B）：reclaim 交棒承接 superseded 父无 blocked_reason，SHALL NOT 假定 recovery_resumed 恒带 blocked_reason。
    dict(pattern=r"(reclaim|superseded\s*父|交棒父)[^。；;!？|]*?(恒|必|一律|都|必带|必填)[^。；;!？|]*?blocked_reason",
         reason="reclaim 交棒承接 superseded 父无 blocked_reason，recovery_resumed 的 blocked_reason 仅承接 recovery_blocked 父时必填（第十七轮 P2-B）",
         scope="segment",
         allow=["SHALL NOT", "缺省", "仅承接 recovery_blocked", "无 blocked_reason", "不假定", "为 null", "可选", "非必填"]),
    # ── 第十八轮新增结构规则（4 条，守护本轮 P1 修复不回归） ──
    # R18-1（req 幂等）：UNIQUE(superseded_from) 冲突后无条件读回已存在 child（不分 payload）即违规——须按 payload 分流。
    # 第十九轮 P1-6：verb 组补「命中/直接 replay/idempotent_replay」，child 限定补「已有」——
    # 覆盖 reviewer 负样本「不同 token 命中已有 child 直接 replay」这类 R18-1 原正则漏检句型。
    dict(pattern=r"(UNIQUE\s*\(\s*superseded_from\s*\)|唯一冲突)[^。；;!？|]*?(读回|返回|映射到?|命中|直接\s*replay)[^。；;!？|]*?(已存在|已有|existing)\s*child(?![^。；;!？|]*payload)",
         reason="UNIQUE(superseded_from) 冲突后 SHALL 按 canonical_payload_hash 分流（一致→existing child/idempotent_replay、不一致→409 already_resolved），SHALL NOT 无条件读回/命中已有 child（第十八轮 P1-req / 第十九轮 P1-2/P1-6）",
         scope="segment",
         # allow 绑定「按 payload 分流」的具体分支短语。SHALL NOT 收 idempotent_replay——它在违规
         # （无条件 replay）与正确（分流后一致 replay）两形都出现、无法区分（第十九轮 P1-6）。
         allow=["payload 一致", "payload 不同", "比对", "canonical_payload_hash", "409", "already_resolved", "分两种", "分流", "分三"]),
    # R18-2（task 聚合）：无 active 取「单个/最新」终态 execution 判完成度即违规——须按全叶子优先级聚合。
    # 第十九轮 P1-6：allow 删除裸「SHALL NOT」——否则同句里针对别的主语（如 superseded_from）的 SHALL NOT
    # 会放过前半句「按最新 execution 终态」的真违规;allow 只绑定与本规则直接对应的全叶子/优先级否定短语。
    dict(pattern=r"(无\s*active|task\s*完成度|任务整体完成度|完成度)[^。；;!？|]*?(取|按)[^。；;!？|]*?(最新|单个|一条)[^。；;!？|]*?(终态\s*)?execution",
         reason="task 完成度 SHALL 构造全部因果叶子按优先级(active>unresolved>失败>完成)聚合，SHALL NOT 取单个最新终态 execution(会掩盖较早 unresolved lineage)（第十八轮 P1-agg / 第十九轮 P1-6 收紧 allow）",
         scope="segment",
         allow=["全部叶子", "全叶子", "全部因果叶子", "所有因果叶子", "优先级", "不掩盖", "同优先级内", "选代表", "tie-break", "SHALL NOT 取", "SHALL NOT 用「取", "非取最新"]),
    # R18-3（protocol 定局性）：protocol_incompatible abandoned 恒/一律定局即违规——须按预算拆。
    dict(pattern=r"protocol[_\s]?incompatible[^。；;!？|]*?(恒|一律|均|都|始终)[^。；;!？|]*?(定局|final)",
         reason="protocol_incompatible abandoned 定局性 SHALL 按预算拆(未耗尽=非定局回queued/耗尽=定局recovery_blocked)，SHALL NOT 一律判定局（第十八轮 P1-proto）",
         scope="segment",
         allow=["SHALL NOT", "按预算拆", "未耗尽", "预算耗尽", "非定局", "分两支"]),
    # R18-4（grant_delta）：出现「增加或重置 / 增加/重置 / 写新预算值 / 新预算值 / 新 budget / 覆盖 remaining」
    # 肯定式即违规——只保留 grant_delta。第十九轮 P1-6：扩匹配「增加/重置」（斜杠形）、独立「新预算值」、
    # 「新 budget」「不同新预算值」等 R18-4 原正则漏掉的真实残余;allow 删裸「SHALL NOT」，只绑 grant_delta 口径短语。
    # 注：`新 budget 行/表/记录`＝为新 chain 建新预算行（合法），非「写新预算值」旧措辞——用负向前视排除;
    # `child 带新 budget`（旧模糊表述）仍需命中，故只排除紧跟 行/表/记录/_remaining 的用法。
    dict(pattern=r"(增加或重置|增加/重置|重置/增加|写\s*新预算值|(?<!post-)(?<!补足后)新\s*预算值|不同\s*新预算值|(?<!观察到\s)(?<!post-grant\s)新\s*budget(?!_remaining)(?!\s*行)(?!\s*表)(?!\s*记录)|覆盖\s*budget_remaining|覆盖\s*remaining|重置某级预算|重置具体某级预算)",
         reason="人工补预算只保留 grant_delta 唯一语义，SHALL NOT 用「增加或重置/增加/重置/写新预算值/新预算值/新 budget/覆盖 remaining」旧措辞（绝对覆盖走独立 admin override，第十八轮 P1-budget / 第十九轮 P1-6 扩匹配）",
         scope="segment",
         window=True,  # 第二十轮 P1-6：allow 只认命中处**前** 24 字窗口，尾随「…并携带 grant_delta」等无关 allow 不再放行。
         allow=["grant_delta", "admin override", "绝对覆盖", "已删", "删「", "旧措辞", "旧口径", "而非", "SHALL NOT 出现", "SHALL NOT 用"]),
    # ── 第十九轮新增 P0 守卫规则（2 条，防两个 P0 复发——不再重复发生 P0） ──
    # R19-P0-1：running NULL / null migration「确认退出/confirmed」→ abandoned 的肯定式（不要求出现 attempt
    #   关键词，覆盖 R17-1 因缺 attempt 而漏检的 reviewer 负样本「running null_conversation_migration 已确认退出 -> abandoned」）。
    #   running NULL 恒 orphaned、清理确认只翻 process_cleanup_state，SHALL NOT 落/改写为 abandoned。
    # 动词前用 (?<!不)(?<!不再)(?<!未)(?<!SHALL NOT ) 排除正确否定「不落/不再落/未落 abandoned」;
    # 改写为 abandoned 的「改写为」也用前视排除「不改写为」。allow 再兜底常见正确口径。
    dict(pattern=r"(running\s*NULL|null[_\s]?conversation[_\s]?migration|process_cleanup_state\s*=?\s*confirmed|已确认(完整)?(进程树)?(退出|清理)|确认清理后?)[^。；;!？|]{0,24}((?<!不)(?<!不再)(?<!未)落|→|->|(?<!不)改写?为|变(成|为)|(?<!不)转为?|判定?为|计入)\s*`?abandoned`?",
         reason="running NULL/null migration 确认退出/清理后 attempt 恒 orphaned（终态不可逆），SHALL NOT 落/改写为 abandoned——清理确认只翻 process_cleanup_state（第十九轮 P0-1）",
         scope="segment",
         window=True,  # 第二十轮 P1-6：allow 只认命中处**前** 24 字窗口，尾随「，SHALL NOT 丢审计」等无关 allow 不再放行。
         allow=["SHALL NOT", "恒 orphaned", "恒 `orphaned`", "不改写", "不落 abandoned", "不再落 abandoned", "不再落 `abandoned`",
                "永久 orphaned", "只翻 process_cleanup_state", "仅 claimed NULL", "claimed NULL", "claimed·CLI 未起",
                "而非 abandoned", "不得改写", "不再落", "无 running"]),
    # R19-P0-2：recovery_blocked 父「重新排队/回 queued/回队/再入队」的肯定式（终态无出边、父永久不变，
    #   只能原子建 queued recovery child）。覆盖 reviewer 负样本「recovery_blocked 确认清理后允许重新排队」——
    #   现有 orphaned→queued 规则不覆盖以 recovery_blocked 为主语的「重新排队」句型。
    dict(pattern=r"`?recovery_blocked`?[^。；;!？|]{0,40}((?<!不)重新\s*排队|(?<!不)再\s*入队|(?<!不)回\s*队|(?<!不)回\s*`?queued`?|(?<!不)重新\s*入队)",
         reason="recovery_blocked 是终态无出边、父永久不变，SHALL NOT 重新排队/回 queued——只允许原子创建 queued recovery child（superseded_from=父）承接（第十九轮 P0-2）",
         scope="segment",
         window=True,  # 第二十轮 P1-6：allow 只认命中处**前** 24 字窗口，尾随「，SHALL NOT 丢 pending」不再放行。
         allow=["SHALL NOT", "不得", "永久不变", "终态无出边", "只允许原子创建", "只建 child", "只原子建",
                "承接续跑", "而非重新排队", "不重新排队", "不回 queued", "不回队"]),
    # ── 第二十轮新增跨真相源结构规则（4 条，守护本轮 P1-2/P1-3/P1-4 收口不回归） ──
    # R20-1（P1-2 lineage 归一）：history_backlog_from_execution_id 作为**独立列/字段**存在即违规——
    #   history backlog 承接已归一入 continuation_from_execution_id + continuation_kind=history_backlog，
    #   不再单列。仅在明确说明「归一/不再单列/归入 continuation」语境放行。
    dict(pattern=r"history_backlog_from_execution_id",
         reason="history backlog 因果承接已归一入 continuation_from_execution_id + continuation_kind=history_backlog，SHALL NOT 再用独立列 history_backlog_from_execution_id（第二十轮 P1-2）",
         scope="segment",
         # 第二十轮 P1-6 自查：R20-1 是**死标识符**规则（同 FORBIDDEN 的 winning_attempt_id/
         # committed_batch_end——字段名本身即信号），合法弃用说明常在**前一句段**，故保持整段 allow
         # 不加 window（before-window 会漏掉跨句段的「替代旧 …」说明而误报真实 proposal 行）。
         # 死标识符不会被「顺手」写出、误放行风险低，与既有 FORBIDDEN 字段规则同粒度。
         allow=["归一", "不再单列", "归入 continuation", "continuation_from_execution_id", "改用", "已删", "旧字段", "不复用", "替代旧", "替代"]),
    # R20-2（P1-2 聚合口径）：「task 级 active 聚合 / task 级 active execution 聚合」旧口径即违规——
    #   完成度/优先级判定 SHALL 走全部因果叶子按优先级聚合，非 task 级单一 active 聚合。
    dict(pattern=r"task\s*级\s*active(\s*execution)?\s*聚合",
         reason="完成度/优先级 SHALL 构造全部因果叶子按优先级聚合，SHALL NOT 用「task 级 active 聚合」旧口径（会掩盖较早 unresolved lineage，第二十轮 P1-2）",
         scope="segment",
         window=True,  # 第二十轮 P1-6 自查：allow 只认命中处前窗口，防尾随「全叶子/而非」放行前半旧口径。
         allow=["SHALL NOT 用「task", "而非 task 级", "非 task 级", "全叶子", "全部因果叶子", "改为全叶子", "已删"]),
    # R20-3（P1-3 resolved 真相源唯一）：resolved「cache 必写 / 必须写 cache / cache 是真相源」旧口径即违规——
    #   resolved 唯一权威=因果键 EXISTS 纯派生，recovery_operations/cache 均非真相源，cache 纯派生默认。
    dict(pattern=r"(resolved\s*)?cache\s*(必写|必须写|恒写|是真相源|作(为)?真相源|当真相源)",
         reason="resolved 唯一权威=因果键 EXISTS 纯派生，cache 是纯派生默认（可选加速），SHALL NOT 把 cache 当真相源/必写（第二十轮 P1-3）",
         scope="segment",
         window=True,  # 第二十轮 P1-6 自查：allow 只认命中处前窗口，防尾随「纯派生/可选」放行前半「cache 必写/当真相源」。
         allow=["纯派生", "非真相源", "不是真相源", "可选", "SHALL NOT 必写", "而非必写", "默认纯派生", "已删"]),
    # R20-4（P1-4 值域）：terminal_source_status='unknown' / =unknown 即违规——
    #   值域恒为 {queued,claimed,running} 三值，推断不了入独立 quarantine 隔离表、列保持 NULL，不引入 unknown。
    dict(pattern=r"terminal_source_status\s*(=|＝|:|为|∈[^。；;!？|]{0,12})[^。；;!？|]{0,6}unknown",
         reason="terminal_source_status 值域恒为 {queued,claimed,running} 三值，推断不了入独立 terminal_source_backfill_quarantine 隔离表、列保持 NULL，SHALL NOT 引入 unknown 值（第二十轮 P1-4）",
         scope="segment",
         window=True,  # 第二十轮 P1-6 自查：allow 只认命中处前窗口，防尾随「SHALL NOT/quarantine/隔离表」放行前半「=unknown」。
         allow=["不含 unknown", "无 unknown", "非 unknown", "不引入 unknown", "SHALL NOT 引入", "而非 unknown", "已删",
                "值域恒", "值域三值", "三值"]),
    # ── 第二十一轮新增跨真相源结构规则（3 条：R21-1a/1b/1c + R21-2，守护本轮 2 个 P0 收口不回归） ──
    # R21-1a（P0-1 提交顺序安全游标）：把「全局自增 id / BIGSERIAL / IDENTITY / MAX(messages.id)」
    #   **充当已提交水位/续传游标**即违规——PG identity 在 INSERT 时分配、不等提交，late-commit 小 id
    #   会被 reader 永久越过。真相源是 per-execution `event_seq`（run_queue 行锁）/ conversation
    #   `message_seq`（conversations 行锁）。specs 里合法出现均为「SHALL NOT 用全局…作水位/游标」否定式
    #   或 SQLite 过渡说明，靠命中处**前** 24 字窗口 allow 放行。
    dict(pattern=r"(全局\s*(自增\s*)?(`?id`?|BIGSERIAL|IDENTITY)|MAX\s*\(\s*messages\.id\s*\))[^。；;!？|]{0,30}(作为?|充当|当作?|直接充当|用作|视作|拿来?作)[^。；;!？|]{0,12}(已提交\s*)?(水位|游标|watermark|cursor)",
         reason="全局自增 id/BIGSERIAL/IDENTITY/MAX(messages.id) SHALL NOT 充当已提交水位/续传游标——PG identity 在 INSERT 时分配不等提交、late-commit 小 id 被 reader 永久越过；真相源=per-execution event_seq（run_queue 行锁）/ conversation message_seq（conversations 行锁）（第二十一轮 P0-1）",
         scope="segment",
         window=True,  # allow 只认命中处前 24 字窗口，防尾随「…SHALL NOT/而非/过渡」放行前半「全局 id 作水位」。
         # 「若用/仍用/如仍用/如用」= P0-1 根因解释 + concurrency 检测 WHEN 子句的假设/检测标记
         # （描述「用全局 id 作水位会被越过」以将其禁止），非规范性主张，属合法否定语境。
         allow=["SHALL NOT", "不用", "不作", "不充当", "不得用", "而非", "非全局", "过渡", "SQLite", "late-commit",
                "会被", "永久越过", "替代", "已删", "不等提交", "根因", "若用", "仍用", "如仍用", "如用", "不一致"]),
    # R21-1b（P0-1 跨 execution 可比假设）：断言「child … 全局 id 必然大于/更大 父」即违规——
    #   per-execution `event_seq` 下父子事件序不需要跨 execution 可比，late-commit 下该全局假设不成立。
    #   死短语式（bare 断言即信号），合法弃用说明常在同段（「…是全局游标的产物…SHALL NOT 再保留…归一」），
    #   故用整段 allow（同 R20-1，不加 window——避免前窗口够不到句首的「旧的/产物/归一」而误报弃用句）。
    dict(pattern=r"child[^。；;!？|]{0,12}全局\s*(`?id`?|Last-Event-ID)[^。；;!？|]{0,8}必然[^。；;!？|]{0,6}(大于|更大)",
         reason="per-execution event_seq 下父子事件序不需跨 execution 可比，SHALL NOT 断言「child 全局 id 必然大于父」（late-commit 下不成立，第二十一轮 P0-1）——切 child execution_id 从 child event_seq 起点回放",
         scope="segment",
         allow=["SHALL NOT", "而非", "归一", "不再依赖", "不依赖", "不成立", "产物", "已删", "替代",
                "late-commit", "废弃", "旧口径", "旧的", "不需"]),
    # R21-1c（P0-1 successor 续订游标）：断言「携带（当前/父的）全局 Last-Event-ID **续订/续传/回放/切 child**」
    #   即违规——successor 订阅 SHALL 切 child execution_id、从 child `event_seq` 起点回放，不复用父的跨
    #   execution 全局游标。要求动词后缀（续订/续传/续跑/回放/订阅/切）——specs 里「携带…全局 Last-Event-ID
    #   且/或依赖」等描述/否定式不带该动词后缀、天然不命中;真正的续订复用旧口径才命中。window 兜底。
    dict(pattern=r"携带\s*(当前\s*|父的?\s*)?(较大\s*)?全局\s*`?Last-Event-ID`?[^。；;!？|]{0,10}(续订|续传|续跑|回放|订阅|切\s*child|切\s*successor)",
         reason="successor 续订 SHALL 切 child execution_id、从 child event_seq 起点回放，SHALL NOT 携带父的跨 execution 全局 Last-Event-ID 续订（per-execution 游标口径归一，第二十一轮 P0-1）",
         scope="segment",
         window=True,
         allow=["SHALL NOT", "不携带", "不复用", "而非", "不再", "归一", "旧口径", "废弃", "旧的", "产物"]),
    # R21-2（P0-2 并集唯一真相源）：断言「三分列 partial unique + 中心化 NOT EXISTS **保证并集唯一/一父
    #   至多一后继**」即违规——PG READ COMMITTED 下两事务各插不同列、命中不同 partial unique 均可提交，
    #   NOT EXISTS 看不到对方未提交行，会建多后继。跨键并集唯一真相源=`execution_edges` 边表
    #   UNIQUE(parent_execution_id)（三类 successor helper 同事务先插边表再建 child）。要求 partial unique
    #   + NOT EXISTS + 唯一性断言宾语（并集唯一/一父至多一后继/跨键唯一）三者共现——specs 里
    #   「SHALL NOT 仅靠…NOT EXISTS」「不再依赖中心化 NOT EXISTS」等否定式无该断言宾语、且 window 兜底。
    dict(pattern=r"(三(个|分)?列|分列)?\s*partial\s*unique[^。；;!？|]{0,20}(\+|加|与|及|和)?[^。；;!？|]{0,10}NOT\s*EXISTS[^。；;!？|]{0,28}(保证|挡住?|防住?|使|令|够|足以|确保)[^。；;!？|]{0,14}(并集唯一|一父至多一(个)?后继|跨键(并集)?唯一|唯一后继|至多一后继|一父一子)",
         reason="跨键「一父至多一后继」并集唯一真相源=execution_edges 边表 UNIQUE(parent_execution_id)（三类 successor helper 同事务先插边表再建 child），SHALL NOT 靠「三分列 partial unique + 中心化 NOT EXISTS」保证——PG READ COMMITTED 下两事务各插不同列均可提交、NOT EXISTS 看不到未提交行会建多后继（第二十一轮 P0-2）",
         scope="segment",
         window=True,
         allow=["SHALL NOT", "不(依赖|靠|够|能)", "仅靠", "不再依赖", "而非", "挡不住", "不成立", "会建",
                "READ COMMITTED", "execution_edges", "边表", "已删", "不足"]),
]

# 显式历史标记（第十二轮 P1-D 引入，第十三轮 P1-E 收紧作用域）：需引用已废旧模型时，
# 用 `HISTORICAL_INVALID:` 前缀 + 紧随的**受引号包裹的历史原文**标注，只有引号内的历史
# 原文对结构规则豁免;引号外的现行文字**重新进入扫描**，SHALL NOT 整段/整行豁免。
#
# 第十三轮 P1-E 之前的实现只判「marker 是否出现在句段中」→ 出现即整段豁免，可被绕过：
#   HISTORICAL_INVALID: 当前规范要求父 recovery_blocked→superseded。   ← 无引号、含现行措辞，旧实现放行
# 收紧后：
#   ① marker 必须在句段**行首/块首**（前面只允许空白或 markdown 列表/引用符号）。
#   ② marker 后必须紧跟受引号（「」『』"" ''）包裹的历史原文;只有引号**内**内容豁免。
#   ③ 豁免块（marker + 引号原文）内 SHALL NOT 含现行规范性措辞（SHALL/MUST/当前实现/
#      现行/要求/必须）——含则视为「拿历史标记包装现行错误」，不豁免。
#   ④ 引号之外（同段其余现行文字）照常扫描。
HISTORICAL_INVALID_MARKER = "HISTORICAL_INVALID:"

# marker 必须出现在句段开头（允许前导空白 + markdown 列表/引用/强调符号），
# 后跟受引号包裹的历史原文。引号内内容是唯一豁免范围。
_HISTORICAL_INVALID_RE = re.compile(
    r"^[\s>*\-`（(]*" + re.escape(HISTORICAL_INVALID_MARKER)
    + r"\s*[「『\"'“‘]([^」』\"'”’]*)[」』\"'”’]"
)
# 豁免块内不得出现的现行规范性措辞（出现则不豁免——防历史标记包装现行错误）。
_CURRENT_NORMATIVE_MARKERS = ["SHALL", "MUST", "当前实现", "现行", "要求", "必须", "当前必须", "当前规范"]


def _historical_exempt_segment(seg):
    """判断某句段是否为「合法的独立历史引用」——仅此情形对结构规则豁免。

    返回 (exempt, remainder)：
      exempt=True 表示该段以合法 HISTORICAL_INVALID:「历史原文」开头、且引用块无现行措辞;
      remainder = 去掉引用块后的**剩余现行文字**，仍需照常扫描（第十三轮 P1-E）。
      exempt=False 时 remainder 为原段（marker 不合法即完全不豁免、整段照常扫描）。
    """
    m = _HISTORICAL_INVALID_RE.match(seg.strip())
    if not m:
        return (False, seg)
    quoted = m.group(1)
    # 引用块内含现行规范性措辞 → 判定为拿历史标记包装现行错误，不豁免。
    if any(mk in quoted for mk in _CURRENT_NORMATIVE_MARKERS):
        return (False, seg)
    # 合法历史引用：只豁免引号内原文，返回引号之后的剩余现行文字继续扫描。
    remainder = seg.strip()[m.end():]
    return (True, remainder)

# 行级 meta 语境标记：命中的行是「描述本 probe 自身/清理清单」的元文档，会成段
# 罗列旧口径作为清理目标，整行豁免（仅限这类自述行，不是通用整行放行）。
META_CONTEXT_MARKERS = ["spec_consistency_probe", "全文清理", "禁止词/字段清单", "禁止词清单"]

DEFAULT_CHANGES = [
    "platform-graceful-restart",
    "agent-session-resume",
    "platform-concurrency-scaling",
]

# probe 伴生文档：这些 .md **本身就在编目/解释旧口径**（回归清单、门禁说明），
# 不是 spec 正文，逐行扫描必然大面积误报。目录级 rglob 时按 basename 跳过它们;
# 若显式作为参数传入单文件则仍扫描（便于针对性调试）。第二十轮 P1-6 引入。
EXCLUDED_BASENAMES = {"REGRESSION_GATE.md"}


def resolve_targets(argv):
    """解析待扫描的 .md 文件列表 + 缺失路径列表。

    返回 (files, missing)。第九轮加固：缺失路径不再静默跳过，交由调用方 exit 1。
    第二十轮：目录扫描时跳过 EXCLUDED_BASENAMES（probe 伴生编目文档）。
    """
    if argv:
        roots = [Path(a) for a in argv]
    else:
        # 本脚本位于 <repo>/openspec/changes/platform-graceful-restart/scripts/
        changes_dir = Path(__file__).resolve().parents[2]
        roots = [changes_dir / name for name in DEFAULT_CHANGES]

    files, missing = [], []
    for root in roots:
        if not root.exists():
            missing.append(root)
            continue
        if root.is_file():
            files.append(root)
        else:
            files.extend(sorted(p for p in root.rglob("*.md")
                                if p.name not in EXCLUDED_BASENAMES))
    return files, missing


# 句段分隔符（第十轮 scope='segment'）：中文/英文句读 + markdown 表格竖线。
# 不切顿号/括号——否则会把「orphaned（已起）… execution 回 queued」的主谓拆散而漏检;
# 正确否定「不回 queued」由模式内 (?<!不) lookbehind 排除，白名单再兜底。
_SEGMENT_SPLIT = re.compile(r"[。；;!？|]")


def _segments(line):
    """把一行切成句段，供 scope='segment' 规则在段内匹配、避免跨句误报。"""
    return [seg for seg in _SEGMENT_SPLIT.split(line) if seg.strip()]


# 第二十轮 P1-6：allow 局部绑定窗口（**opt-in**，仅对易被绕过的规则开启）。
# 根因：默认的 `any(snip in scan_target for snip in allow)` 把 allow 片段与**整个句段**
# 比对——只要同句任意位置出现无关的 SHALL NOT / grant_delta，就整段放行，被 reviewer
# 实证可绕过若干守卫（如「普通恢复增加/重置预算，并携带 grant_delta。」——grant_delta
# 是对合法 admin 分支的描述，却放行了同句「增加/重置」旧口径）。
# 观测规律：**合法否定语境几乎都在命中处之前**（「只保留 grant_delta … SHALL NOT 用增加/
# 重置」「SHALL NOT 因确认清理改写为 abandoned」「无 running…→abandoned 表述」），而
# reviewer 实证的绕过语境（尾随 grant_delta / 「，SHALL NOT 丢 pending」）都在命中处**之后**。
# 故对这几条规则用**非对称窗口**：只认命中处之前 WINDOW_BEFORE 字符内的 allow，命中处之后
# 一律不认（after=0）——尾随的无关 allow 再也放不了行。其余规则保持整段 allow（默认行为，
# 第十九轮及之前语义不变），避免误伤 rename 说明/probe 自述/HISTORICAL_INVALID 引用等长句。
_ALLOW_WINDOW_BEFORE = 24


def _match_exempt_windowed(scan_target, pattern, allow):
    """第二十轮 P1-6：非对称局部绑定 allow（before=WINDOW_BEFORE, after=0）。

    对 pattern 的**每一处**命中，只在 [start-BEFORE, end] 窗口内找 allow 片段：
      - 无命中 → True（无违规）。
      - allow 为空 → 有命中即 False。
      - 任一命中的前窗口内无 allow → False（真违规）。
      - 所有命中的前窗口内都有 allow → True（合法否定语境，豁免）。
    仅供开了 window 标志的规则使用;命中处之后的 allow 一律不认，杜绝尾随 allow 绕过。
    """
    matches = list(re.finditer(pattern, scan_target))
    if not matches:
        return True
    if not allow:
        return False
    for m in matches:
        lo = max(0, m.start() - _ALLOW_WINDOW_BEFORE)
        window = scan_target[lo:m.end()]
        if not any(snip in window for snip in allow):
            return False
    return True


def _allowed(scan_target, rule):
    """按规则是否开 window 标志，选择「命中处前窗口 allow」或「整段 allow」。"""
    allow = rule.get("allow", [])
    pattern = rule["pattern"]
    if rule.get("window"):
        return _match_exempt_windowed(scan_target, pattern, allow)
    # 默认：整段 allow（第十九轮及之前语义）。有命中且无 allow 片段则不豁免。
    if not re.search(pattern, scan_target):
        return True
    return any(snip in scan_target for snip in allow)


def scan_line(line):
    """返回该行命中的 (kind, reason) 违规列表。

    kind='forbidden' 走每条规则的专属片段白名单;kind='structural' 走结构白名单。
    第九轮：白名单改为「具体片段匹配」，SHALL NOT 整行出现任一通用标记词就放行。
    第十轮：structural 规则支持 scope='segment'——在句段内匹配长任务行，段内命白名单才放行。
    """
    # meta 自述行（描述 probe/清理清单本身）整行豁免——这类行必然罗列旧口径。
    if any(marker in line for marker in META_CONTEXT_MARKERS):
        return []
    hits = []
    for rule in FORBIDDEN:
        # 第二十轮 P1-6：window 规则走命中处前窗口 allow，其余走整段 allow（_allowed）。
        if not _allowed(line, rule):
            hits.append(("forbidden", rule["reason"]))
    for rule in STRUCTURAL:
        scope = rule.get("scope", "line")
        if scope == "segment":
            # 逐句段匹配：某句段命中 pattern，且该句段（或命中处前窗口）内既无本规则专属
            # allow 片段、也非合法独立历史引用块，才算违规（第十二轮删全局 sentiment
            # 白名单;第十三轮 P1-E：HISTORICAL_INVALID 只豁免引号内历史原文、剩余现行
            # 文字仍扫描;第二十轮 P1-6：window 规则改命中处前窗口绑定，其余保持整段）。
            hit = False
            for seg in _segments(line):
                exempt, remainder = _historical_exempt_segment(seg)
                # 合法历史引用块：引号内豁免，只对剩余现行文字判违规。
                scan_target = remainder if exempt else seg
                if not _allowed(scan_target, rule):
                    hit = True
                    break
            if hit:
                hits.append(("structural", rule["reason"]))
        else:
            exempt, remainder = _historical_exempt_segment(line)
            scan_target = remainder if exempt else line
            if not _allowed(scan_target, rule):
                hits.append(("structural", rule["reason"]))
    return hits


def scan_text(text):
    """扫描整段文本，返回 [(lineno, kind, reason, line), ...]。供单元测试直接调用。"""
    out = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, reason in scan_line(line):
            out.append((lineno, kind, reason, line.strip()))
    return out


def _reconfig_utf8():
    # Windows 控制台默认 GBK，正文含 emoji/中文，统一切到 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv):
    _reconfig_utf8()

    files, missing = resolve_targets(argv)
    if missing:
        print("❌ 目标路径不存在（第九轮：缺失即失败，不跳过）:", file=sys.stderr)
        for m in missing:
            print(f"   {m}", file=sys.stderr)
        return 1
    if not files:
        print("❌ 没有可扫描的文件", file=sys.stderr)
        return 1

    violations = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 第九轮：读取失败直接 exit 1，不再仅 warning。
            print(f"❌ 读取失败（视为违规）: {f}: {exc}", file=sys.stderr)
            return 1
        for lineno, kind, reason, snippet in scan_text(text):
            violations.append((f, lineno, kind, reason, snippet))

    print("📊 spec_consistency_probe")
    print(f"   扫描文件数: {len(files)}")
    print(f"   违规命中数: {len(violations)}")

    if not violations:
        print("✅ 未发现非白名单语境的旧口径或结构违规")
        return 0

    print("\n❌ 发现违规:")
    for f, lineno, kind, reason, snippet in violations:
        print(f"   [{kind}] {f}:{lineno}")
        print(f"     原因: {reason}")
        print(f"     行内: {snippet[:120]}")
    return 1


# ── 脚本自身单元测试（第九轮 P1-F 放行条件） ─────────────────────────────────
def _self_test():
    """验证真旧句被拦、混合错误句被拦、正确句放行、缺失路径/不可读文件失败。"""
    _reconfig_utf8()
    cases = []

    def check(name, cond):
        cases.append((name, bool(cond)))

    # 1. 真旧句：普通 retry 走 recovery child（无白名单）→ 命中 forbidden
    hits = scan_text("普通 retry 场景下 attempt 直接走 recovery child 续跑。")
    check("真旧句 retry→recovery child 被拦", any(k == "forbidden" for _, k, _, _ in hits))

    # 2. 白名单语境：明确说「不得走 recovery child」→ 放行
    hits = scan_text("普通 retry SHALL NOT 走 recovery child（recovery child 仅供 supersede）。")
    check("白名单语境 retry 放行", not hits)

    # 3. winning_attempt_id 裸引用 → 命中；解释性改名句 → 放行
    check("winning_attempt_id 裸引用被拦",
          any(k == "forbidden" for _, k, _, _ in scan_text("消费者读 winning_attempt_id 得定局 attempt。")))
    check("winning_attempt_id 改名说明放行",
          not scan_text("命名用 final 而非 winning，故不再用 winning_attempt_id。"))

    # 4. 结构规则：orphaned 回 queued → 命中
    check("orphaned→queued 结构违规被拦",
          any(k == "structural" for _, k, _, _ in scan_text("protocol mismatch 时 orphaned 回 queued 等兼容 Worker。")))
    # 5. 结构规则白名单：说明「不得回 queued」→ 放行
    check("orphaned 不得回 queued 放行",
          not scan_text("orphaned SHALL NOT 回 queued（防双执行）。"))

    # 6. 结构规则：OR 前置口径 → 命中
    check("child 前置 OR 口径被拦",
          any(k == "structural" for _, k, _, _ in scan_text("旧执行已确认停 或 已被 fencing 且清理时才建 child。")))

    # 7. 混合错误句：一行同时含通用标记词 + 真旧句用法 → 仍命中（整行放行已废除）
    hits = scan_text("我们不再用别的字段，直接读 winning_attempt_id 当定局指针。")
    check("混合错误句不被整行白名单绕过", any(k == "forbidden" for _, k, _, _ in hits))

    # 8. 缺失路径 → main 返回 1
    check("缺失路径 exit 1", main(["/nonexistent/path/xyz-should-not-exist"]) == 1)

    # 9. resolve_targets 对缺失返回 missing 而非静默跳过
    _, missing = resolve_targets(["/nonexistent/path/xyz"])
    check("resolve_targets 报告缺失路径", len(missing) == 1)

    # 10. 真实长任务行：orphaned…（大量中间文字）…execution 回 queued → 句段级仍命中
    long_line = (
        "- [ ] 1.6b finish_execution：protocol_incompatible claimed→attempt 落 "
        "abandoned（未起 CLI）/orphaned（已起）、execution 回 queued 等兼容 Worker、"
        "owner 退休、pending 保留，仅反复不兼容且预算耗尽才 recovery_blocked"
    )
    check("长任务行 orphaned→queued 被句段级捕获",
          any(k == "structural" for _, k, _, _ in scan_text(long_line)))

    # 11. 长行三分支正确写法（gate 已释放→running→orphaned→recovery_blocked 不回队）→ 放行
    good_long = (
        "claimed·gate 未释放→abandoned+queued；gate 已释放/CLI 已起时 attempt 已 running，"
        "走 running→orphaned+recovery_blocked，SHALL NOT 回 queued（防双执行）"
    )
    check("正确三分支长行放行", not any(k == "structural" for _, k, _, _ in scan_text(good_long)))

    # 12. 三元一致性：gate 已释放却写 claimed→orphaned → 命中
    check("gate 已释放写 claimed→orphaned 被拦",
          any(k == "structural" for _, k, _, _ in scan_text("gate 已释放时 claimed→orphaned 落 recovery_blocked")))

    # 13. 不可读文件 → main 返回 1（第十轮：覆盖读取失败分支，非仅缺失路径）
    import tempfile
    tmpdir = tempfile.mkdtemp()
    bad = Path(tmpdir) / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00\x00 invalid utf-8 \xc0\xc1")
    check("不可读文件 exit 1", main([str(bad)]) == 1)
    try:
        bad.unlink()
        Path(tmpdir).rmdir()
    except OSError:
        pass

    # 14. 第十一轮 P1-D：attempt 层出现 recovery_blocked → 命中
    check("attempt 层 recovery_blocked 被拦",
          any(k == "structural" for _, k, _, _ in scan_text("attempt 状态 recovery_blocked→superseded 人工恢复")))
    # 15. 终态 recovery_blocked→superseded 出边 → 命中
    check("recovery_blocked→superseded 出边被拦",
          any(k == "structural" for _, k, _, _ in scan_text("人工恢复时父 recovery_blocked→superseded 并建 child")))
    # 16. 正确口径：父 recovery_blocked 永久不变、只建 child → 放行
    check("父终态不变只建 child 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "父 recovery_blocked 永久不变，SHALL NOT 改 superseded，只原子建一个 superseded_from=父 的 child")))
    # 17. manual_recovery_token 误当父级唯一约束 → 命中
    check("manual_recovery_token 冒充父级唯一约束被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "UNIQUE(recovery_chain_id, manual_recovery_token) 保证一父至多一个 child")))
    # 18. 正确口径：UNIQUE(superseded_from) 保证父子基数 → 放行
    check("UNIQUE(superseded_from) 父子基数放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "UNIQUE(superseded_from) 保证一父至多一个 child，token 只做请求幂等")))

    # ── 第十二轮 P1-D：删全局 sentiment 白名单后，5 条绕过负样本必须被拦 ──
    # 这些句子含旧 sentiment 词（纠正/删除/不走/自相矛盾/终态不可逆），旧版本会整段放行、
    # 放过其中真违规;新版本每规则精确 allow + HISTORICAL_INVALID 显式标记后必须 CAUGHT。
    bypass_cases = [
        "纠正旧模型后，orphaned 回 queued 等兼容 Worker。",
        "删除冗余字段，父 recovery_blocked→superseded 后建 child。",
        "不走老路径，attempt 状态 recovery_blocked 直接落终态。",
        "自相矛盾已消除，UNIQUE(recovery_chain_id, manual_recovery_token) 保证一父至多一个 child。",
        "终态不可逆原则下，gate 已释放时 claimed→orphaned 落 recovery_blocked。",
    ]
    for i, bc in enumerate(bypass_cases, start=1):
        check(f"P1-D sentiment 绕过负样本{i} 被拦",
              any(k == "structural" for _, k, _, _ in scan_text(bc)))

    # 19. HISTORICAL_INVALID 显式标记段：成段引用旧模型 → 放行（唯一成段豁免）
    # 第十三轮 P1-E 收紧：marker 必须在句段起始才豁免;段首 marker + 引号原文 → 放行。
    check("HISTORICAL_INVALID 段首标记块放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "删旧口径；HISTORICAL_INVALID:「orphaned 一律回 queued」")))
    # 19b. P1-E：marker 非句段起始（前有现行文字）→ 不再豁免，段内违规仍被拦
    check("HISTORICAL_INVALID 非段首不豁免被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "SHALL NOT 保留旧 HISTORICAL_INVALID:「orphaned 一律回 queued」的口径")))
    # 20. 无标记的同类旧口径 → 仍被拦（marker 必须显式书写）
    check("无 HISTORICAL_INVALID 标记的旧口径仍被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "保留旧的 orphaned 一律回 queued 口径继续跑。")))

    # 21. 新规则①：NULL task 固定 full replay 可执行 → 命中
    check("NULL task 固定 full replay 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "某 NULL conversation 的 run 固定 full replay 执行不复用 session。")))
    # 22. 正确口径：NULL task 不可执行 → 放行
    check("NULL task 不可执行放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "conversation_id IS NULL 的 task 一律不可执行，NULL 组索引仅防存量脏数据并发。")))
    # 22b. NULL task 直接说「可执行」→ 命中（裸「可执行」触发，(?<!不)(?<!非)(?<!设) 排除否定）
    check("NULL task 可执行被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "某 NULL conversation 的 task 可执行并复用 session。")))
    # 22c. 正确不变量标题「可执行 task 必须拥有非 NULL」→ 放行（(?!必) + allow 双重排除）
    check("可执行必有真 conversation 标题放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "可执行 task 必须拥有非 NULL conversation，NULL/quarantined task 一律不可执行。")))

    # 23. 新规则②：父流追加 manual_recovery → 命中
    check("父流追加 manual_recovery 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "人工恢复时向父事件流追加 manual_recovery 事件让客户端发现 child。")))
    # 24. 正确口径：manual_recovery 走 child 流首事件、不追加父流 → 放行
    check("manual_recovery 走 child 流放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "manual_recovery 作为 child 事件流首个控制事件，SHALL NOT 追加到已封闭的父流。")))

    # ── 第十三轮 P1-E：HISTORICAL_INVALID marker 严格作用域，反嵌入绕过 ──
    # 25. marker 后无引号、直接跟现行错误规范 → 不豁免、命中
    check("P1-E marker 无引号跟现行错误被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "HISTORICAL_INVALID: 当前规范要求父 recovery_blocked→superseded。")))
    # 26. marker 非句段起始（前有现行文字）+ 引号后现行错误 → 不豁免、命中
    check("P1-E marker 非段首+引号后现行错误被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "说明 HISTORICAL_INVALID:「旧口径」后，当前实现父 recovery_blocked→superseded。")))
    # 27. HTML 注释包装 marker + 段内现行错误 → 现行部分仍命中
    check("P1-E 注释包装 marker 现行错误仍被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "<!-- HISTORICAL_INVALID: orphaned 回 queued --> 当前必须 orphaned 回 queued。")))
    # 28. 合法独立历史引用块（段首 marker + 引号原文，无现行措辞）→ 放行
    check("P1-E 合法独立历史块放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "删除旧口径；HISTORICAL_INVALID:「claimed→abandoned/orphaned 一律回 queued」")))
    # 29. 合法历史块引号内含违规词（recovery_blocked→superseded）→ 仍放行（引号内豁免）
    check("P1-E 历史块引号内违规词放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "纠正自相矛盾；HISTORICAL_INVALID:「复用 finish_execution + 父 recovery_blocked→superseded」")))
    # 30. marker + 引号内含现行规范性措辞（SHALL）→ 判定为包装现行错误、不豁免命中
    check("P1-E 历史块引号内含 SHALL 不豁免被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "HISTORICAL_INVALID:「orphaned 回 queued SHALL 保留」")))
    # 31. 合法历史块之后同段追加现行错误 → 现行部分仍被扫描命中
    check("P1-E 历史块后现行错误仍被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "HISTORICAL_INVALID:「旧模型」 当前 orphaned 回 queued 继续跑")))
    # 32. 第十五轮 P1-1：自动 supersede 写序漏 recovery_resumed → 命中
    check("P1-1 自动写序漏 recovery_resumed 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "自动 supersede：父 superseded → child queued → commit")))
    # 33. 正确写序含 recovery_resumed(reclaim) → 放行
    check("P1-1 含 recovery_resumed 写序放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "自动 supersede：父 superseded → child recovery_resumed(source=reclaim) → child queued → commit")))
    # 34. superseded→child queued 但用「全局游标续订」描述（无写序语义）→ 放行
    check("P1-1 superseded 全局游标续订放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "收到父 superseded 后订阅 child、携带全局 Last-Event-ID 无损取到 child queued")))
    # 35. 第十五轮 P1-1：SSE event payload 用 recovery_source → 命中
    check("P1-1 SSE payload recovery_source 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "SSE event payload 带 recovery_source 区分来源")))
    # 36. recovery_source 只在 HTTP 响应 → 放行
    check("P1-1 recovery_source 仅 HTTP 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "recovery_source 仅 HTTP 响应体字段、SSE event payload 用 source 而非 recovery_source")))

    # ── 第十七轮新增自测（7 条规则各正负样本） ──
    # 37. R17-1（P0）：running NULL 确认退出后 attempt 落 abandoned → 命中
    check("R17-1 running NULL 确认退出 attempt→abandoned 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "running NULL 已确认退出后 attempt 落 abandoned 并路由 migration")))
    # 38. R17-1 正确口径：attempt 恒 orphaned 不改写 → 放行
    check("R17-1 attempt 恒 orphaned 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "running NULL 已确认退出后 attempt 恒 orphaned 不改写，只翻 process_cleanup_state=confirmed")))
    # 39. R17-2（P1-B）：orphaned 恒映射 process_not_confirmed_dead → 命中
    check("R17-2 orphaned 恒 process_not_confirmed_dead 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "orphaned 一律 process_not_confirmed_dead 驱动 recovery_blocked")))
    # 40. R17-2 正确口径：按子类分 → 放行
    check("R17-2 orphaned 子类分放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "orphaned 的 blocked_reason 按来源子类分，SHALL NOT 恒映射 process_not_confirmed_dead")))
    # 41. R17-3（P1-C）：final=NULL 例外引用运行期 source_status=queued → 命中
    check("R17-3 final=NULL 引用 source_status 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "该终态 final_attempt_id=NULL 合法（例外：recovery_blocked AND null_conversation_migration AND source_status=queued）")))
    # 42. R17-3 正确口径：引用 terminal_source_status → 放行
    check("R17-3 terminal_source_status 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "final=NULL 例外引用固化不可变列 terminal_source_status=queued，非运行期 source_status")))
    # 43. R17-4（P1-A）：null migration resolved 认 superseded_from → 命中
    check("R17-4 null migration resolved 认 superseded_from 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "null_conversation_migration 父的 resolved 派生查 superseded_from 判断是否已恢复")))
    # 44. R17-4 正确口径：认 migration_from → 放行
    check("R17-4 null migration 认 migration_from 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "null_conversation_migration 父的 resolved 派生认 migration_from_execution_id 而非 superseded_from")))
    # 45. R17-5（P1-D）：不同 token 无条件读回赢家 child → 命中
    check("R17-5 不同 token 无条件映射赢家被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "不同 token 并发时读回已存在 child 返回 idempotent_replay")))
    # 46. R17-5 正确口径：按 payload 分流 → 放行
    check("R17-5 按 payload 分流放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "不同 token 输家按 payload 分两种：payload 一致读回 existing child、payload 不同返 409 already_resolved")))
    # 47. R17-6（P1-E）：写新预算值/覆盖 → 命中
    check("R17-6 写新预算值被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "把人工给的新预算值写入该 recovery chain 的预算记录")))
    # 48. R17-6 正确口径：grant_delta → 放行
    check("R17-6 grant_delta 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "按唯一 grant_delta>0 补预算（budget_remaining += grant_delta），绝对覆盖走独立 admin override")))
    # 49. R17-7（P2-B）：reclaim 父恒带 blocked_reason → 命中
    check("R17-7 reclaim 父恒带 blocked_reason 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "reclaim 交棒的 recovery_resumed 必带 blocked_reason 供消费者识别")))
    # 50. R17-7 正确口径：superseded 父无 blocked_reason → 放行
    check("R17-7 superseded 父无 blocked_reason 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "reclaim 交棒承接 superseded 父时无 blocked_reason（缺省/null），仅承接 recovery_blocked 父时必填")))

    # ── 第十八轮新增自测（4 条规则各正负样本） ──
    # 51. R18-1：UNIQUE(superseded_from) 冲突无条件读回已存在 child → 命中
    check("R18-1 无条件读回已存在 child 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "UNIQUE(superseded_from) 冲突后读回并返回已存在 child、idempotent_replay=true")))
    # 52. R18-1 正确口径：按 payload 分流 → 放行
    check("R18-1 按 payload 分流放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "UNIQUE(superseded_from) 冲突后先比对 payload，payload 一致读回已存在 child、payload 不同返 409 already_resolved")))
    # 53. R18-2：无 active 取单个最新终态 execution → 命中
    check("R18-2 取单个最新终态 execution 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "无 active 时 task 完成度取最新终态 execution 按 created_at DESC")))
    # 54. R18-2 正确口径：全叶子优先级聚合 → 放行
    check("R18-2 全叶子优先级聚合放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "task 完成度构造所有因果叶子按优先级 active>unresolved>失败>完成 聚合，SHALL NOT 取单个最新")))
    # 55. R18-3：protocol_incompatible 恒定局 → 命中
    check("R18-3 protocol_incompatible 恒定局被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "abandon_reason=protocol_incompatible 一律定局、被 final 引用")))
    # 56. R18-3 正确口径：按预算拆 → 放行
    check("R18-3 protocol 按预算拆放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "protocol_incompatible 按预算拆：未耗尽预算非定局回 queued、预算耗尽定局 recovery_blocked")))
    # 57. R18-4：增加或重置预算 / 写新预算值 → 命中
    check("R18-4 增加或重置预算被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "人工增加或重置预算写入该 recovery chain")))
    # 58. R18-4 正确口径：只 grant_delta → 放行
    check("R18-4 grant_delta 唯一放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "人工补预算只保留 grant_delta 唯一语义，SHALL NOT 用增加或重置旧措辞")))

    # ── 第十九轮 P1-6：reviewer 实证的 6 条 false-negative 负样本，全部必须 CAUGHT ──
    # 这些句子是 reviewer 直接调用 scan_text() 返回空列表（漏检）的真实残余口径;
    # 扩匹配 R18-1/R18-2/R18-4 + 新增 R19-P0-1/P0-2 后，六条必须全部命中。
    fn_samples = [
        "recovery_budget_exhausted 需要明确增加/重置具体某级预算",              # R18-4 增加/重置 斜杠形
        "task 完成度按最新 execution 终态，SHALL NOT 只沿 superseded_from",       # R18-2 裸 SHALL NOT 不再放行
        "UNIQUE(superseded_from) 冲突后不同 token 命中已有 child 直接 replay",     # R18-1 命中/replay/已有 child
        "running null_conversation_migration 已确认退出 -> abandoned",             # R19-P0-1 无 attempt 关键词
        "recovery_blocked 确认清理后允许重新排队",                                 # R19-P0-2 重新排队肯定式
        "不同 payload（如不同新预算值）并发恢复",                                  # R18-4 不同新预算值
    ]
    for i, s in enumerate(fn_samples, start=1):
        check(f"P1-6 false-negative 负样本{i} 被拦",
              any(k == "structural" for _, k, _, _ in scan_text(s)))

    # 59. R19-P0-1 正确口径：running NULL 恒 orphaned 不改写 → 放行
    check("R19-P0-1 running NULL 恒 orphaned 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "running NULL 确认清理后 attempt 恒 orphaned、不改写为 abandoned，只翻 process_cleanup_state=confirmed")))
    # 60. R19-P0-1：仅 claimed NULL 落 abandoned 的正确窄口径 → 放行
    check("R19-P0-1 仅 claimed NULL abandoned 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "仅 claimed NULL·CLI 未起无残留进程才落 abandoned，running NULL 恒 orphaned 不改写")))
    # 61. R19-P0-2 正确口径：recovery_blocked 永久不变、只建 child → 放行
    check("R19-P0-2 recovery_blocked 只建 child 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "父 recovery_blocked 永久不变，SHALL NOT 重新排队/回 queued，只允许原子创建 queued recovery child 承接")))
    # 62. R19-P0-2：recovery_blocked 明确「不回 queued」→ 放行（(?<!不) + allow 双重排除）
    check("R19-P0-2 recovery_blocked 不回 queued 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "recovery_blocked 终态无出边，父不回 queued、不重新排队")))

    # ── 第二十轮 P1-6：allow 局部窗口绑定——3 条尾随 allow 混合绕过句必须 CAUGHT ──
    # reviewer 实证：旧整段 allow 下，同句尾随的无关 grant_delta / SHALL NOT 会整段放行、
    # 放过前半句真违规。window=True 后 allow 只认命中处前 24 字窗口，尾随 allow 不再放行。
    windowed_bypass = [
        "普通恢复增加/重置预算，并携带 grant_delta。",              # R18-4：尾随 grant_delta 不再放行前半「增加/重置」
        "recovery_blocked 父回 queued，SHALL NOT 丢 pending。",       # R19-P0-2：尾随「SHALL NOT 丢 pending」不放行「回 queued」
        "running NULL 已确认退出后落 abandoned，SHALL NOT 丢审计。",  # R19-P0-1：尾随「SHALL NOT 丢审计」不放行「落 abandoned」
    ]
    for i, s in enumerate(windowed_bypass, start=1):
        check(f"P1-6 尾随 allow 混合绕过句{i} 被拦",
              any(k == "structural" for _, k, _, _ in scan_text(s)))

    # 63. R18-4 命中处**前**窗口内的 grant_delta 仍正常放行（前置合法否定语境不误伤）
    check("R18-4 前置 grant_delta 语境放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "只保留 grant_delta 唯一语义，SHALL NOT 用增加/重置旧措辞")))
    # 64. R19-P0-1 命中处**前**窗口内的 SHALL NOT（跨「因确认清理」）仍放行（spec:989 真实句型）
    check("R19-P0-1 前置 SHALL NOT 因确认清理放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "process_cleanup_state 为 confirmed 均指同一 orphaned attempt、SHALL NOT 因确认清理改写为 abandoned")))
    # 65. R19-P0-1 meta「无 running…→abandoned 表述」自述句放行（tasks:66 真实句型）
    check("R19-P0-1 无 running→abandoned 表述放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "tasks/spec/design 无 running·已确认退出→abandoned 表述）")))

    # ── 第二十轮新增 4 条跨真相源结构规则（R20-1~4）各正负样本（6 条真实残留 + 正向守护） ──
    # 66. R20-1：history_backlog_from_execution_id 作为独立列 → 命中
    check("R20-1 history_backlog_from_execution_id 独立列被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "successor 携带 trigger=history_backlog / history_backlog_from_execution_id 指向上批")))
    # 67. R20-1 正确口径：归一入 continuation_from_execution_id → 放行
    check("R20-1 归一入 continuation 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "history backlog 承接归一入 continuation_from_execution_id + continuation_kind=history_backlog，替代旧 history_backlog_from_execution_id 单列")))
    # 68. R20-2：task 级 active 聚合旧口径 → 命中
    check("R20-2 task 级 active 聚合被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "普通 successor 另由 task 级 active 聚合判定完成度")))
    # 69. R20-2 正确口径：全因果叶子优先级聚合 → 放行
    check("R20-2 全因果叶子聚合放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "普通 successor 由全因果叶子优先级聚合判定，而非 task 级单值 active 聚合")))
    # 70. R20-3：resolved cache 必写/当真相源 → 命中
    check("R20-3 resolved cache 必写被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "resolved 状态 cache 必写，消费者直接读 cache 判 resolved")))
    # 71. R20-3 正确口径：cache 纯派生非真相源 → 放行
    check("R20-3 cache 纯派生放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "resolved 唯一权威=因果键 EXISTS 纯派生，cache 是纯派生默认、非真相源、可选加速")))
    # 72. R20-4：terminal_source_status='unknown' → 命中
    check("R20-4 terminal_source_status unknown 被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "推断不了时 terminal_source_status=unknown 兜底")))
    # 73. R20-4 正确口径：值域三值 + quarantine 隔离 → 放行
    check("R20-4 值域三值 quarantine 放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "terminal_source_status 值域恒为 queued/claimed/running 三值，推断不了入 quarantine 隔离表、列保持 NULL，不引入 unknown")))

    # ── 第二十轮 P1-6 收尾自查：R20-2/3/4 也须 window 绑定——尾随无关 allow 不得放行前半旧口径 ──
    # （自查发现这三条初版用整段 allow，可被「…旧口径，全叶子/纯派生/quarantine 另说」尾随绕过，
    #   与本轮要修的 P1-6 同源;加 window=True 后必须 CAUGHT。R20-1 是死标识符规则，保持整段。）
    r20_tail_bypass = [
        "task 级 active 聚合判定完成度，全叶子另说。",              # R20-2 尾随「全叶子」
        "resolved cache 必写，纯派生另说。",                        # R20-3 尾随「纯派生」
        "terminal_source_status=unknown 兜底，quarantine 另说。",   # R20-4 尾随「quarantine」
    ]
    for i, s in enumerate(r20_tail_bypass, start=1):
        check(f"R20-{i+1} 尾随 allow 绕过被拦",
              any(k == "structural" for _, k, _, _ in scan_text(s)))

    # ── 第二十一轮新增 R21-1a/1b/1c + R21-2 各正负样本（本轮 2 个 P0 的真实残留 + 正向守护） ──
    # R21-1a（P0-1 提交顺序安全游标）：全局 id / MAX(messages.id) 充当水位/游标 → 命中
    check("R21-1a 全局自增 id 充当已提交水位被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "SSE 续传用全局自增 id 直接充当已提交水位、reader 之后只查 id > 大id")))
    check("R21-1a MAX(messages.id) 作水位被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "session 增量用 MAX(messages.id) 作水位、committed_msg_id 推进")))
    # R21-1a 正确口径：per-execution event_seq / message_seq 行锁 → 放行
    check("R21-1a per-execution event_seq 行锁放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "SHALL NOT 用全局自增 id 作水位——真相源是 per-execution event_seq 由 run_queue 行锁分配")))
    check("R21-1a SQLite 过渡说明放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "SQLite 过渡期单写事务下全局 id 无 late-commit 问题、可作 event_seq 过渡实现、而非永久契约")))
    # R21-1a window 自查：尾随 allow 不得放行前半「全局 id 作水位」
    check("R21-1a 尾随 allow 绕过被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "全局自增 id 作已提交水位，SQLite 过渡另说。")))
    # R21-1b（P0-1 跨 execution 可比假设）：断言 child 全局 id 必然大于父 → 命中
    check("R21-1b child 全局 id 必然大于父被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "child 事件全局 id 必然大于父 superseded event id，前端续订即可无损")))
    # R21-1b 正确口径：per-execution 不需跨 execution 可比 → 放行
    check("R21-1b per-execution 不需跨 execution 可比放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "父子事件序不需要跨 execution 可比，SHALL NOT 断言 child 全局 id 必然大于父——late-commit 下不成立、已废弃的旧口径产物")))
    # R21-1c（P0-1 successor 续订游标）：携带父全局 Last-Event-ID 续订 → 命中
    check("R21-1c 携带全局 Last-Event-ID 续订被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "前端收父 superseded 后携带当前全局 Last-Event-ID 续订 successor 流")))
    # R21-1c 正确口径：切 child execution_id 从 child event_seq 回放 → 放行
    check("R21-1c 切 child 从 event_seq 回放放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "successor 续订 SHALL 切 child execution_id、从 child event_seq 起点回放，不复用父的全局 Last-Event-ID")))
    # R21-2（P0-2 并集唯一）：partial unique + NOT EXISTS 保证并集唯一 → 命中
    check("R21-2 三分列 partial unique + NOT EXISTS 保证并集唯一被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "三分列 partial unique + 中心化 NOT EXISTS 保证一父至多一后继并集唯一")))
    # R21-2 正确口径：execution_edges 边表 UNIQUE(parent) → 放行
    check("R21-2 execution_edges 边表并集唯一放行",
          not any(k == "structural" for _, k, _, _ in scan_text(
              "跨键并集唯一以 execution_edges 边表 UNIQUE(parent_execution_id) 为准，SHALL NOT 靠三分列 partial unique + NOT EXISTS——READ COMMITTED 下挡不住跨列并发")))
    # R21-2 window 自查：尾随 allow 不得放行前半「partial unique + NOT EXISTS 保证并集唯一」
    check("R21-2 尾随 allow 绕过被拦",
          any(k == "structural" for _, k, _, _ in scan_text(
              "三分列 partial unique + NOT EXISTS 保证并集唯一，execution_edges 另说。")))

    passed = sum(1 for _, ok in cases if ok)
    print("🧪 self-test")
    for name, ok in cases:
        print(f"   {'✅' if ok else '❌'} {name}")
    print(f"   {passed}/{len(cases)} 通过")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit(main(sys.argv[1:]))
