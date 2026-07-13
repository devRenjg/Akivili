"""Akivili 记忆卫生探针（P0+P1）。

验证记忆注入/沉淀的四项优化（隔离库 + 直接调被测函数）：
  P0-1 recent 只存净结论、滚动上限 3 条（不拿 stdout 兜底）。
  P0-2 knowhow 注入按与任务关键词重叠度精选 top-N，剥离 task 标记。
  P1-3 反思 prompt 含「低价值任务回无」的质量门槛文案。
  P1-4 history 回灌滑动窗口裁到最近 N 条。

临时 config/DB，测完清理。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_qa_suite import setup_isolated_config, bootstrap_backend  # noqa: E402


class Probe:
    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(r[1] for r in self.results)


async def run_probe(paths):
    probe = Probe()
    await bootstrap_backend(paths)
    import memory as mem_mod
    from memory import upsert_managed_section, select_relevant_knowhow
    from executor import runner
    import reflect

    slug = "engineering-data-engineer"

    # ---- P0-2：knowhow 相关性精选 top-N ----
    # 写入 10 条 knowhow：一半数据向、一半前端向，验证按任务关键词精选
    bullets = [
        "识别整列错位脏数据：按 ingest 批次分组比对 <!-- akivili:task:43 -->",
        "聚合前用 count(distinct mid) 按 sid 去重防官号虚高 <!-- akivili:task:43 -->",
        "右偏分布用中位数+尾部刻画别用均值 <!-- akivili:task:43 -->",
        "清脏数据按备份→删除→加固→补单测治本 <!-- akivili:task:43 -->",
        "数据口径要在文档里显式写死并给理由 <!-- akivili:task:43 -->",
        "前端用 Vue+Element Plus 按真实技术栈描述能力 <!-- akivili:task:53 -->",
        "展示层承诺给可量化验收口径 Lighthouse 90+ <!-- akivili:task:53 -->",
        "自我介绍套固定五段结构便于汇总 <!-- akivili:task:53 -->",
        "弱网 3 秒可用是前端体验硬指标 <!-- akivili:task:53 -->",
        "无障碍 WCAG 2.1 AA 是交付底线 <!-- akivili:task:53 -->",
    ]
    body = "## 🧠 工作经验与 Know-how（做同类任务前先看）\n\n" + "\n".join(f"- {b}" for b in bullets)
    upsert_managed_section(slug, "knowhow", body)

    data_task = "确认某数据表有效性：核对取数口径、去重、脏数据识别与分层落表"
    picked = select_relevant_knowhow(slug, data_task, top_n=4)
    probe.check("P0-2 knowhow 精选到 top-N 条", picked.count("\n- ") == 4,
                f"注入条数={picked.count(chr(10)+'- ')}")
    probe.check("P0-2 精选命中数据向经验（含'脏数据'/'去重'/'口径'）",
                any(k in picked for k in ("脏数据", "去重", "口径", "中位数")),
                "相关性匹配生效")
    probe.check("P0-2 注入时剥离 task 归属标记",
                "akivili:task:" not in picked, "标记已剥离")

    # 条目数 <= top_n → 全给
    small = select_relevant_knowhow(slug, data_task, top_n=20)
    probe.check("P0-2 条目<=top_n 时全给", small.count("\n- ") == 10, f"={small.count(chr(10)+'- ')}")

    # ---- P0-1 + P1-4 常量 ----
    probe.check("P0-1 recent 滚动上限降到 3", runner._RECENT_RUNS_MAX == 3,
                f"_RECENT_RUNS_MAX={runner._RECENT_RUNS_MAX}")
    probe.check("P1-4 history 滑动窗口常量存在且合理",
                getattr(runner, "_HISTORY_MAX_MSGS", 0) >= 10,
                f"_HISTORY_MAX_MSGS={getattr(runner,'_HISTORY_MAX_MSGS',None)}")

    # P1-4 _clip_history 行为
    long_hist = [{"role": "user", "content": f"m{i}"} for i in range(50)]
    clipped = runner._clip_history(long_hist)
    probe.check("P1-4 超长 history 裁到最近 N 条",
                len(clipped) == runner._HISTORY_MAX_MSGS and clipped[-1]["content"] == "m49",
                f"裁后={len(clipped)} 末条={clipped[-1]['content']}")
    short_hist = [{"role": "user", "content": "only"}]
    probe.check("P1-4 短 history 原样不裁", runner._clip_history(short_hist) == short_hist)

    # ---- P0-1 _compose_injected_memory 组装 ----
    upsert_managed_section(slug, "recent", "## 🗒️ 近期做过的任务\n\n### 某任务\n- 我的产出：净结论")
    upsert_managed_section(slug, "workspace", "## 🗂️ 工作区\n\n只在项目路径内操作")
    composed = runner._compose_injected_memory(slug, data_task)
    probe.check("P0-1 注入记忆含 knowhow+recent+workspace 三段",
                all(s in composed for s in ("Know-how", "近期做过", "工作区")),
                "三段齐全")
    probe.check("P0-1 注入的 knowhow 也走了相关性精选（不超过 top-N 默认8）",
                composed.count("\n- ") <= 8 + 3,  # knowhow<=8 + recent/workspace 里的 bullet
                f"bullet 行={composed.count(chr(10)+'- ')}")

    # ---- P1-3 反思质量门槛文案 ----
    probe.check("P1-3 反思 prompt 含「回无」质量门槛",
                "只回一个字：无" in reflect.REFLECT_PROMPT and "宁缺毋滥" in reflect.REFLECT_PROMPT,
                "门槛文案就位")

    return probe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="akivili_memhyg_"))
    try:
        paths = setup_isolated_config(tmp)
        probe = asyncio.run(run_probe(paths))
        n_ok = sum(1 for _, ok, _ in probe.results if ok)
        print("\n" + ("✅ 全部通过" if probe.ok else "❌ 存在失败项"))
        print(f"{n_ok}/{len(probe.results)} 通过")
        sys.exit(0 if probe.ok else 1)
    finally:
        if args.keep:
            print(f"[keep] {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

