# 一键部署：后端 + 前端 + 分享服务 + （可选）公网隧道
# 用法：powershell -ExecutionPolicy Bypass -File start.ps1  [--tunnel]
param(
    [switch]$tunnel = $false   # 加 -tunnel 启动 cloudflared 公网分享
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$WebDir = Join-Path $Root "web"

Write-Host "=== AI 通用 Agent 一键启动 ===" -ForegroundColor Cyan

# 1. 后端 API（8000）
Write-Host "[1/4] 启动后端 API (http://localhost:8000) ..." -ForegroundColor Yellow
$env:PYTHONPATH = $Backend
$env:PYTHONIOENCODING = "utf-8"
Start-Process -WindowStyle Hidden python -ArgumentList "-m","uvicorn","app.main:app","--port","8000" -WorkingDirectory $Root
Start-Sleep -Seconds 6

# 2. 前端（3000）
Write-Host "[2/4] 启动前端 (http://localhost:3000) ..." -ForegroundColor Yellow
Start-Process -WindowStyle Hidden npm -ArgumentList "run","dev" -WorkingDirectory $Frontend
Start-Sleep -Seconds 4

# 3. 分享服务（web 目录静态托管，8001）
Write-Host "[3/4] 启动分享服务 (http://localhost:8001) ..." -ForegroundColor Yellow
Start-Process -WindowStyle Hidden python -ArgumentList "-m","http.server","8001","--directory",$WebDir -WorkingDirectory $Root

# 4. 公网隧道（可选）
if ($tunnel) {
    Write-Host "[4/4] 启动公网隧道 (cloudflared) ..." -ForegroundColor Yellow
    $cf = Join-Path $Root "..\cloudflared.exe"
    if (Test-Path $cf) {
        # 隧道暴露前端 3000（访问者先进页面浏览作品）；如还需分享产物直链，
        # 可再开一条隧道指向 8001，并把 .env 的 PUBLIC_ASSETS_URL 设为该地址
        Start-Process -WindowStyle Hidden $cf -ArgumentList "tunnel","--url","http://localhost:3000","--no-autoupdate" -WorkingDirectory $Root
        Write-Host "隧道启动中，公网地址请查看 cloudflared 输出窗口" -ForegroundColor Yellow
    } else {
        Write-Host "未找到 cloudflared.exe（放在项目上级目录），跳过隧道" -ForegroundColor Red
    }
} else {
    Write-Host "[4/4] 跳过公网隧道（加 -tunnel 启用）" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "=== 启动完成 ===" -ForegroundColor Green
Write-Host "前端:    http://localhost:3000  (一句话自动化 /mini, 分享中心 /gallery)"
Write-Host "后端:    http://localhost:8000/docs"
Write-Host "分享:    http://localhost:8001  (web 作品)"
Write-Host ""
Write-Host "停止：关掉对应进程或重启机器；建议使用任务管理器结束 python/npm 进程" -ForegroundColor DarkGray
