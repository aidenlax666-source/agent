# ============================================================
# 打包本地执行端为独立 exe（Windows 双击即用）
#
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File build_local_worker.ps1
#
# 产物：dist/local_worker.exe（约 10-15MB，含 Python 运行时）
# 用户拿到 exe 后：双击 → 填云端地址 + 自己的 token → 开始接收本地任务
# ============================================================
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==> 安装打包依赖（首次）"
python -m pip install --quiet pyinstaller httpx

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
    Write-Host "    分发方式：把 exe + 使用说明（README）一起发给用户，双击即用"
    Write-Host "    用户首次运行：local_worker.exe --server https://你的云端 --token 登录token"
} else {
    Write-Host "打包失败" -ForegroundColor Red
    exit 1
}
