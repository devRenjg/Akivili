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


def resolve_targets(argv):
    """解析待扫描的 .md 文件列表 + 缺失路径列表。

    返回 (files, missing)。第九轮加固：缺失路径不再静默跳过，交由调用方 exit 1。
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
            files.extend(sorted(root.rglob("*.md")))
    return files, missing


# 句段分隔符（第十轮 scope='segment'）：中文/英文句读 + markdown 表格竖线。
# 不切顿号/括号——否则会把「orphaned（已起）… execution 回 queued」的主谓拆散而漏检;
# 正确否定「不回 queued」由模式内 (?<!不) lookbehind 排除，白名单再兜底。
_SEGMENT_SPLIT = re.compile(r"[。；;!？|]")


def _segments(line):
    """把一行切成句段，供 scope='segment' 规则在段内匹配、避免跨句误报。"""
    return [seg for seg in _SEGMENT_SPLIT.split(line) if seg.strip()]


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
        if re.search(rule["pattern"], line):
            if any(snip in line for snip in rule.get("allow", [])):
                continue
            hits.append(("forbidden", rule["reason"]))
    for rule in STRUCTURAL:
        scope = rule.get("scope", "line")
        rule_allow = rule.get("allow", [])
        if scope == "segment":
            # 逐句段匹配：某句段命中 pattern，且该**句段内**既无本规则专属 allow 片段、
            # 也非合法独立历史引用块，才算违规（第十二轮删全局 sentiment 白名单;
            # 第十三轮 P1-E：HISTORICAL_INVALID 只豁免引号内历史原文、剩余现行文字仍扫描）。
            hit = False
            for seg in _segments(line):
                exempt, remainder = _historical_exempt_segment(seg)
                # 合法历史引用块：引号内豁免，只对剩余现行文字判违规。
                scan_target = remainder if exempt else seg
                if not re.search(rule["pattern"], scan_target):
                    continue
                if any(snip in scan_target for snip in rule_allow):
                    continue
                hit = True
                break
            if hit:
                hits.append(("structural", rule["reason"]))
        else:
            exempt, remainder = _historical_exempt_segment(line)
            scan_target = remainder if exempt else line
            if re.search(rule["pattern"], scan_target):
                if any(snip in scan_target for snip in rule_allow):
                    continue
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
