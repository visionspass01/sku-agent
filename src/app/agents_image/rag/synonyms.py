"""查询改写（第五轮 P1-1）：生图业务同义词扩展。

生图知识库只有十几篇短文，用户/图位的说法与语料措辞经常对不上（"主图" vs "首图"、
"留白" vs "边距"）。这里做**确定性**的同义词扩展（零模型、零成本、可测），
在 BM25/向量之前把查询补齐，提升中文短查询的召回。
"""

from __future__ import annotations

# 左侧是语料/需求里的常见说法，右侧是需要补进来的别名
QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "主图": ("首图", "封面图"),
    "白底": ("纯白", "白背景"),
    "场景": ("环境", "场景代入"),
    "留白": ("边距", "margin"),
    "占比": ("画面占比", "填充比"),
    "细节": ("特写", "微距"),
    "比例": ("画幅", "长宽比"),
    "变体": ("差异化", "A/B"),
    "文案": ("图内文字", "主标语"),
    "身份": ("颜色", "版型", "logo"),
}


def rewrite_query(query: str, synonyms: dict[str, tuple[str, ...]] | None = None) -> str:
    """把查询里出现的关键词补上同义词；已经出现过的词不重复追加。"""
    text = str(query or "").strip()
    if not text:
        return ""
    table = synonyms if synonyms is not None else QUERY_SYNONYMS
    additions: list[str] = []
    for word, aliases in table.items():
        if word not in text:
            continue
        for alias in aliases:
            if alias and alias not in text and alias not in additions:
                additions.append(alias)
    if not additions:
        return text
    return f"{text} {' '.join(additions)}"


__all__ = ["QUERY_SYNONYMS", "rewrite_query"]
