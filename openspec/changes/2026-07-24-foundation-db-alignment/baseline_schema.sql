-- [table] activities
CREATE TABLE activities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    actor_type  TEXT DEFAULT 'system',        -- user|agent|system
    actor_name  TEXT DEFAULT '',
    action      TEXT NOT NULL,                -- created|status_changed|priority_changed|assigned|task_started|task_completed|task_failed|commented
    detail      TEXT DEFAULT '{}',            -- JSON: {from,to,...}
    created_at  TEXT DEFAULT (datetime('now'))
);

-- [table] agent_profiles
CREATE TABLE agent_profiles (
    slug        TEXT PRIMARY KEY,          -- Agent 身份；模型/记忆/Skills 均按此跨项目共享
    provider_id TEXT DEFAULT '',           -- 接入的大模型供应商（对应 config 里的 provider）
    updated_at  TEXT DEFAULT (datetime('now'))
, nickname TEXT DEFAULT '', avatar TEXT DEFAULT '');

-- [table] agent_skills
CREATE TABLE agent_skills (
    agent_slug  TEXT NOT NULL,             -- 按 Agent 身份；跨项目共享启用的 Skill
    skill_slug  TEXT NOT NULL,
    PRIMARY KEY (agent_slug, skill_slug)
);

-- [table] agent_templates
CREATE TABLE agent_templates (
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
, tags TEXT DEFAULT '', origin TEXT DEFAULT 'scan');

-- [table] conversations
CREATE TABLE conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id    INTEGER REFERENCES project_agents(id) ON DELETE SET NULL,
    title       TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);

-- [table] messages
CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,          -- user / assistant / system / tool
    content         TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now'))
, author_slug TEXT DEFAULT '', author_name TEXT DEFAULT '', run_id INTEGER);

-- [table] project_agents
CREATE TABLE project_agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    template_id INTEGER REFERENCES agent_templates(id),  -- 来源模版，自建可为空
    slug        TEXT DEFAULT '',            -- 记忆归属标识：继承模版 slug（同一 Agent 跨项目共用记忆）
    name        TEXT NOT NULL,
    emoji       TEXT DEFAULT '',
    color       TEXT DEFAULT '',
    persona     TEXT DEFAULT '',            -- 实际生效的人格正文（可在项目内改造）
    provider_id TEXT DEFAULT '',            -- 指定供应商，空=用全局默认
    enabled     INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
, is_leader INTEGER DEFAULT 0);

-- [table] projects
CREATE TABLE projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    local_path  TEXT NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT DEFAULT 'active',      -- active / archived
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
, git_url TEXT DEFAULT '');

-- [table] run_events
CREATE TABLE run_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_queue_id  INTEGER,                    -- 关联 run_queue.id（调度视角主键）
    task_run_id   INTEGER,                    -- 关联 task_runs.id（执行视角，领取后才有）
    task_id       INTEGER,
    agent_slug    TEXT DEFAULT '',
    event         TEXT NOT NULL,              -- enqueued|claimed|retry|done|failed（终态用 run_queue 语义）
    detail        TEXT DEFAULT '{}',          -- JSON：{attempts,backoff,fail_reason,source_run_id,...}
    ts            TEXT DEFAULT (datetime('now'))
);

-- [table] run_logs
CREATE TABLE run_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    ts      TEXT DEFAULT (datetime('now')),
    channel TEXT DEFAULT 'event',            -- stdout|stderr|event|system
    content TEXT DEFAULT ''
, tool TEXT DEFAULT '', tool_input TEXT DEFAULT '', tool_output TEXT DEFAULT '');

-- [table] run_queue
CREATE TABLE run_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_slug  TEXT NOT NULL,
    trigger     TEXT DEFAULT 'mention',       -- assign|mention|leader|collaborate
    is_leader   INTEGER DEFAULT 0,            -- 本次运行是否以团队负责人身份
    prompt      TEXT DEFAULT '',
    status      TEXT DEFAULT 'queued',        -- queued|running|done|failed
    created_at  TEXT DEFAULT (datetime('now'))
, attempts INTEGER DEFAULT 0, next_retry_at TEXT, task_run_id INTEGER, source_run_id INTEGER, source_message_id INTEGER);

-- [table] skill_downloads
CREATE TABLE skill_downloads (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id  INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    ip        TEXT DEFAULT '',
    ts        TEXT DEFAULT (datetime('now'))
);

-- [table] skills
CREATE TABLE skills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,       -- 取自 skills/ 下文件名
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    source_path TEXT DEFAULT '',
    body        TEXT DEFAULT '',            -- 能力指令正文（注入 Agent 系统提示）
    imported_at TEXT DEFAULT (datetime('now'))
, is_dir INTEGER DEFAULT 0, downloadable INTEGER DEFAULT 1);

-- [table] task_runs
CREATE TABLE task_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    conversation_id INTEGER,
    agent_slug      TEXT DEFAULT '',
    status          TEXT DEFAULT 'running',   -- running|succeeded|failed|killed
    provider_id     TEXT DEFAULT '',
    pid             INTEGER,                  -- 子进程 PID，用于 kill
    started_at      TEXT DEFAULT (datetime('now')),
    ended_at        TEXT
, fail_reason TEXT DEFAULT '');

-- [table] tasks
CREATE TABLE tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    status          TEXT DEFAULT 'planning',  -- planning|in_progress|reviewing|done|archived
    assignee_slug   TEXT DEFAULT '',          -- 负责人（project_agents.slug）
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    order_idx       INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
, priority TEXT DEFAULT 'none', parent_task_id INTEGER);

-- [table] users
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',  -- admin | user
    token         TEXT UNIQUE,
    last_seen     TEXT
);

-- [table] workflow_runs
CREATE TABLE workflow_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    status      TEXT DEFAULT 'pending',     -- pending / running / success / failed
    state_json  TEXT DEFAULT '{}',          -- 运行时状态快照
    started_at  TEXT DEFAULT (datetime('now')),
    ended_at    TEXT
);

-- [table] workflows
CREATE TABLE workflows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    definition_json TEXT DEFAULT '{}',      -- 编排定义（串/并/条件）
    status          TEXT DEFAULT 'draft',
    created_at      TEXT DEFAULT (datetime('now'))
);
