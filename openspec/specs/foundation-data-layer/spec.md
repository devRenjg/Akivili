# foundation-data-layer

## Purpose

统一后端数据底座：单一连接入口、版本化 Schema 迁移、类型安全的 ORM 访问层、引擎方言单点隔离，并以 **PostgreSQL 为唯一数据库引擎**（无 SQLite、无降级、无双引擎兼容）。消除并发写锁库、消除运行期隐式 `ALTER` 演进结构、消除散落的手写 SQL 与方言字面量，使数据层可迁移、可回滚、可测试。

## Requirements

### Requirement: 统一连接入口与并发调优

系统 SHALL 通过单一连接入口访问数据库，且该入口 SHALL 启用 WAL 日志模式与每连接忙等超时，消除并发写下的 `database is locked`。除数据库初始化建库路径外，业务代码 SHALL NOT 各自直连数据库。

#### Scenario: 所有业务访问走统一入口
- **WHEN** 任一业务模块需要读写数据库
- **THEN** 它通过统一连接入口获取连接，不自行 `connect`（初始化建库路径除外）

#### Scenario: 连接启用调优参数
- **WHEN** 统一入口建立连接
- **THEN** 该连接已启用 `WAL`、`busy_timeout`（值来自配置非硬编码）、`foreign_keys`

#### Scenario: 并发写不再锁库
- **WHEN** 多个协程并发写同一库
- **THEN** 写请求在 busy_timeout 内串行完成，不返回 `database is locked`

#### Scenario: 收口不改变业务行为
- **WHEN** 旁路连接改为走统一入口后运行既有回归
- **THEN** 所有读结果与写副作用与收口前一致

### Requirement: 版本化 Schema 迁移

系统的数据库结构 SHALL 由编号迁移唯一定义，具备版本记录、启动自动应用、可回滚能力。系统 SHALL NOT 再用运行期探测列存在性（`PRAGMA table_info`）后隐式 `ALTER` 的方式演进结构。

#### Scenario: 启动自动应用迁移
- **WHEN** 后端启动
- **THEN** 系统在处理业务前把数据库升级到最新迁移版本

#### Scenario: 空库可重放重建
- **WHEN** 对一个全新空库应用全部迁移
- **THEN** 得到的结构与固化基线快照逐表逐字段一致

#### Scenario: 存量库幂等承接
- **WHEN** 已有数据的库首次接入迁移框架
- **THEN** 系统将其标记为基线版本已应用，不重复建表、不丢失数据

#### Scenario: 迁移可回滚
- **WHEN** 需要撤销某次结构变更
- **THEN** 该迁移提供可执行的反向操作，能回退到上一版本

### Requirement: 类型安全的 ORM 访问层

系统的数据读写 SHALL 经由 ORM 模型层完成，业务代码 SHALL NOT 手写散落的字符串 SQL。ORM 模型 SHALL 与迁移定义的结构一致。

#### Scenario: 业务读写经 ORM
- **WHEN** 业务模块执行数据查询或变更
- **THEN** 它通过 ORM 模型 / 查询 API 完成，而非拼接 SQL 字符串

#### Scenario: 收敛后行为等价
- **WHEN** 某表的手写 SQL 迁移到 ORM 后运行该表相关回归
- **THEN** 读结果与写副作用与迁移前逐一一致

### Requirement: 引擎方言单点隔离

数据库引擎特有的方言（时间函数、自增主键、占位符、upsert、布尔、时间时区、标识符大小写）SHALL 只存在于 ORM / 统一 helper 一层，业务代码 SHALL NOT 直接书写引擎方言字面量。

#### Scenario: 时间取值不写字面量
- **WHEN** 业务需要「当前时间」
- **THEN** 它使用 ORM 时间表达式 / 统一 helper，而非 `datetime('now')` 或 `now()` 字面量

#### Scenario: 方言差异由 helper 层吸收
- **WHEN** 业务需要自增主键 / upsert / 相对时间等带方言差异的操作
- **THEN** 业务代码调用 ORM / 统一 helper 表达意图，PostgreSQL 方言的具体写法只落在该层内

### Requirement: PostgreSQL 单引擎

系统 SHALL 以 PostgreSQL 为**唯一**数据库引擎运行，SHALL NOT 提供 SQLite 运行期路径、降级或双引擎兼容。运行期驱动 SHALL 为异步 asyncpg，迁移期为同步 psycopg，连接串来自单一配置源（`AKIVILI_DB_URL` 优先，否则按 `AKIVILI_PG_*` 分段环境变量拼装）。单进程架构 SHALL 保持不变。

#### Scenario: 默认连接 PostgreSQL
- **WHEN** 未显式指定 `AKIVILI_DB_URL` 而启动后端
- **THEN** 系统按 `AKIVILI_PG_*` 分段环境变量拼出 PostgreSQL 连接串并启动，运行期驱动为 asyncpg

#### Scenario: PG 不可用则不启动
- **WHEN** 目标 PostgreSQL 不可连接
- **THEN** 启动前置的就绪检查在超时窗口内探测失败后中止启动并给出排查指引，系统 SHALL NOT 回退到任何本地/内存/SQLite 路径

#### Scenario: 无 SQLite 运行期路径
- **WHEN** 审视业务 / 路由 / 执行 / 测试 seed 的运行期数据访问
- **THEN** 不存在任何 aiosqlite 运行期连接或 SQLite 引擎分支；历史 SQLite 数据仅由一次性割接工具读取作为搬迁源

#### Scenario: 迁移在 PostgreSQL 上可应用与回滚
- **WHEN** 对 PostgreSQL 库应用全部迁移，或回退某次迁移
- **THEN** 迁移在 PostgreSQL 上成功建库 / 回滚，结构与固化基线一致

### Requirement: 数据搬迁一致性

系统 SHALL 提供把历史 SQLite 数据一次性搬迁到 PostgreSQL 的工具，搬迁后 SHALL 能校验两库数据一致。此为割接期一次性能力，非运行期双引擎依赖。

#### Scenario: 数据搬迁一致
- **WHEN** 把 SQLite 数据搬迁到 PostgreSQL 后做一致性校验
- **THEN** 逐表行数与关键字段抽样在两库间一致

