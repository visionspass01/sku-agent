"""部署态守卫的回归测试：客户端 IP 解析、默认密钥自检、向量维度与 provider 一致。

这三项都是「容器部署才暴露、本地裸跑看不出来」的问题（见 docs/DEPLOY.md 第 3、9 节），
因此单独成文件。
"""

from __future__ import annotations


class _FakeRequest:
    """只实现 `_client_ip` 用到的两个属性（headers / client）。"""

    def __init__(self, headers: dict[str, str] | None = None, host: str | None = "10.1.2.3"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})() if host else None


def test_client_ip_prefers_forwarded_for_first_hop():
    from app.api.routes import _client_ip

    req = _FakeRequest({"x-forwarded-for": "203.0.113.9, 172.18.0.5"})
    assert _client_ip(req) == "203.0.113.9", "应取 X-Forwarded-For 首段，而不是代理内网 IP"


def test_client_ip_falls_back_to_real_ip_and_peer():
    from app.api.routes import _client_ip

    assert _client_ip(_FakeRequest({"x-real-ip": "198.51.100.7"})) == "198.51.100.7"
    # 无代理头时保持原行为：取直连地址
    assert _client_ip(_FakeRequest()) == "10.1.2.3"
    assert _client_ip(_FakeRequest(host=None)) == "-"


def test_security_health_flags_public_defaults(_settings, monkeypatch):
    from app.api.routes import _security_health
    from app.config import DEFAULT_AUTH_SECRET, DEFAULT_PLATFORM_INTERNAL_TOKEN

    # 默认值 → 两个标记都为 True（用 monkeypatch 改，避免污染同 session 的共享 settings）
    monkeypatch.setattr(_settings, "auth_secret", DEFAULT_AUTH_SECRET)
    monkeypatch.setattr(_settings, "platform_internal_token", DEFAULT_PLATFORM_INTERNAL_TOKEN)
    snap = _security_health()
    assert snap["secret_is_default"] is True
    assert snap["platform_token_is_default"] is True
    assert snap["ok"] is False
    assert "openssl" in snap["note"]

    # 覆盖成随机值 → 通过
    monkeypatch.setattr(_settings, "auth_secret", "a" * 64)
    monkeypatch.setattr(_settings, "platform_internal_token", "b" * 32)
    snap = _security_health()
    assert snap["ok"] is True
    assert snap["note"] == ""


def test_nginx_redirects_https_to_http():
    """浏览器默认用 https 打开 IP 时，443 必须 301 到 http（自签证书兜底）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    nginx = (root / "docker" / "nginx.conf").read_text(encoding="utf-8")
    assert "listen 443 ssl" in nginx, "需要 443 TLS 监听才能做跳转"
    assert "return 301 http://$host$request_uri;" in nginx, "443 必须 301 到 http"
    assert "redirect-selfsigned.pem" in nginx and "redirect-selfsigned.key" in nginx

    deploy = (root / "docker" / "docker-compose.deploy.yml").read_text(encoding="utf-8")
    assert '"443:443"' in deploy, "deploy compose 必须暴露 443"
    assert "./certs:/etc/nginx/certs:ro" in deploy, "需要挂载证书目录"

    overlay = (root / "docker" / "docker-compose.https.yml").read_text(encoding="utf-8")
    assert '"443:443"' not in overlay, "overlay 不应重复声明 443（基础文件已映射，重复会冲突）"

    script = (root / "scripts" / "gen_redirect_cert.sh").read_text(encoding="utf-8")
    assert "openssl req -x509" in script, "自签证书应由脚本生成，而不是入库"
    assert "subjectAltName=IP:" in script, "IP 访问需要 IP SAN"


def test_vector_dim_follows_actual_provider(_settings, monkeypatch):
    """`EMBEDDING_BACKEND=tencent` 但缺 Key 时，维度必须回落到哈希 512（否则集合维度错配）。"""
    from app.rag import embedding, vector_store

    monkeypatch.setattr(embedding, "_provider", None, raising=False)
    monkeypatch.setattr(embedding, "_provider_error", "", raising=False)

    monkeypatch.setattr(_settings, "embedding_backend", "hash")
    assert vector_store._dim_for_embedding() == 512

    monkeypatch.setattr(_settings, "embedding_backend", "tencent")
    monkeypatch.setattr(_settings, "tencent_embedding_api_key", "")
    monkeypatch.setattr(embedding, "_provider", None, raising=False)
    assert vector_store._dim_for_embedding() == 512, "缺 Key 时 provider 降级哈希，维度应为 512"

    monkeypatch.setattr(_settings, "tencent_embedding_api_key", "sk-test-not-used")
    monkeypatch.setattr(_settings, "tencent_embedding_model", "kinfra-text-embedding-0.6b")
    monkeypatch.setattr(embedding, "_provider", None, raising=False)
    assert vector_store._dim_for_embedding() == 1024, "有 Key 时按腾讯 0.6b 模型取 1024 维"

    # 收尾：还原全局 provider，避免影响其它用例
    monkeypatch.setattr(embedding, "_provider", None, raising=False)
