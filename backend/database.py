"""SQLite 数据库：连接 + 表结构基线。

表结构覆盖 Akivili 的核心实体：
- projects        项目（自起标题 + 绑定本地文件夹）
- agent_templates Agent 模版（来自 C:\\Code\\Agents 库，只读导入源的快照）
- project_agents  项目内 Agent 实例（从模版导入，可改造/自建，归属于项目）
- conversations   与某个 Agent 的会话
- messages        会话内的消息（含流式产出落库）
- workflows       工作流定义（配置编排）
- workflow_runs   工作流运行实例

P1 仅需建库与基线；字段会随 P2+ 演进，用轻量迁移（IF NOT EXISTS）兜底。
"""
import aiosqlite

from config import load_settings

# 表结构基线。projects/agents 等的业务字段在各自阶段补充。
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    local_path  TEXT NOT NULL,
    git_url     TEXT DEFAULT '',            -- 仓库链接（展示用；本地目录仍是真实工作目录）
    description TEXT DEFAULT '',
    status      TEXT DEFAULT 'active',      -- active / archived
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,       -- 取自模版文件名，如 specialized-project-owner
    name        TEXT NOT NULL,              -- frontmatter name，如 项目负责人
    division    TEXT DEFAULT '',            -- 所属分类目录
    description TEXT DEFAULT '',
    emoji       TEXT DEFAULT '',
    color       TEXT DEFAULT '',
    source_path TEXT DEFAULT '',            -- 模版 .md 的绝对路径
    body        TEXT DEFAULT '',            -- 人格正文（frontmatter 之后）
    imported_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS project_agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    template_id INTEGER REFERENCES agent_templates(id),  -- 来源模版，自建可为空
    slug        TEXT DEFAULT '',            -- 记忆归属标识：继承模版 slug（同一 Agent 跨项目共用记忆）
    is_leader   INTEGER DEFAULT 0,          -- 团队总负责人 Team Leader（每项目至多一个），排序置顶
    name        TEXT NOT NULL,
    emoji       TEXT DEFAULT '',
    color       TEXT DEFAULT '',
    persona     TEXT DEFAULT '',            -- 实际生效的人格正文（可在项目内改造）
    provider_id TEXT DEFAULT '',            -- 指定供应商，空=用全局默认
    enabled     INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id    INTEGER REFERENCES project_agents(id) ON DELETE SET NULL,
    title       TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,          -- user / assistant / system / tool
    content         TEXT DEFAULT '',
    author_slug     TEXT DEFAULT '',        -- 发言作者的成员 slug（assistant 消息用）
    author_name     TEXT DEFAULT '',        -- user 消息的发送者名（登录用户名，供时间线按人显示）
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workflows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    definition_json TEXT DEFAULT '{}',      -- 编排定义（串/并/条件）
    status          TEXT DEFAULT 'draft',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    status      TEXT DEFAULT 'pending',     -- pending / running / success / failed
    state_json  TEXT DEFAULT '{}',          -- 运行时状态快照
    started_at  TEXT DEFAULT (datetime('now')),
    ended_at    TEXT
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    slug        TEXT PRIMARY KEY,          -- Agent 身份；模型/记忆/Skills/昵称/头像 均按此跨项目共享
    provider_id TEXT DEFAULT '',           -- 接入的大模型供应商（对应 config 里的 provider）
    nickname    TEXT DEFAULT '',           -- 昵称（显示为「昵称（名字）」）
    avatar      TEXT DEFAULT '',           -- 头像文件名（存于 icon 文件夹）
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS skills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,       -- 取自 skills/ 下文件名
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    source_path TEXT DEFAULT '',
    body        TEXT DEFAULT '',            -- 能力指令正文（注入 Agent 系统提示）
    is_dir      INTEGER DEFAULT 0,          -- 1=目录型 Skill（SKILL.md + scripts/references），下载打包 zip
    imported_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS skill_downloads (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id  INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    ip        TEXT DEFAULT '',
    ts        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_skills (
    agent_slug  TEXT NOT NULL,             -- 按 Agent 身份；跨项目共享启用的 Skill
    skill_slug  TEXT NOT NULL,
    PRIMARY KEY (agent_slug, skill_slug)
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    status          TEXT DEFAULT 'backlog',  -- backlog|in_progress|reviewing|done|blocked
    priority        TEXT DEFAULT 'none',      -- urgent|high|medium|low|none
    parent_task_id  INTEGER,                  -- 子任务：指向父任务
    assignee_slug   TEXT DEFAULT '',          -- 负责人（project_agents.slug）
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    order_idx       INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    actor_type  TEXT DEFAULT 'system',        -- user|agent|system
    actor_name  TEXT DEFAULT '',
    action      TEXT NOT NULL,                -- created|status_changed|priority_changed|assigned|task_started|task_completed|task_failed|commented
    detail      TEXT DEFAULT '{}',            -- JSON: {from,to,...}
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    conversation_id INTEGER,
    agent_slug      TEXT DEFAULT '',
    status          TEXT DEFAULT 'running',   -- running|succeeded|failed|killed
    provider_id     TEXT DEFAULT '',
    pid             INTEGER,                  -- 子进程 PID，用于 kill
    started_at      TEXT DEFAULT (datetime('now')),
    ended_at        TEXT
);

CREATE TABLE IF NOT EXISTS run_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    ts          TEXT DEFAULT (datetime('now')),
    channel     TEXT DEFAULT 'event',        -- stdout|stderr|event|system|tool|tool_result|thinking
    content     TEXT DEFAULT '',
    tool        TEXT DEFAULT '',             -- 工具名（Bash/Read/Write…）
    tool_input  TEXT DEFAULT '',             -- 工具完整入参 JSON（含实际命令）
    tool_output TEXT DEFAULT ''              -- 工具完整输出
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',  -- admin | user
    token         TEXT UNIQUE,
    last_seen     TEXT
);

