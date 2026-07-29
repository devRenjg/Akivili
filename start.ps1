Write-Host "=== Akivili 启动脚本 ===" -ForegroundColor Cyan

# [1/5] 后端依赖
Write-Host "`n[1/5] 安装后端依赖..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\backend"
py -3.12 -m pip install -r requirements.txt -q

# [2/5] 前端依赖
Write-Host "[2/5] 安装前端依赖..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\frontend"
if (-not (Test-Path "node_modules")) {
    npm install
} else {
    Write-Host "  node_modules 已存在，跳过。"
}

# [3/5] PostgreSQL 就绪检查（S5：PG 单引擎、无降级——PG 不通则后端启动即崩，先探明再放行）
Write-Host "[3/5] 检查 PostgreSQL 就绪..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\backend"
py -3.12 wait_for_pg.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "PostgreSQL 未就绪，已中止启动（详见上方排查指引）。" -ForegroundColor Red
    exit 1
}

# [4/5] 启动后端
Write-Host "[4/5] 启动后端 (端口 8100)..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\backend"
$backend = Start-Process -FilePath "py" -ArgumentList "-3.12", "main.py" -PassThru -NoNewWindow

# [5/5] 启动前端
Write-Host "[5/5] 启动前端 (端口 3100)..." -ForegroundColor Yellow
Set-Location "$PSScriptRoot\frontend"
$frontend = Start-Process -FilePath "cmd" -ArgumentList "/c", "npm run dev" -PassThru -NoNewWindow

Write-Host "`n=== 服务已启动 ===" -ForegroundColor Green
Write-Host "  前端: http://localhost:3100" -ForegroundColor Cyan
Write-Host "  后端: http://localhost:8100" -ForegroundColor Cyan
Write-Host "`n按 Ctrl+C 停止所有服务。`n" -ForegroundColor Gray

try {
    Wait-Process -Id $backend.Id
} finally {
    if (!$backend.HasExited) { Stop-Process -Id $backend.Id -Force }
    if (!$frontend.HasExited) { Stop-Process -Id $frontend.Id -Force }
    Write-Host "服务已停止。" -ForegroundColor Yellow
}
