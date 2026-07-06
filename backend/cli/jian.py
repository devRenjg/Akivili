#!/usr/bin/env python3
"""jian —— Agent 平台 CLI。Agent 在任务执行时用它在平台上真正操作：
建子任务卡片、发言、改状态、查花名册。

身份与地址由执行环境注入的环境变量提供（runner 注入）：
  JIAN_API        后端地址，如 http://127.0.0.1:8100
  JIAN_TASK_ID    当前任务 id
  JIAN_AGENT_SLUG 当前 Agent 身份

用法：
  jian roster                              查团队花名册
  jian comment "内容"                       在当前任务发言（@成员会触发其执行）
  jian status <backlog|in_progress|reviewing|done|blocked>
  jian subtask --title "标题" [--owner <slug>] [--body "正文" | --body-file <路径>]
      建子任务卡片，Owner 默认为你自己，自动挂到当前任务
"""
import argparse
import os
import sys
import json
import urllib.request
import urllib.error

API = os.environ.get("JIAN_API", "http://127.0.0.1:8100")
TASK_ID = os.environ.get("JIAN_TASK_ID", "")
AGENT_SLUG = os.environ.get("JIAN_AGENT_SLUG", "")


def _post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"[jian] 请求失败 {e.code}: {e.read().decode('utf-8', 'replace')}\n")
        sys.exit(1)


def _get(path):
    try:
        with urllib.request.urlopen(API + path, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"[jian] 请求失败 {e.code}: {e.read().decode('utf-8', 'replace')}\n")
        sys.exit(1)


def main():
    if not TASK_ID or not AGENT_SLUG:
        sys.stderr.write("[jian] 缺少身份环境变量（JIAN_TASK_ID / JIAN_AGENT_SLUG），只能在任务执行上下文里用。\n")
        sys.exit(1)

    p = argparse.ArgumentParser(prog="jian")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("roster")

    pc = sub.add_parser("comment")
    pc.add_argument("body", nargs="?", default="")   # 短内容可直接传；多行长内容请用 --body-file
    pc.add_argument("--body-file", default="",
                    help="从文件读发言正文（多行/长内容推荐，避免命令行参数被截断）")
    pc.add_argument("--stdin", action="store_true", help="从标准输入读发言正文")

    ps = sub.add_parser("status")
    ps.add_argument("status")

    pt = sub.add_parser("subtask")
    pt.add_argument("--title", required=True)
    pt.add_argument("--owner", default="")
    pt.add_argument("--body", default="")
    pt.add_argument("--body-file", default="")
    pt.add_argument("--assign", action="store_true",
                    help="委派模式：指派给 --owner 并触发其在子任务里执行")

    args = p.parse_args()
    tid = int(TASK_ID)

    if args.cmd == "roster":
        print(_get(f"/api/agent-cli/roster/{tid}")["roster"])
    elif args.cmd == "comment":
        body = args.body
        if args.body_file:
            with open(args.body_file, encoding="utf-8") as f:
                body = f.read()
        elif args.stdin:
            body = sys.stdin.read()
        if not body.strip():
            sys.stderr.write("[jian] 发言内容为空：请传入内容，或用 --body-file <文件> / --stdin\n")
            sys.exit(1)
        _post("/api/agent-cli/comment", {"task_id": tid, "agent_slug": AGENT_SLUG, "body": body})
        print("[jian] 已发言")
    elif args.cmd == "status":
        r = _post("/api/agent-cli/status", {"task_id": tid, "agent_slug": AGENT_SLUG, "status": args.status})
        print(f"[jian] 状态已改为 {r['status']}")
    elif args.cmd == "subtask":
        body = args.body
        if args.body_file:
            with open(args.body_file, encoding="utf-8") as f:
                body = f.read()
        r = _post("/api/agent-cli/subtask", {
            "task_id": tid, "agent_slug": AGENT_SLUG,
            "title": args.title, "owner_slug": args.owner, "body": body,
            "assign": args.assign,
        })
        if r.get("delegated"):
            print(f"[jian] 已委派子任务 #{r['subtask_id']} 给 {r['owner']}（已触发其执行）")
        else:
            print(f"[jian] 已创建子任务 #{r['subtask_id']}（Owner={r['owner']}），已挂到当前任务")
    else:
        p.print_help()


if __name__ == "__main__":
    main()
