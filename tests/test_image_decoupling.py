"""解耦契约：`agents_image` 不得依赖 SKU 业务包，`app/core` 不得依赖任何业务包。

三个方向：
1. `agents_image/**` 禁止 import `app.agents*`（除自身）/`app.tools*`/`app.memory*`；
2. `app/agents/**`、`app/tools/**`、`app/memory/**` 禁止 import `agents_image`（只有 main.py 挂载）；
3. `app/core/**` 禁止 import 任何业务包。
另外：`APP_MODULES=image` 时能独立启动（子进程冒烟，真跑一次 TestClient）。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def _imports(path: Path) -> list[str]:
    # utf-8-sig：个别历史文件的 __init__.py 带 BOM
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
    return mods


def _is_sku_business(module: str) -> bool:
    """SKU 业务包（`app.agents_image` 不算：它自己）。"""
    if module == "app.agents_image" or module.startswith("app.agents_image."):
        return False
    return module in {"app.agents", "app.tools", "app.memory"} or any(
        module.startswith(prefix + ".") for prefix in ("app.agents", "app.tools", "app.memory")
    )


def test_agents_image_does_not_import_sku_business():
    offenders: list[str] = []
    for path in (SRC / "app" / "agents_image").rglob("*.py"):
        for module in _imports(path):
            if _is_sku_business(module):
                offenders.append(f"{path.relative_to(SRC)} -> {module}")
    assert not offenders, "agents_image 不应依赖 SKU 业务包：" + "; ".join(offenders)


def test_sku_business_does_not_import_agents_image():
    offenders: list[str] = []
    for sub in ("agents", "tools", "memory"):
        for path in (SRC / "app" / sub).rglob("*.py"):
            for module in _imports(path):
                if module == "app.agents_image" or module.startswith("app.agents_image."):
                    offenders.append(f"{path.relative_to(SRC)} -> {module}")
    assert not offenders, "SKU 业务包不应反向依赖 agents_image：" + "; ".join(offenders)


def test_core_has_no_business_dependency():
    offenders: list[str] = []
    for path in (SRC / "app" / "core").rglob("*.py"):
        for module in _imports(path):
            if module.startswith(("app.agents", "app.tools", "app.memory")):
                offenders.append(f"{path.relative_to(SRC)} -> {module}")
    assert not offenders, "app/core 必须是业务无关的：" + "; ".join(offenders)


def test_http_layer_does_not_import_orchestration_nodes():
    """P2-8（第九轮）：规划逻辑收进 `planning.py` 后，HTTP 路由不再直接依赖编排节点。

    过去 `api/routes.py` 直接 import `nodes.plan_slots` / `nodes.validate`，
    规划口径与校验规则在"API 层 + 编排层"两处各写一份；现在只有 planning 一处。
    """
    from app.agents_image import planning
    from app.agents_image.api import routes

    source = Path(routes.__file__).read_text(encoding="utf-8")
    assert "from app.agents_image.nodes" not in source, "HTTP 层不应直接依赖编排节点"
    # 兼容别名：老测试/脚本按 routes._xxx 引用
    assert routes._attrs_for_target is planning.attrs_for_target
    assert routes._variant_matrix is planning.variant_matrix
    assert routes._resolve_targets is planning.resolve_targets


def test_parse_modules_default_and_override():
    from app.main import parse_modules

    assert parse_modules(None) == {"sku", "image"}
    assert parse_modules("") == {"sku", "image"}
    assert parse_modules("image") == {"image"}
    assert parse_modules(" sku , ") == {"sku"}
    assert parse_modules("SKU,Image") == {"sku", "image"}


def test_image_only_mode_boots_without_sku(tmp_path):
    """子进程里用 APP_MODULES=image 真启一次服务：image 路由在、SKU 路由不在。"""
    env = os.environ.copy()
    env["APP_MODULES"] = "image"
    env["PYTHONPATH"] = str(SRC)
    env["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'image_only.db').as_posix()}"
    env["IMAGE_ENABLED"] = "true"
    env["PYTHONIOENCODING"] = "utf-8"   # 子进程日志是中文，避免父进程按 GBK 解码报警
    env["IMAGE_STORAGE"] = "local"
    env["IMAGE_CLEANUP_INTERVAL_MINUTES"] = "0"
    script = (
        "import json\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "with TestClient(app) as client:\n"
        "    health = client.get('/api/health')\n"
        "    img = client.get('/api/image/health')\n"
        "    skus = client.get('/api/skus')\n"
        "    agents_page = client.get('/agents.html')\n"
        "    print(json.dumps({\n"
        "        'health': health.status_code,\n"
        "        'health_image': (health.json().get('image') or {}).get('provider') if health.status_code == 200 else None,\n"
        "        'image_health': img.status_code,\n"
        "        'skus': skus.status_code,\n"
        "        'agents_page': agents_page.status_code,\n"
        "    }))\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=str(ROOT), timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["health"] == 200
    assert data["health_image"] == "fake"          # image 模块的 health 段被注册进来了
    assert data["image_health"] == 200
    assert data["skus"] == 404                      # SKU 路由未挂载
    assert data["agents_page"] == 200
