# 安全设计（Security）

本项目在安全上做了系统性投入。以下按威胁模型列出防护措施。适合面试安全话题时展开。

---

## 威胁模型

本项目让 **LLM 生成代码并执行**，核心威胁是：
1. 生成的代码恶意/意外访问宿主机敏感资源（文件、网络、命令）
2. 用户上传的 zip 触发路径穿越 / 资源耗尽
3. 跨用户数据泄漏（任务、登录态、匿名身份冒用）
4. 产物页（LLM 生成的 HTML）XSS 窃取同源凭据
5. 成本滥用（LLM API 费用、磁盘/内存耗尽）

---

## 防护矩阵

### 1. 代码执行隔离（最重要）

| 层 | 措施 |
|---|---|
| 首选 | **Docker 沙箱**：无 `--privileged`、脚本目录只读挂载（`/scripts` ro）、输出目录独立（`/output` rw）、内存 256-512MB 限额、CPU 配额、bridge 网络（非 host）、超时 + 无输出卡死检测 + 进程树清理 |
| 回退 | 无 Docker 时宿主 subprocess：独立临时工作目录 + 环境变量脱敏（剥离 KEY/SECRET/TOKEN/PASSWORD/JWT）+ 输出字节预算 + 超时 + 进程组清理 |
| 前置 | **AST 静态扫描**（见下） |

### 2. AST 静态扫描（`backend/app/sandbox/security.py`）

执行前对生成代码做语法树分析，拦截：

- **动态执行**：`eval/exec/compile/globals/locals/vars/__import__`
- **命令执行**：`os.system/popen/fork/spawn*/startfile`，含绕过形态：
  - `getattr(os, "system")`
  - `os.__dict__["system"]`
  - `from os import system`
  - `import builtins; builtins.eval(...)`
- **SSRF**：字符串常量中的内网/回环/链路本地/云元数据地址（含 IPv6、进制编码变体）；域名做 **DNS 解析后按 IP 判定**；非 http(s) scheme 一律拒绝
- **敏感文件读取**：`open(".env")`、`read_text`、`load_workbook` 等作用于 `.env/.db/.pem/.key/browser_profile/config.py` 等路径
- **登录态外泄**：注入的 `_AUTH`（用户登录 Cookie 的 storage_state）只允许出现在 `browser.new_context(storage_state=_AUTH)`；print/写盘/网络调用/赋值后外发 → 拦截
- **越权删除**：`os.remove/shutil.rmtree` 作用于项目目录外路径
- **`__builtins__` 走私**：任何 `__builtins__[...]` / 属性链访问

### 3. 命令执行安全（dev 管线）

- 危险命令黑名单（rm/del/format/shutdown/taskkill…）
- **Windows cmd 分隔符 `&` 拦截**（等价 bash `;`，只放行 `&&` 步骤串联）——这是审计发现的真实绕过点
- 超时限制（300s）+ 输出截断

### 4. 上传/解压防护

- **zip-slip**：解压前逐成员校验 `normpath` 后必须落在目标目录内
- **zip 炸弹**：文件数 ≤5000、单文件 ≤20MB、总大小 ≤200MB，且**按实际写出字节计数**（不信任 zip 头声明的 file_size）
- 上传：扩展名白名单 + 大小上限 200MB + 按 IP 限速（匿名/注册用户都限）

### 5. 产物 XSS 隔离

- 产物页（LLM 生成的 HTML/SVG/XML）与 API **不同源**：API 不挂载 web/ 静态目录
- 下载端点对 html/svg/xml 强制 `application/octet-stream + X-Content-Type-Options: nosniff + Content-Disposition: attachment`
- 产物外泄防护：`[OUTPUT_FILE]` 协议只接受沙箱输出目录或 web/ 内的路径（`_safe_output_src` realpath 校验）——防 LLM 脚本把 `.env` 等任意文件"发布"成可下载产物

### 6. 认证与数据隔离

- JWT：算法白名单（HS256）、密钥从环境变量读取
- **匿名身份绑定 IP**：`sha256(IP|anon_id)` 派生，脱离 IP 的 id 无效——防冒用他人匿名任务/积分/登录态
- **XFF 可信校验**：仅可信代理（本机回环）时信任 X-Forwarded-For，且取**末值**（首值客户端可伪造）
- 任务/提醒/监控/通知全部按 `user_id` 归属校验
- 积分原子扣减（`UPDATE ... WHERE credits >= ?`）
- 登录限速 + 邮箱锁定

### 7. 数据层

- SQL 全参数化（`?` 绑定）
- 动态字段名只来自硬编码白名单（`_ALLOWED_MINI_UPDATE_FIELDS`）
- `ON CONFLICT` 不更新 `user_id`（防任务归属被改写）

### 8. 资源控制

- 匿名提交限速（10/min/IP）、dev 接口限速
- 沙箱并发上限、超时上限（1800s 硬顶）
- 产物定期清理（可配置）
- 上传限速防刷磁盘

---

## 已知边界（诚实声明）

1. **宿主回退模式不是隔离**：Windows 本地无 Docker 时脚本直接在宿主执行，AST 扫描是 best-effort 防线。**生产部署必须启用 Docker**（代码已支持，自动降级为 fail-open 是本地开发便利性取舍）。
2. **静态扫描无法 100% 覆盖**：别名（`import os as x`）、动态属性访问等仍可能绕过，需运行时沙箱兜底。
3. 登录态 Cookie 以明文 JSON 存于 `browser_profile/{user_id}/`（本地单用户场景）；多用户生产建议加密落盘。
