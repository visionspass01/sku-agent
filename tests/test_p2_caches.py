"""P2 性能优化测试：撤销令牌 TTL 缓存、health 线程计数缓存。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def clean_auth():
    from app.auth import reset_revocation_cache

    reset_revocation_cache()
    yield
    reset_revocation_cache()


def _token() -> str:
    from app.auth import AuthUser, issue_token

    return issue_token(AuthUser(username="worker1", role="user", department="男装"))


def test_revocation_lookup_is_cached(monkeypatch, clean_auth):
    """第二次校验同一 token 不再查库（TTL 内），但登出仍立即生效。"""
    from app import auth

    calls = {"n": 0}

    def _fake_lookup(token):  # noqa: ANN001
        calls["n"] += 1
        return False

    monkeypatch.setattr(auth, "_lookup_revoked_in_db", _fake_lookup)
    token = _token()
    assert auth.is_revoked(token) is False
    assert auth.is_revoked(token) is False
    assert calls["n"] == 1, "TTL 内应命中缓存，只查一次库"


def test_revocation_cache_expires(monkeypatch, clean_auth, _settings):
    from app import auth

    calls = {"n": 0}
    monkeypatch.setattr(auth, "_lookup_revoked_in_db", lambda token: calls.__setitem__("n", calls["n"] + 1) or False)
    monkeypatch.setattr(_settings, "revocation_cache_seconds", 0.0)
    token = _token()
    auth.is_revoked(token)
    auth.is_revoked(token)
    assert calls["n"] == 2, "TTL=0 时应每次重新校验"


def test_logout_takes_effect_immediately(clean_auth):
    """登出走进程内黑名单，不受缓存影响（先缓存"未撤销"再登出，也必须立刻失效）。"""
    from starlette.exceptions import HTTPException

    from app.auth import decode_token, is_revoked, revoke_token

    token = _token()
    assert is_revoked(token) is False     # 写入"未撤销"缓存
    revoke_token(token)
    assert is_revoked(token) is True
    with pytest.raises(HTTPException):
        decode_token(token)


def test_health_thread_count_is_cached(monkeypatch, _settings):
    from app.agents import checkpoint

    monkeypatch.setattr(_settings, "health_cache_seconds", 60.0)
    checkpoint._health_cache.update({"at": 0.0, "threads": None})
    calls = {"n": 0}

    def _fake_count():
        calls["n"] += 1
        return 7

    monkeypatch.setattr(checkpoint, "_count_threads", _fake_count)
    assert checkpoint.checkpoint_health()["threads"] == 7
    assert checkpoint.checkpoint_health()["threads"] == 7
    assert calls["n"] == 1, "TTL 内应复用缓存"
    checkpoint._health_cache.update({"at": 0.0, "threads": None})


def test_health_endpoint_still_has_fields(app_client):
    body = app_client.get("/api/health").json()
    assert body["checkpoint"]["max_threads"] >= 1
    assert "threads" in body["checkpoint"]
    assert body["agents"]["count"] >= 3
