#!/usr/bin/env bash
# pre-commit 钩子：commit 前自动跑后端测试（改代码即自动验证，失败阻止提交）
# 安装：把本文件复制到 .git/hooks/pre-commit（或运行 setup-git-hooks.sh）
echo "=== [pre-commit] 运行测试套件 ==="

cd "$(git rev-parse --show-toplevel)" || exit 1

# 只跑与改动相关的测试（后端改了跑后端，前端改了就跳过——前端测试暂无）
CHANGED_BACKEND=$(git diff --cached --name-only | grep -E "^backend/|^agent-cli.py" || true)

if [ -n "$CHANGED_BACKEND" ]; then
    echo "检测到后端改动，运行 pytest..."
    if command -v python >/dev/null 2>&1; then
        PYTHONPATH=backend python -m pytest backend/tests -q 2>&1 | tail -20
        STATUS=${PIPESTATUS[0]}
        if [ "$STATUS" -ne 0 ]; then
            echo ""
            echo "❌ 测试未通过，提交被阻止。请修复后再 commit。"
            echo "   （跳过检查：git commit --no-verify）"
            exit 1
        fi
        echo "✅ 测试全部通过"
    else
        echo "⚠️ 未找到 python，跳过测试"
    fi
else
    echo "无后端改动，跳过测试"
fi

exit 0
