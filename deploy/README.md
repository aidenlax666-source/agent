# 云部署（HTTPS 反代 / 证书 / 静态缓存）

本目录是云服务器上线 HTTPS 的现成配置，三件套：

| 文件 | 作用 |
|---|---|
| `nginx-https.conf` | Nginx 反向代理：HTTP→HTTPS 跳转、TLS 加固、安全响应头、前端/API/产物静态三路转发、静态长缓存、Range 流式放行 |
| `init-certbot.sh` | Let's Encrypt 证书首次签发 + 启用自动续期（certbot.timer） |
| `README.md` | 部署步骤 |

## 架构

```
客户端 ──▶ Nginx :443 (TLS)
              ├──▶ /          → 前端 Next.js :3000（含 WebSocket 升级）
              ├──▶ /api/      → 后端 API :8000
              └──▶ /assets/   → 产物静态 :8001（视频/图片/游戏/报告）
```

## 快速开始

```bash
# 1. 装 Nginx（如已装跳过）
sudo apt-get install -y nginx

# 2. 首次签发证书（一次性）
sudo bash deploy/init-certbot.sh your-domain.com your@email.com

# 3. 部署 Nginx 配置（改好域名后）
sudo cp deploy/nginx-https.conf /etc/nginx/sites-available/aiagent
sudo ln -sf /etc/nginx/sites-available/aiagent /etc/nginx/sites-enabled/aiagent
sudo rm -f /etc/nginx/sites-enabled/default   # 去掉默认站点
sudo nginx -t && sudo systemctl reload nginx
```

## 后端需要同步的 .env

Nginx 加 HTTPS 后，后端要信任 Nginx 转发的 X-Forwarded-For
（取最后一个值 = 客户端真实 IP，防伪造 IP 绕过限速）：

```env
TRUSTED_PROXY_IPS=127.0.0.1,::1        # 本机 Nginx 是可信代理
PUBLIC_API_BASE=https://your-domain.com
CORS_ORIGINS=https://your-domain.com
```

## 证书自动续期

certbot 安装时自带 `certbot.timer`（每天检查，到期前 30 天续期）。
验证：`sudo certbot renew --dry-run` 显示成功即 OK。
续期后 Nginx 自动加载新证书（certbot nginx 插件会重载）。

## 安全要点（已内置在配置里）

- **HSTS**：`Strict-Transport-Security` 强制 HTTPS
- **nosniff**：`X-Content-Type-Options` 防 MIME 嗅探（产物 HTML 不可内联执行）
- **X-Frame-Options**：防点击劫持
- **Range 流式**：产物视频/音频放行 Range 请求（边下边播）
- **WebSocket**：`Upgrade/Connection` 头转发（联机游戏实时通信）
