"""`docs/ABOUT.md` 目录（TOC）锚点的回归测试。

同一份文档有两个渲染面，锚点必须都能跳：
- GitHub 直接看 `docs/ABOUT.md` → 按 GitHub 的 slug 规则生成锚点；
- 看板 `/about.html` → `web/markdown-lite.js` 渲染，标题 id 由同一套规则生成。

这里把「目录里的链接」与「标题算出来的 slug」对齐检查一遍，再用 node 真跑一次
渲染器，确认它给每个标题加的 id 与目录一致（node 缺失时跳过）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "ABOUT.md"
LITE = ROOT / "web" / "markdown-lite.js"
ABOUT_HTML = ROOT / "web" / "about.html"


def _slug(text: str) -> str:
    """与 markdown-lite.js 的 slugify 完全一致（也是 GitHub 的规则）。"""
    buf = []
    for ch in text.strip().lower():
        cat = unicodedata.category(ch)
        if ch in "-_" or ch == " " or ch.isalnum() or cat.startswith("M"):
            buf.append(ch)
    return ("".join(buf).replace(" ", "-")) or "section"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _headings(text: str) -> list[tuple[int, str]]:
    out = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,6}) (.+)$", line)
        if m:
            out.append((len(m.group(1)), m.group(2).strip()))
    return out


def _toc_anchors(text: str) -> list[str]:
    """目录块（H1 与 `## 项目结构` 之间的 blockquote）里所有 `](#xxx)` 链接的锚点。"""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## 项目结构"))
    toc = [line for line in lines[:start] if line.startswith(">")]
    anchors: list[str] = []
    for line in toc:
        anchors.extend(re.findall(r"\]\(#([^)\s]+)\)", line))
    return anchors


def test_toc_sits_between_title_and_first_section():
    lines = _text().splitlines()
    assert lines[0].startswith("# "), "第一行必须是文档标题"
    toc_idx = [i for i, line in enumerate(lines) if line.startswith("> **目录**")]
    assert len(toc_idx) == 1, "目录只能有一份"
    first_section = next(i for i, line in enumerate(lines) if line.startswith("## 项目结构"))
    assert lines[0].startswith("# ") and toc_idx[0] > 0 and toc_idx[0] < first_section


def test_toc_covers_both_first_level_sections():
    text = _text()
    heads = _headings(text)
    assert [t for lvl, t in heads if lvl == 2] == ["项目结构", "技术设计"]
    anchors = _toc_anchors(text)
    for name in ("项目结构", "技术设计"):
        assert _slug(name) in anchors, f"目录缺少一级章节：{name}"
    # 每个二级章节（###）都要在目录里有入口
    for _lvl, title in [h for h in heads if h[0] == 3]:
        assert _slug(title) in anchors, f"目录缺少二级章节：{title}"


def test_toc_anchors_match_heading_slugs():
    text = _text()
    known = {_slug(t) for _lvl, t in _headings(text)}
    anchors = _toc_anchors(text)
    assert anchors, "目录里没有任何链接"
    missing = [a for a in anchors if a not in known]
    assert not missing, f"这些目录链接没有对应标题：{missing}"


def test_toc_dead_lines_removed():
    """原 1.9 已并入 1.5.3，编号顺延后不应残留旧编号。"""
    text = _text()
    assert "1.5.7" not in text and "1.5.8" not in text
    assert "1.9 用户偏好" not in text


def test_design_section_uses_2x_numbering():
    """「项目结构」用 1.x，「技术设计」用 2.x 依次排（2.1、2.2 …），两块不能串号。"""
    text = _text()
    lines = text.splitlines()
    i_structure = lines.index("## 项目结构")
    i_design = lines.index("## 技术设计")

    structure_nums = [line.split()[1] for line in lines[i_structure:i_design]
                      if line.startswith("### ")]
    design_nums = [line.split()[1] for line in lines[i_design:]
                   if line.startswith("### ")]
    assert structure_nums == ["1.1", "1.2", "1.3", "1.4"], structure_nums
    assert design_nums == [f"2.{i}" for i in range(1, len(design_nums) + 1)], design_nums
    # 子小节跟随父级编号（2.1.x / 2.4.x / 2.5.x）
    for line in lines[i_design:]:
        if line.startswith("#### "):
            assert line.split()[1].startswith("2."), line
    # 交叉引用不能指着旧编号
    assert "见 1.5" not in text and "见 1.8" not in text


def test_cross_references_point_to_existing_sections():
    """正文里所有「见 2.N」都必须能找到对应的 `### 2.N` 标题。"""
    text = _text()
    numbers = {line.split()[1] for line in text.splitlines() if line.startswith("### 2.")}
    refs = set(re.findall(r"见 (2\.\d+)", text))
    assert refs, "文档里应当有指向技术设计小节的交叉引用"
    missing = sorted(r for r in refs if r not in numbers)
    assert not missing, f"这些交叉引用指向了不存在的小节：{missing}"


def test_section_1_1_describes_actual_demo_stack():
    """1.1 的演示形态必须写实际部署栈；不能再只写「本地 SQLite + fakeredis 零依赖」。"""
    text = _text()
    start = text.index("### 1.1 项目定位")
    end = text.index("### 1.2 技术栈速览")
    block = text[start:end]
    for keyword in ("mysql", "qdrant", "fakeredis", "nginx"):
        assert keyword in block.lower(), f"1.1 缺少实际部署组件：{keyword}"
    assert "本地 SQLite + fakeredis 零依赖运行" not in block, "旧的演示形态描述应已被替换"
    # 演示数据与降级状态要如实写出来（不写估算值）
    assert "70,212" in block and "kb_chunks_512" in block
    assert "降级" in block


def test_markdown_lite_renders_heading_ids_and_inpage_links():
    """真跑一遍前端渲染器：标题要带 id，目录里的 `#xxx` 不能变成新标签页链接。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过着色/渲染器检查")

    script = r"""
const fs = require("fs");
const vm = require("vm");
const lite = fs.readFileSync(process.argv[2], "utf8");
const md = fs.readFileSync(process.argv[3], "utf8");
const sandbox = { console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(lite, sandbox);
const html = sandbox.window.renderMarkdown(md);
const ids = Array.from(html.matchAll(/<h[1-6] id="([^"]+)"/g)).map((m) => m[1]);
const inpage = sandbox.window.renderMarkdown("[跳到项目结构](#项目结构)").trim();
const external = sandbox.window.renderMarkdown("[看文档](DEPLOY.md)").trim();
console.log(JSON.stringify({ ids, inpage, external }));
"""
    # 注意：Windows 下 node 常是 .cmd 包装，`-e` 传多行脚本会被 cmd 拆坏，改成落临时文件再跑
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "render_check.js"
        js.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [node, str(js), str(LITE), str(DOC)],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
    assert proc.returncode == 0, proc.stderr[-1500:]
    data = json.loads(proc.stdout.strip().splitlines()[-1])

    text = _text()
    rendered = data["ids"]
    assert len(rendered) == len(set(rendered)), "标题 id 必须唯一（重复标题要加 -1 后缀）"
    expected = [_slug(t) for lvl, t in _headings(text) if lvl in (2, 3)]
    missing = [i for i in expected if i not in rendered]
    assert not missing, f"渲染器没有给这些标题加 id（目录会跳空）：{missing}"
    order = [rendered.index(i) for i in expected]
    assert order == sorted(order), "标题 id 的出现顺序应与文档顺序一致"

    assert 'href="#项目结构"' in data["inpage"]
    assert "target=" not in data["inpage"], "文档内锚点不应该开新标签页"
    assert 'target="_blank"' in data["external"], "外部链接仍应新标签页打开"


def test_about_page_has_scroll_target_styles():
    html = ABOUT_HTML.read_text(encoding="utf-8")
    assert "scroll-margin-top" in html, "锚点跳转需要避开顶部裁切"
    assert "scroll-behavior: smooth" in html
