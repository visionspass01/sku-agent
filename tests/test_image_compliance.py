"""D16：像素级合规校验（PNG 零依赖）+ `--fix` + 夹具生成。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    make = _load("make_image_fixtures", ROOT / "scripts" / "make_image_fixtures.py")
    directory = tmp_path_factory.mktemp("fixtures")
    return make.build(directory)


@pytest.fixture(scope="module")
def checker():
    return _load("check_image", ROOT / "scripts" / "check_image.py")


def test_png_decoder_matches_written_pixels():
    from app.agents_image.pixels import decode_png
    from app.agents_image.png import encode_png

    rows = bytes((10, 20, 30) * 4 + (200, 210, 220) * 4)
    data = encode_png(8, 1, rows)
    width, height, channels, pixels = decode_png(data)
    assert (width, height, channels) == (8, 1, 3)
    assert pixels[:3] == bytearray((10, 20, 30))


def test_compliant_fixture_passes(checker, fixtures):
    result = checker.check(fixtures["ok"], platform="淘系")
    assert result["ok"] is True, result
    assert result["stats"]["pixel_check"] is True
    assert all(item["passed"] for item in result["checks"])


def test_dirty_white_background_fails(checker, fixtures):
    result = checker.check(fixtures["dirty_white"], platform="淘系")
    assert result["ok"] is False
    failed = {item["name"] for item in result["checks"] if not item["passed"]}
    assert "白底纯度" in failed


def test_tiny_subject_reports_subject_and_margin(checker, fixtures):
    result = checker.check(fixtures["tiny_subject"], platform="淘系")
    assert result["ok"] is False
    failed = {item["name"] for item in result["checks"] if not item["passed"]}
    assert "主体占比" in failed or "四周留白" in failed


def test_non_square_fixture_fails_aspect(checker, fixtures):
    result = checker.check(fixtures["not_square"], platform="淘系")
    assert result["ok"] is False
    failed = {item["name"] for item in result["checks"] if not item["passed"]}
    assert "比例" in failed


def test_oversized_file_fails_size(checker, fixtures):
    result = checker.check(fixtures["too_large"], platform="淘系")
    assert result["ok"] is False
    failed = {item["name"] for item in result["checks"] if not item["passed"]}
    assert "文件大小" in failed


def test_fix_repairs_dirty_white_and_aspect(checker, fixtures, tmp_path):
    dirty = checker.fix(fixtures["dirty_white"], tmp_path / "fixed_dirty.png", platform="淘系")
    assert dirty["ok"] is True, dirty

    square = checker.fix(fixtures["not_square"], tmp_path / "fixed_square.png", platform="淘系")
    assert square["stats"]["width"] == square["stats"]["height"]
    assert square["ok"] is True, square


def test_text_detect_is_advisory_only(checker, fixtures):
    result = checker.check(fixtures["ok"], platform="淘系", text_detect=True)
    names = {item["name"]: item for item in result["checks"]}
    assert "图内文字（启发式）" in names
    assert names["图内文字（启发式）"]["blocking"] is False


def test_cli_json_output(fixtures):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_image.py"), str(fixtures["ok"]),
         "--platform", "淘系", "--json"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), timeout=120,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src"),
             "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True and payload["platform"] == "淘系"


def test_cli_reports_failure_exit_code(fixtures):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_image.py"), str(fixtures["not_square"])],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), timeout=120,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src"),
             "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 1
    assert "[FAIL]" in proc.stdout


def test_generator_does_not_use_pillow():
    source = (ROOT / "scripts" / "make_image_fixtures.py").read_text(encoding="utf-8")
    assert "PIL" not in source