CREATE TABLE IF NOT EXISTS run_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_slug  TEXT NOT NULL,
    trigger     TEXT DEFAULT 'mention',       -- assign|mention|leader|collaborate
    is_leader   INTEGER DEFAULT 0,            -- 本次运行是否以团队负责人身份
    prompt      TEXT DEFAULT '',
    status      TEXT DEFAULT 'queued',        -- queued|running|done|failed
    created_at  TEXT DEFAULT (datetime('now'))
);
"""


def get_db_path() -> str:
    return load_settings().db_path


async def init_db() -> None:
    """建库 + 基线表。幂等，可重复调用。"""
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(SCHEMA)
        await _migrate(db)
        await db.commit()


async def _migrate(db) -> None:
    """轻量迁移：为已存在的旧表补新列（CREATE TABLE IF NOT EXISTS 不会加列）。"""
    # projects.git_url（仓库链接，展示用）
    cur = await db.execute("PRAGMA table_info(projects)")
    pjcols = {row[1] for row in await cur.fetchall()}
    if "git_url" not in pjcols:
        await db.execute("ALTER TABLE projects ADD COLUMN git_url TEXT DEFAULT ''")
    cur = await db.execute("PRAGMA table_info(project_agents)")
    cols = {row[1] for row in await cur.fetchall()}
    if "is_leader" not in cols:
        await db.execute("ALTER TABLE project_agents ADD COLUMN is_leader INTEGER DEFAULT 0")
    # tasks 新列
    cur = await db.execute("PRAGMA table_info(tasks)")
    tcols = {row[1] for row in await cur.fetchall()}
    if "priority" not in tcols:
        await db.execute("ALTER TABLE tasks ADD COLUMN priority TEXT DEFAULT 'none'")
    if "parent_task_id" not in tcols:
        await db.execute("ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER")
    # 合并「规划中(planning)」到「待办(backlog)」、「已归档(archived)」到「已完成(done)」：迁移旧数据
    await db.execute("UPDATE tasks SET status='backlog' WHERE status='planning'")
    await db.execute("UPDATE tasks SET status='done' WHERE status='archived'")
    # agent_profiles 新列
    cur = await db.execute("PRAGMA table_info(agent_profiles)")
    pcols = {row[1] for row in await cur.fetchall()}
    if "nickname" not in pcols:
        await db.execute("ALTER TABLE agent_profiles ADD COLUMN nickname TEXT DEFAULT ''")
    if "avatar" not in pcols:
        await db.execute("ALTER TABLE agent_profiles ADD COLUMN avatar TEXT DEFAULT ''")
    # skills 目录型标记
    cur = await db.execute("PRAGMA table_info(skills)")
    scols = {row[1] for row in await cur.fetchall()}
    if "is_dir" not in scols:
        await db.execute("ALTER TABLE skills ADD COLUMN is_dir INTEGER DEFAULT 0")
    # messages 发言作者归属（用于详情动态区按成员显示头像/昵称）
    cur = await db.execute("PRAGMA table_info(messages)")
    mcols = {row[1] for row in await cur.fetchall()}
    if "author_slug" not in mcols:
        await db.execute("ALTER TABLE messages ADD COLUMN author_slug TEXT DEFAULT ''")
    if "author_name" not in mcols:
        await db.execute("ALTER TABLE messages ADD COLUMN author_name TEXT DEFAULT ''")
    # run_logs 结构化工具字段（用于「日志详情」还原命令与运行时详情）
    cur = await db.execute("PRAGMA table_info(run_logs)")
    rlcols = {row[1] for row in await cur.fetchall()}
    if "tool" not in rlcols:
        await db.execute("ALTER TABLE run_logs ADD COLUMN tool TEXT DEFAULT ''")
    if "tool_input" not in rlcols:
        await db.execute("ALTER TABLE run_logs ADD COLUMN tool_input TEXT DEFAULT ''")
    if "tool_output" not in rlcols:
        await db.execute("ALTER TABLE run_logs ADD COLUMN tool_output TEXT DEFAULT ''")


async def get_connection() -> aiosqlite.Connection:
    """获取一个开启外键约束、行可按列名取值的连接。调用方负责关闭。"""
    db = await aiosqlite.connect(get_db_path())
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row
    return db
