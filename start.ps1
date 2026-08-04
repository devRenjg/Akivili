Write-Host "=== Akivili 启动脚本 ===" -ForegroundColor Cyan

# [1/6] 后端依赖
Write-Host "`n[1/6] 安装后端依赖..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\backend"
py -3.12 -m pip install -r requirements.txt -q

# [2/6] 前端依赖
Write-Host "[2/6] 安装前端依赖..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\frontend"
if (-not (Test-Path "node_modules")) {
    npm install
} else {
    Write-Host "  node_modules 已存在，跳过。"
}

# [3/6] PostgreSQL 就绪检查（S5：PG 单引擎、无降级——PG 不通则后端启动即崩，先探明再放行）
Write-Host "[3/6] 检查 PostgreSQL 就绪..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\backend"
py -3.12 wait_for_pg.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "PostgreSQL 未就绪，已中止启动（详见上方排查指引）。" -ForegroundColor Red
    exit 1
}

# [4/6] 启动后端 API（端口 8100，仅 HTTP/SSE + 入队 + 查询；执行面已剥离到 worker）
Write-Host "[4/6] 启动后端 API (端口 8100)..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\backend"
$backend = Start-Process -FilePath "py" -ArgumentList "-3.12", "main.py" -PassThru -NoNewWindow

# [5/6] 启动 worker（执行面独立进程：领队列 + 跑 Agent + CLI 子进程 + 孤儿回收/巡检 + kill 信号）
# worker-split-minimal 组 1：执行面与 API 分进程，重启 API 不打断在跑的队列路径 Agent。
# 与 API 各自独立进程；run_migrations 有 pg advisory lock 串行化，谁先起都安全。
Write-Host "[5/6] 启动 worker (执行面独立进程)..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\backend"
$worker = Start-Process -FilePath "py" -ArgumentList "-3.12", "worker.py" -PassThru -NoNewWindow

# [6/6] 启动前端
Write-Host "[6/6] 启动前端 (端口 3100)..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\frontend"
$frontend = Start-Process -FilePath "cmd" -ArgumentList "/c", "npm run dev" -PassThru -NoNewWindow

Write-Host "`n=== 服务已启动 ===" -ForegroundColor Green
Write-Host "  前端:   http://localhost:3100" -ForegroundColor Cyan
Write-Host "  后端API: http://localhost:8100" -ForegroundColor Cyan
Write-Host "  worker: 执行面独立进程 (PID $($worker.Id))" -ForegroundColor Cyan
Write-Host "`n按 Ctrl+C 停止所有服务。`n" -ForegroundColor Gray

try {
    Wait-Process -Id $backend.Id
} finally {
    if (!$backend.HasExited) { Stop-Process -Id $backend.Id -Force }
    if ($worker -and !$worker.HasExited) { Stop-Process -Id $worker.Id -Force }
    if (!$frontend.HasExited) { Stop-Process -Id $frontend.Id -Force }
    Write-Host "服务已停止。" -ForegroundColor Yellow
}
