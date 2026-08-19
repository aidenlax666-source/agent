# ============================================================
# 打包本地执行端为独立 exe（Windows 双击即用）
#
# 用法（PowerShell，在 cli/ 目录下执行）：
#   powershell -ExecutionPolicy Bypass -File build_local_worker.ps1
#   指定云端地址（推荐）：
#   $env:LOCAL_WORKER_SERVER="https://your-cloud.com"; powershell -ExecutionPolicy Bypass -File build_local_worker.ps1
#
# 产物：cli/dist/local_worker.exe（约 10-15MB，含 Python 运行时）
# 用户体验：双击 exe → 输入云端账号密码登录一次 → 之后全自动接收本地任务
# ============================================================
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "local_worker.py"

# 1. 把默认云端地址打进 exe（用户不用填）
$server = $env:LOCAL_WORKER_SERVER
if ($server) {
    Write-Host "==> 内置云端地址: $server"
    $content = [System.IO.File]::ReadAllText($script, [System.Text.Encoding]::UTF8)
    $content = $content -replace 'DEFAULT_SERVER = "[^"]*"', ('DEFAULT_SERVER = "' + $server + '"')
    [System.IO.File]::WriteAllText($script, $content, (New-Object System.Text.UTF8Encoding $false))
} else {
    Write-Host "==> 未设置 LOCAL_WORKER_SERVER，使用源码默认地址（开发用 http://localhost:8000 请自行改）"
}

# 2. 安装打包依赖（首次）
Write-Host "==> 安装打包依赖（首次）"
python -m pip install --quiet pyinstaller httpx

# 3. 打包
Write-Host "==> 打包 local_worker.exe"
Push-Location $root
try {
    python -m PyInstaller `
        --onefile `
        --console `
        --name local_worker `
        --clean `
        --noconfirm `
        local_worker.py
} finally {
    Pop-Location
}

$exe = Join-Path $root "dist\local_worker.exe"
if (Test-Path $exe) {
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "==> 完成：$exe（${size}MB）"
    Write-Host "    分发方式：把 exe + 使用说明发给用户"
    Write-Host "    用户使用：双击 exe → 输入云端账号密码登录一次 → 自动接收本地任务"
} else {
    Write-Host "打包失败" -ForegroundColor Red
    exit 1
}
