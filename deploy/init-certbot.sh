#!/usr/bin/env bash
# ============================================================
# Let's Encrypt 证书签发 + 自动续期（云服务器）
#
# 用法：
#   sudo bash deploy/init-certbot.sh your-domain.com your@email.com
#
# 首次执行：签发证书并配置 Nginx
# 之后续期自动完成（certbot 自带 systemd timer，无需手动）
# 手动测试续期：sudo certbot renew --dry-run
# ============================================================
set -euo pipefail

DOMAIN="${1:?用法: init-certbot.sh <域名> <邮箱>}"
EMAIL="${2:?用法: init-certbot.sh <域名> <邮箱>}"

echo "==> 安装 certbot + nginx 插件"
sudo apt-get update
sudo apt-get install -y certbot python3-certbot-nginx

echo "==> 签发证书（域名: $DOMAIN）"
sudo certbot certonly --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --no-eff-email

echo "==> 确认续期定时器已启用"
sudo systemctl enable certbot.timer
sudo systemctl start certbot.timer
sudo systemctl list-timers certbot.timer

echo ""
echo "==> 完成。请确认 deploy/nginx-https.conf 里的域名已改为 $DOMAIN，"
echo "    然后: sudo nginx -t && sudo systemctl reload nginx"
