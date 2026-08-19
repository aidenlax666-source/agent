# 安装 git pre-commit 钩子（Windows PowerShell）
# 作用：每次 git commit 前自动运行后端测试，失败阻止提交。
# 用法：powershell -ExecutionPolicy Bypass -File setup-git-hooks.ps1

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$hook = Join-Path $repo ".git\hooks\pre-commit"

$hookContent = @'
#!/bin/sh
# pre-commit hook (installed by setup-git-hooks.ps1)
# 每次 commit 自动跑后端测试，失败阻止提交

echo "=== [pre-commit] 运行测试套件 ==="
cd "$(git rev-parse --show-toplevel)" || exit 1

CHANGED_BACKEND=$(git diff --cached --name-only | grep -E "^backend/|^cli/agent-cli\.py")

if [ -n "$CHANGED_BACKEND" ]; then
    echo "检测到后端改动，运行 pytest..."
    if command -v python >/dev/null 2>&1; then
        PYTHONPATH=backend python -m pytest backend/tests -q 2>&1 | tail -25
        STATUS=$?
        # tail 会吞掉 pytest 的退出码，用 PIPESTATUS
        STATUS=${PIPESTATUS[0]}
        if [ "$STATUS" -ne 0 ]; then
            echo ""
            echo "TEST FAILED: 测试未通过，提交被阻止。修复后重试，或 git commit --no-verify 跳过。"
            exit 1
        fi
        echo "TEST PASSED: 测试全部通过"
    else
        echo "python not found, skip"
    fi
else
    echo "无后端改动，跳过测试"
fi

exit 0
'@

try {
    Set-Content -Path $hook -Value $hookContent -Encoding utf8NoBOM -Force
    Write-Host "✅ pre-commit 钩子已安装: $hook" -ForegroundColor Green
    Write-Host "   效果：每次 git commit 自动跑 pytest，失败阻止提交。"
    Write-Host "   跳过：git commit --no-verify"
} catch {
    Write-Host "❌ 安装失败: $_" -ForegroundColor Red
    exit 1
}
