# worker-split-minimal (delta)

## ADDED Requirements

### Requirement: 执行层与 API 层进程分离

系统 SHALL 把 Agent 执行层（run 领取、并发池、CLI 子进程管理、收尾）运行在**独立于 API 进程的 worker 进程**中。API 进程 SHALL 只负责 HTTP/SSE、业务路由与入队（写 `run_queue`），SHALL NOT 在自身进程内运行执行循环。二者 SHALL 能各自独立重启。

#### Scenario: API 重启不打断在跑执行

- **WHEN** worker 进程正在执行一个 run，此时 API 进程被重启
- **THEN** worker 进程不受影响，该 run 继续执行至收尾，其结果正常落库；API 重启完成后可查询到该 run 的最终状态

#### Scenario: worker 独立领取

- **WHEN** API 进程仅执行入队（`enqueue_run`）而不启动任何执行循环
- **THEN** 独立的 worker 进程从 `run_queue` 领取并执行该 run，不依赖 API 进程内的调度

### Requirement: worker 子进程树 containment

系统 SHALL 保证 worker 进程终止时，其派生的 CLI 子进程被连带清理（Windows Job Object / POSIX 进程组等价机制），使「已死的 worker 不会再有子进程写数据库或工作目录」。

#### Scenario: 强杀 worker 后无残留写

- **WHEN** worker 进程被强制终止（非优雅退出）
- **THEN** 其派生的所有 CLI 子进程在约定时间内退出，不再对数据库或文件产生写入

### Requirement: 重启时遗留在跑 run 的整批恢复

系统 SHALL 在 worker 启动时，将上一代 worker 遗留的在跑 run（`run_queue.status IN ('running','claimed')` 且不属于当前 worker 代）整批判死并重投——以单条批量更新完成，SHALL NOT 逐个做精细的 generation 交棒/接管。

#### Scenario: 上一代残留 run 被整批判死重投

- **WHEN** 上一代 worker 异常退出，遗留若干 `running`/`claimed` 状态的 run
- **THEN** 新 worker 启动时把这些 run 整批标记为失败并重新入队，任务可被重新领取执行，不产生双执行

### Requirement: 单 worker 下的 claim 冗余安全带

系统的 run 领取 SHALL 使用条件更新（`UPDATE ... WHERE id=? AND status='queued'`），当条件不满足（rowcount=0）时视为已被领取并返回空。单 worker 场景下此为零行为变化的冗余保护，为未来多 worker 的原子领取铺路。

#### Scenario: 单 worker 领取无重复无遗漏

- **WHEN** 单 worker 从队列连续领取多个 queued run
- **THEN** 每个 run 恰好被领取一次，不重复领取、不遗漏，行为与加 CAS 条件前一致
