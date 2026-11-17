"""四层记忆测试：L0 工具契约 / L1 事实卡与工作集 / L2 工作区 / L3 组织档案。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------- 夹具
def _many_rows(n: int = 120) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/inventory"):
            return httpx.Response(200, json={
                "sku_id": "ST-92-C1-M", "on_hand": 900, "locked": 10, "reserved_crm": 5,
                "order_hold": 5, "available": 880, "as_of": "2026-11-02",
            })
        if path.endswith("/approvals"):
            return httpx.Response(200, json={
                "status": "all",
                "items": [{"approval_id": f"appr-{i}", "sku_id": "ST-92-C1-M", "qty": i}
                          for i in range(n)],
            })
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture()
def platform_client(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.tools import platform_tools

    settings = get_settings()
    monkeypatch.setattr(settings, "artifacts_dir", str(tmp_path / "artifacts"), raising=False)
    monkeypatch.setattr(settings, "analyses_dir", str(tmp_path / "analyses"), raising=False)
    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=_many_rows(),
    )
    platform_tools.set_platform_client(client)
    yield client
    platform_tools.set_platform_client(None)


def _tools(role: str = "admin", department: str | None = None):
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    user = AuthUser(username=role, role=role, department=department)
    return {t.name: t for t in build_platform_tools(user)}, user


# ---------------------------------------------------------------- L0 工具契约
def test_l0_small_result_has_result_id(seeded, platform_client):
    tools, _ = _tools()
    inv = asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    assert inv["available"] == 880  # 原有字段保持兼容
    assert inv["result_id"].startswith("res_")
    assert inv["as_of"]
    assert inv["truncated"] is False
    assert inv["summary"]


def test_l0_large_result_truncated_to_artifact(seeded, platform_client):
    tools, _ = _tools()
    data = asyncio.run(tools["list_pending_approvals"].ainvoke({}))
    assert data["truncated"] is True
    assert data["row_count"] == 120
    assert len(data["items"]) <= 3  # 只留预览
    path = Path(data["artifact_path"])
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 121  # 1 行 meta + 120 行明细
    assert "artifact_path" in data["summary"]


def test_l0_result_registered_in_ledger(seeded, platform_client):
    from app.memory.result_store import get_result

    tools, _ = _tools()
    inv = asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    row = get_result(inv["result_id"])
    assert row is not None
    assert row["tool"] == "get_inventory"
    assert row["as_of"]


def test_l0_prune_levels():
    from app.memory.prune import prune_tool_text

    short = "x" * 100
    assert prune_tool_text(short, (4096, 2048, 512)) == short
    long_text = "y" * 9000
    pruned = prune_tool_text(long_text, (4096, 2048, 512))
    assert "剪枝" in pruned
    assert len(pruned) < 600


# ---------------------------------------------------------------- L1 事实卡 / 工作集
def test_l1_facts_card_deterministic():
    from app.memory.session_memory import build_facts_card

    history = [
        {"role": "user", "content": "ST-13-C1-M 可用库存多少？", "tool_trace": []},
        {"role": "assistant", "content": "可用库存 42 件，预计 3 天售罄。", "tool_trace": [
            {"type": "call", "tool": "get_inventory", "args": {"sku_id": "ST-13-C1-M"}},
            {"type": "result", "tool": "get_inventory",
             "result": '{"available": 42, "result_id": "res_abc123def4"}'},
        ]},
    ]
    card, trigger = build_facts_card(history)
    assert trigger == "rules"
    assert card.startswith("<analysis-facts")
    assert "res_abc123def4" in card
    assert "可用库存 42" in card
    assert build_facts_card(history)[0] == card  # 同输入同输出（可复现）


def test_l1_working_set_union_and_reference():
    from app.memory.session_memory import (
        extract_entities,
        merge_working_set,
        working_set_text,
    )

    first = extract_entities("ST-13-C1-M 近7天有没有断码，黑色")
    ws1 = merge_working_set(None, first)
    second = extract_entities("这个款 M 码还能撑几天")
    ws2 = merge_working_set(ws1, second)
    assert "ST-13-C1-M" in ws2["skus"]
    assert ws2["version"] == 2
    assert "M" in ws2["sizes"]
    assert "近7天" in working_set_text(ws2)


def test_l1_refresh_memory_writes_facts_and_working_set(seeded):
    from app.agents import chat_store
    from app.db.engine import session_scope
    from app.memory import session_memory

    with session_scope() as session:
        sid = chat_store.create_session(session, "admin").id
        for i in range(7):
            chat_store.append_message(session, sid, "user", f"第{i}轮：ST-92-C1-M 库存多少")
            chat_store.append_message(session, sid, "assistant", "可用库存 880 件（res_aaaaaaaaaa）",
                                      [{"type": "result", "tool": "get_inventory",
                                        "result": '{"result_id": "res_aaaaaaaaaa"}'}])
        stats = session_memory.refresh_memory(session, sid, "ST-92-C1-M 断货风险如何，近7天")
        assert stats["facts_version"] >= 1

    with session_scope() as session:
        snap = session_memory.get_memory_snapshot(session, sid)
        assert snap is not None
        assert "<analysis-facts" in snap["facts_card"]
        assert "ST-92-C1-M" in snap["working_set"]["skus"]
        ctx = session_memory.build_memory_context(session, sid)
        assert "<working-set" in ctx["text"]


# ---------------------------------------------------------------- L2 工作区
def test_l2_workspace_baseline_and_skills():
    from app.memory import workspace

    baseline = workspace.load_workspace_baseline()
    assert "可用库存" in baseline
    items = workspace.list_skills()
    assert len(items) == 5
    assert all(item["summary"] for item in items)
    catalog = workspace.skills_catalog_text()
    assert len(catalog) < 1500  # 目录常驻但不能膨胀
    loaded = workspace.load_skill("restock-calc")
    assert loaded["name"] == "restock-calc"
    assert "再订货点" in loaded["content"]
    assert "error" in workspace.load_skill("no-such-skill")


def test_l2_system_prompt_contains_memory_layers(seeded, monkeypatch):
    from app.agents import chat_agent
    from app.auth import AuthUser

    async def fake_metadata():
        return "可用库存 = 现存 − 锁定", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", fake_metadata)
    user = AuthUser(username="worker1", role="user", department="男装")
    prompt, version = asyncio.run(chat_agent.build_system_prompt(
        user, memory_text="<working-set>SKU: ST-92-C1-M</working-set>",
        plan=None, session_ref_text="",
    ))
    assert version == 1
    assert "result_id" in prompt
    assert "L2 工作区基线" in prompt
    assert "load_skill" in prompt
    assert "<working-set>" in prompt


# ---------------------------------------------------------------- L3 组织记忆
def test_l3_save_and_search_by_department(seeded, platform_client):
    from app.db.engine import session_scope
    from app.memory import org_memory

    with session_scope() as session:
        male = org_memory.save_analysis(
            session=session, department="男装", username="worker1",
            question="ST-92-C1-M 过季积压处理", conclusion="建议先跨仓调拨再考虑折价清仓",
            key_metrics={"available": 880, "sell_through_days": 120},
            evidence=["res_1111111111"], metric_version="2026Q1",
        )
        org_memory.save_analysis(
            session=session, department="女装", username="worker2",
            question="ST-91-C1-M 断码补货", conclusion="S/L 码断码需补 120 件",
            key_metrics={"available": 60}, evidence=["res_2222222222"],
        )

    with session_scope() as session:
        hits = org_memory.search_analyses(session, "积压 调拨", departments=["男装"], limit=5)
        # 测试库是**会话级共享**的（别的用例也会写档案），所以按 id 断言"能检索到"，
        # 而不是断言"排第一"。
        assert male["analysis_id"] in {h["analysis_id"] for h in hits}
        assert all(h["department"] == "男装" for h in hits)
        # 女装用户看不到男装档案（部门隔离即权限）
        wrong = org_memory.get_analysis(session, male["analysis_id"], departments=["女装", "童装"])
        assert wrong is None
        ok = org_memory.get_analysis(session, male["analysis_id"], departments=None)
        assert ok is not None and "建议先跨仓调拨" in ok["markdown"]


def test_l3_session_reference_only_carries_conclusion(seeded, platform_client):
    from app.db.engine import session_scope
    from app.memory import org_memory

    with session_scope() as session:
        saved = org_memory.save_analysis(
            session=session, department="男装", username="worker1",
            question="周度复盘：男装断码", conclusion="本周断码集中在 ST-92",
            key_metrics={"gap_skus": 3}, evidence=["res_3333333333"],
        )
    with session_scope() as session:
        # 复审 S1：引用解析返回 {"status","token","items"}（命中/无权限/不存在三态）
        state = org_memory.resolve_session_reference(
            session, "@session 男装 周度复盘 断码", departments=["男装"],
        )
        text = org_memory.analysis_context(state)
    assert state["status"] == "ok"
    assert state["items"] and state["items"][0]["analysis_id"] == saved["analysis_id"]
    assert 'status="ok"' in text
    assert "本周断码集中在 ST-92" in text
    assert "res_3333333333" in text
