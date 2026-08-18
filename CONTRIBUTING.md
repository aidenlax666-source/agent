# Contributing Guide

欢迎贡献！无论是功能、文档、测试还是安全审计。

## 开发环境

```bash
# 后端
cd backend && pip install -r requirements.txt
cp ../.env.example ../.env   # 填入 DEEPSEEK_API_KEY

# 前端
cd frontend && npm install
```

启动：`powershell -ExecutionPolicy Bypass -File start.ps1`（或见 README 手动启动）

## 提交 PR 前检查

1. **代码可运行**：改动的模块能正常 import/启动
2. **语法正确**：`python -m py_compile <file>`
3. **不破坏核心闭环**：至少跑通一次一句话任务（`/mini`）或开发助手（`/claude`）
4. **安全**：任何"生成代码并执行"相关的改动，请阅读 [SECURITY.md](docs/SECURITY.md)，确保不绕过静态扫描/路径校验

## 代码规范

- Python 3.10+，类型标注（`from __future__ import annotations`）
- 安全关键函数（路径/命令/解压）必须有注释说明防护点
- 前端：TypeScript + React（Next.js App Router）
- 提交信息用中文或英文皆可，说明"改了什么 + 为什么"

## 新增技能（零代码扩展）

技能系统通过 `backend/app/skills/<name>/SKILL.md` 声明：

```markdown
---
name: video-edit
description: 视频剪辑（FFmpeg）
keywords: 视频剪辑, ffmpeg, 裁剪, 拼接
---

（专家指南，告诉 LLM 如何用 FFmpeg 完成任务、输出协议等）
```

关键词自动匹配，命中后任务强制走技能提示词。见现有技能示例。

## 测试

当前无自动化测试套件（Roadmap 中）。贡献测试是大欢迎项：
- 单元测试：`backend/tests/`（pytest）
- 集成测试：跑真实任务断言产物

## 行为准则

友善、尊重、对事不对人。见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
