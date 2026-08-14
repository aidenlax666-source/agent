# 🤖 AI 通用自动化 Agent

> 一句话描述需求 → AI 自动写代码、执行、四重校验、迭代修改、分享结果。
> 支持网页抓取、办公文档、图片/PDF、AI 漫剧、音乐、视频生成等，无需写代码。

## 核心能力

### 一句话自动化（`/mini`）
- **自然语言 → 脚本 → 执行 → 四重校验**（数量/字段/功能覆盖/数据值），发现错误自动修复
- **迭代修改**：对结果不满意，直接提修改意见，AI 在原任务上重跑
- **确认流**：满意一键确认，结果即最终版
- **任务历史**：SQLite 持久化，重启不丢，随时回看
- **定时执行**：任务可设置每隔 N 分钟 / 每天 HH:MM 自动重复
- **图片上传**：上传需求截图/参考图，豆包视觉识别后自动传入 DeepSeek

### 内容生成（分享中心 `/gallery`）
- **可视化报告**：需求含"报告/可视化"自动生成精美 HTML 报告（统计卡片+SVG 图表），公网可分享
- **网页游戏 / AI 漫剧 / AI 音乐 / AI 视频 / AI 图片**：输入主题即生成，二维码+链接分享
- **zip 打包**：全部作品一键打包下载

### 多模态（火山方舟/豆包）
| 能力 | 模型 | 用途 |
|---|---|---|
| 图片理解 | `doubao-seed-1-6-vision-250815` | DeepSeek 看不懂图片时识别总结传给它 |
| 文生视频 | `doubao-seedance-2-0-260128` | 提示词 → 5s 视频 |
| 图生视频 | seedance（带参考图） | 图片 → 动态视频 |
| 文生图 | `doubao-seedream-4-0-250828` | 提示词 → 插画图片 |
| TTS 配音 | edge-tts（优先）/ Windows SAPI | 文本 → 语音（漫剧旁白等） |

### 数据处理（`/mini` 数据问答）
- 上传 Excel/CSV → 自然语言提问（"哪个产品销量最高？"）→ AI 直接回答

## 快速启动

```powershell
# 一键启动（后端 8000 + 前端 3000 + 分享服务 8001）
powershell -ExecutionPolicy Bypass -File start.ps1

# 加公网隧道（可分享链接）
powershell -ExecutionPolicy Bypass -File start.ps1 -tunnel
```

或手动：
```bash
# 后端
cd backend && pip install -r requirements.txt
uvicorn app.main:app --port 8000

# 前端
cd frontend && npm install && npm run dev

# 分享服务（web 作品静态托管）
python -m http.server 8001 --directory web
```

访问：
- 前端 http://localhost:3000（一句话自动化 `/mini`、分享中心 `/gallery`）
- 后端 API http://localhost:8000/docs

## 环境变量（.env）

```env
# DeepSeek（文本大脑）
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_API_KEYS=sk-a,sk-b          # 多 key 逗号分隔（负载均衡）
AI_MODEL=deepseek-chat
AI_MODEL_REASONING=deepseek-reasoner # 复杂任务用推理模型

# 豆包/火山方舟（多模态：视觉识别/视频/图片生成）
DOUBAO_API_KEY=ark-xxx
DOUBAO_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
DOUBAO_VISION_MODEL=doubao-seed-1-6-vision-250815

# 沙箱
SANDBOX_TIMEOUT=60
SANDBOX_ALLOW_SUBPROCESS=true

# JWT
JWT_SECRET_KEY=xxx
```

## API 概览

| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/api/mini/tasks` | 提交一句话任务（可带 url/image_paths），扣 1 额度 |
| GET | `/api/mini/tasks` | 任务历史列表 |
| GET | `/api/mini/tasks/{id}` | 任务状态/结果 |
| POST | `/api/mini/tasks/{id}/iterate` | 迭代修改（提反馈重跑） |
| POST | `/api/mini/tasks/{id}/confirm` | 确认结果（最终版） |
| POST | `/api/mini/tasks/{id}/cancel` | 取消运行中任务 |
| POST | `/api/mini/tasks/{id}/schedule` | 设置定时执行 |
| POST | `/api/mini/tasks/{id}/download` | 下载结果文件 |
| POST | `/api/mini/qa` | 数据问答（file_path + question） |
| GET | `/api/gallery` | 分享作品列表 |
| GET | `/api/gallery/download-zip` | 作品 zip 打包下载 |
| POST | `/api/upload` | 上传文件（截图/数据文件） |
| POST | `/api/sessions/login` | 打开浏览器登录（保存登录态） |

## 架构

```
用户一句话
   │
   ▼
统一任务对象（普通/报告/定时，可带图片）
   │
   ▼
DeepSeek 结构化理解 → 生成 Python 脚本（分领域提示词模板）
   │
   ▼
沙箱执行 → 四重校验（数量/字段/覆盖/值）→ 自愈 → 预览
   │
   ▼
结果：Excel/HTML报告/图片/视频/漫剧/音乐 + 迭代/确认/定时/分享
   │
   ▼
多模态补盲区：DeepSeek 看不懂的（图片/视频）→ 豆包视觉/Seedance
```

服务结构：`backend/app/services/mini_generator.py`（核心闭环）、`mini_tasks.py`（后台队列+持久化+定时）、`vision_client.py`/`video_client.py`/`tts_client.py`（多模态）、`api/mini.py`/`gallery.py`（API）。

## 项目结构

```
ai-automation-generator/
├── backend/
│   ├── app/
│   │   ├── api/          auth, auth_sessions, upload, mini, gallery   ← 后端 API
│   │   ├── services/     mini_generator, mini_tasks, vision/video/tts_client,
│   │   │                 page_capture, self_healing, llm_client, long_task
│   │   ├── sandbox/      docker_executor + 安全扫描/自动修复
│   │   ├── config.py / database.py / main.py
│   │   └── requirements.txt
│   └── Dockerfile
├── frontend/
│   └── src/app/          login, register, mini（一句话自动化）, gallery（分享中心）
├── make_*.py             AI 内容生成器：游戏 / 网页游戏 / 可视化报告 / AI漫剧 / 音乐
├── gen_*.py              生成器产物脚本
├── start.ps1             一键启动（后端+前端+分享服务+可选公网隧道）
├── docker-compose.yml
└── README.md
```

> 说明：仓库只包含核心代码与内容生成器；测试脚本与运行产物（batch_*.py、test_*.py、*_result.json、调试图片等）不入库（见 `.gitignore`）。


## 验证成绩（真实测试）

- 100 个通用任务连测 98%+（Excel/Word/PPT/文件/数据/文本/API/图片/PDF）
- 复合长难任务（3-6 维度组合）9/9 通过，数据精确（与官方 API 逐项核对）
- 小红书登录态抓取 3/3、多模态（图片识别/视频/图生视频）实测通过
- 四重校验拦截假通过（数量不足/漏字段/漏功能/值异常自动修复）
