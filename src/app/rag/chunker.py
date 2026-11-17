"""语义分块（RAG 链路第 2 步）。

策略：按 Markdown 标题切段 → 段内按中文句读滑窗（320 字/60 重叠）→ 表格行单独成块。

两个关键设计（评审 3.3 / 3.4-6）：
1. **chunk_id 内容寻址**：`doc_id#sha1(段标题+正文)[:8]`。旧实现用 `doc#序号`，
   文档一改（少一节）所有编号都漂移，引用失效、向量库里还会残留旧块。
   换成内容哈希后：同样的内容永远同一个 id，增删改只影响变动的块。
2. **块正文带文档/小节前缀**：`《补货 SOP》> 审批门槛\n正文`。
   检索命中「≥200 件需审批」这种句子时，如果没有标题前缀，模型分不清是哪份 SOP 的门槛。
"""

from __future__ import annotations

import dataclasses
import hashlib
import re

from app.rag.loader import KbDocument

MAX_CHARS = 320
OVERLAP = 60

# 分块方案版本：内容寻址 id / 标题前缀 这类改动会让**同样的文档**产出不同的块。
# 索引层用它判断"要不要重建"——只看文档 sha256 的话，升级分块逻辑后老块会一直留着
# （chunk_id 还是旧格式），检索质量悄悄退化。改分块逻辑时必须 +1。
# v3：块元数据新增 authority / scope / key_facts（口径冲突治理）——正文与 id 不变，
# 但老索引里没有这些字段，必须整库重建一次才能参与冲突裁决。
# v4：key_facts 改为**按小节**下发（`section|key=value`），不相关的小节不再携带口径。
# v5：事实带触发词（`fact_triggers`）+ 文档指纹纳入 front matter（元数据变更也会重建）。
CHUNKER_VERSION = "5"


@dataclasses.dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    ord: int
    text: str
    meta: dict


def chunk_digest(doc_id: str, heading: str, text: str) -> str:
    """块内容指纹：与位置无关，只与「哪篇 + 哪节 + 什么内容」有关。"""
    raw = f"{doc_id}\x00{heading}\x00{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:8]


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；;.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _split_sections(body: str) -> list[str]:
    """按标题切段：标题行保留在段首，便于召回时保留上下文。"""
    lines = body.split("\n")
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if re.match(r"^#{1,6}\s", line) and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    merged = ["\n".join(sec).strip() for sec in sections if "\n".join(sec).strip()]
    return merged


def _is_table_line(text: str) -> bool:
    return all(
        (not ln.strip()) or ln.strip().startswith("|") or ln.strip().startswith("---")
        for ln in text.splitlines()
    ) and "|" in text


def chunk_document(doc: KbDocument, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[Chunk]:
    chunks: list[Chunk] = []
    ord_seq = 0
    for section in _split_sections(doc.body):
        section_title = section.split("\n", 1)[0]
        if _is_table_line(section):
            # 表格逐行成块，保留表头语义
            lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
            header = lines[0] if lines else ""
            for ln in lines[1:]:
                if set(ln) <= {"-", "|", " "}:
                    continue
                chunks.append(_make_chunk(doc, ord_seq, f"{header}\n{ln}", header,
                                          section_title=section_title))
                ord_seq += 1
            continue

        sentences = _split_sentences(section)
        if not sentences:
            continue
        heading = section.split("\n", 1)[0][:40]
        buf = ""
        for sent in sentences:
            if not buf:
                buf = sent
            elif len(buf) + len(sent) + 1 <= max_chars:
                buf += "\n" + sent
            else:
                chunks.append(_make_chunk(doc, ord_seq, buf, heading,
                                          section_title=section_title))
                ord_seq += 1
                tail = buf[-overlap:] if overlap > 0 else ""
                buf = (tail + "\n" + sent).strip() if tail else sent
        if buf:
            chunks.append(_make_chunk(doc, ord_seq, buf, heading,
                                      section_title=section_title))
            ord_seq += 1
    return chunks


def _make_chunk(doc: KbDocument, ord_seq: int, text: str, heading: str = "",
                section_title: str = "") -> Chunk:
    body = text.strip()
    clean_heading = re.sub(r"^#+\s*", "", heading or "").strip()
    # 首节的标题往往就是文档标题的变体（多几个字/少了括号），别重复成「《A》> A…」
    title = doc.title.strip()
    if clean_heading and title:
        shorter, longer = sorted((clean_heading, title), key=len)
        if len(shorter) >= 6 and (shorter == longer or shorter in longer):
            clean_heading = ""
    digest = chunk_digest(doc.doc_id, clean_heading, body)
    meta = doc.meta()
    meta["section"] = (clean_heading or body.split("\n", 1)[0][:40]).lstrip("# ").strip()
    # 事实按**小节**归属（复审 P2-2）：不相关的小节不带口径，冲突就不会在
    # "问流程"的问题上冒出来。匹配用原始小节标题（`section_title`），
    # 因为 `clean_heading` 可能已被上面的去重逻辑清空。
    meta["key_facts"] = doc.facts_for(section_title)
    prefix = f"《{doc.title}》" + (f" > {clean_heading}" if clean_heading else "")
    return Chunk(
        chunk_id=f"{doc.doc_id}#{digest}",
        doc_id=doc.doc_id,
        ord=ord_seq,
        # 正文带标题前缀（检索/引用可读；`meta["body"]` 保留原文用于去重与相似度）
        text=f"{prefix}\n{body}",
        meta={**meta, "body": body},
    )
