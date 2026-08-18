import logging
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # AI - DeepSeek（支持多个 key，用逗号分隔，用于负载均衡和限流切换）
    deepseek_api_key: str = "sk-your-deepseek-api-key"  # 单个 key（兼容旧配置）
    deepseek_api_keys: str = ""  # 多个 key 用逗号分隔，如 sk-key1,sk-key2
    deepseek_base_url: str = "https://api.deepseek.com"
    ai_model: str = "deepseek-chat"
    ai_model_reasoning: str = "deepseek-reasoner"  # 复杂任务用的推理模型
    # 开发改码（/dev/apply 主生成）使用的模型：空 = 用 ai_model（deepseek-chat，快）；
    # 需要更高质量的大项目代码可在 .env 设为 deepseek-reasoner（慢但更稳）
    dev_modify_model: str = ""

    # 可选 LLM 提供商（统一走 OpenAI 兼容 /chat/completions 接口；留空则只用 DeepSeek）
    llm_provider: str = "deepseek"  # deepseek | openai | anthropic | ollama | custom
    openai_api_key: str = ""        # llm_provider=openai 时必填
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""     # llm_provider=anthropic 时必填
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    ollama_base_url: str = "http://localhost:11434/v1"  # Ollama 本地（OpenAI 兼容端点）
    ollama_model: str = "qwen2.5:7b"
    custom_openai_base_url: str = ""   # 任何 OpenAI 兼容服务（如 OneAPI/LiteLLM）
    custom_openai_key: str = ""
    custom_openai_model: str = ""

    # AI - 豆包（火山方舟，多模态视觉：DeepSeek 识别不了图片时用它识别并总结）
    doubao_api_key: str = ""  # ark- 开头的火山方舟 key
    doubao_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_vision_model: str = "doubao-seed-1-6-vision-250815"  # 视觉模型名或接入点 ep-xxx

    # 豆包 TTS（火山引擎语音控制台，X-Api-Key 鉴权，与方舟 ark- key 不同）
    doubao_tts_api_key: str = ""
    doubao_tts_voice: str = "zh_female_shuangkuaisisi_uranus_bigtts"  # 默认音色（爽快思思 2.0）

    # 反爬适配域名（逗号分隔）：这些站点的抓取任务走"有头+人速+验证码等待"模式
    anti_bot_domains: str = "xiaohongshu.com,zhihu.com,weibo.com,douban.com,taobao.com,jd.com"

    # 产物页面（游戏/内容/报告等）需要连接的 API 域（与产物静态服务不同源，防止同源 XSS）
    public_api_base: str = "http://localhost:8000"

    # 产物/上传文件清理（天）：0=不清理（默认）。>0 时每天清理超过 N 天的 web/ 产物与 uploads/ 上传文件
    asset_cleanup_days: int = 0

    # Sandbox
    sandbox_image: str = "python:3.11-slim"
    sandbox_timeout: int = 60
    sandbox_max_timeout: int = 1800  # 单次执行总超时上限（秒），挡掉 LLM 估算的荒谬值
    sandbox_inactivity_timeout: int = 120  # 无新日志输出的秒数，超过判定卡死
    sandbox_max_concurrency: int = 3  # 同时最多执行的脚本数，超出排队
    sandbox_allow_subprocess: bool = True  # False 时沙箱禁止 import subprocess/socket/smtplib/ftplib
    sandbox_headful: bool = True  # True=有头浏览器（弹窗口，反爬通过率高）；False=无头（服务器可用）

    # JWT
    jwt_secret_key: str = ""  # 必须在 .env 配置；为空时启动生成临时密钥并告警
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    # App
    app_env: str = "development"
    debug: bool = True
    cors_origins: str = "http://localhost:3000"  # 逗号分隔的允许来源

    # 可信代理 IP（逗号分隔）：仅当直连来源在此白名单（含本机回环）时才信任
    # X-Forwarded-For 头（防伪造 IP 绕过限速）；留空/不含直连 IP 则一律用直连 IP
    trusted_proxy_ips: str = "127.0.0.1,::1"

    class Config:
        # 固定用仓库根目录的 .env（与 start.ps1 的启动目录一致），避免按 CWD 漂移
        env_file = str(Path(__file__).resolve().parent.parent.parent / ".env")
        case_sensitive = False
        extra = "ignore"  # 忽略 .env 里遗留的旧配置项（如 DATABASE_URL/REDIS_URL）


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    if not s.jwt_secret_key:
        # 未配置密钥时生成临时密钥（仅 demo 可用，重启后旧 token 失效）
        s.jwt_secret_key = secrets.token_hex(32)
        logging.getLogger("app.config").warning(
            "JWT_SECRET_KEY 未配置，已生成临时密钥（重启后旧 token 失效）。"
            "生产环境请在 .env 里设置随机密钥。"
        )
    return s
