# API 概览

Base URL: `http://localhost:8000/api` · 交互文档: `http://localhost:8000/docs`

鉴权：`Authorization: Bearer <JWT>`（登录用户）或 `X-Anonymous-Id: <id>`（匿名会话）。

---

## 任务（/mini/tasks）

| 方法 | 路径 | 功能 | 说明 |
|---|---|---|---|
| POST | `/mini/tasks` | 提交一句话任务 | body: `{requirement, url?, image_paths?, data_paths?}`；扣 1 积分；支持自动化意图（提醒/监控/定时） |
| GET | `/mini/tasks` | 任务历史 | `?limit=20` |
| GET | `/mini/tasks/{id}` | 任务状态/结果 | 含 status/progress/result/error |
| POST | `/mini/tasks/{id}/iterate` | 迭代修改 | body: `{feedback}`；原任务上重跑 |
| POST | `/mini/tasks/{id}/confirm` | 确认结果 | 结果即最终版 |
| POST | `/mini/tasks/{id}/cancel` | 取消运行中 | |
| POST | `/mini/tasks/{id}/schedule` | 定时执行 | `{schedule_type: interval\|daily, schedule_value, enabled}` |
| GET | `/mini/tasks/{id}/download` | 下载结果文件 | 强制下载 + nosniff |

## 开发助手（/dev）

| 方法 | 路径 | 功能 | 说明 |
|---|---|---|---|
| POST | `/dev/tasks` | 一句话开发任务 | multipart: `requirement` + `file`(zip)；返回 dev_diff/dev_modified_zip |
| POST | `/dev/plan` | 出修改方案（不写文件） | multipart: `requirement` + `file`(zip) + `feedback?` |
| POST | `/dev/apply` | 按方案落地改动 | multipart: `requirement` + `plan` + `file`(zip) + `feedback?` |

dev 接口都支持模型输出 **patch（unified diff）** 而非完整文件——见 [ARCHITECTURE.md](ARCHITECTURE.md#2-apply_patch-协议省输出-token-的核心)。

## 自动化管理

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/automations` | 提醒 + 监控列表 |
| POST | `/reminders` | 创建提醒 `{time: "HH:MM", text}` |
| DELETE | `/reminders/{rid}` | 删除（幂等） |
| POST | `/reminders/{rid}/toggle` | 启停 |
| POST | `/monitors` | 创建监控 `{type: window\|screen, ...}` |
| DELETE | `/monitors/{mid}` | 删除（幂等） |
| POST | `/monitors/{mid}/toggle` | 启停 |

## 其他

| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/upload` | 上传文件（截图/数据），白名单扩展名 |
| POST | `/mini/qa` | 数据问答 `{file_path, question}` |
| GET | `/gallery` | 分享作品列表 |
| GET | `/gallery/download-zip` | 作品打包下载 |
| POST | `/sessions/login` | 打开浏览器登录（保存登录态，SSRF 拦截内网） |
| GET | `/sessions/status` | 登录窗口状态（仅本人） |
| POST | `/auth/register` `/auth/login` | 注册/登录 |
| GET | `/health` | 健康检查 |

## 任务状态机

```
queued → running → done / error / cancelled / confirmed
              ↘ no_data / login_required / robots_blocked（业务状态）
```

## 自动化意图解析（纯正则，零成本）

提交任务时后端用正则解析需求文本，命中即自动分支：

- `每天 9:00 提醒我喝水` → 创建定时提醒
- `监控屏幕，当出现 XX 时提醒我` → 创建屏幕监控
- `每隔 30 分钟执行 XX` → 创建循环任务
