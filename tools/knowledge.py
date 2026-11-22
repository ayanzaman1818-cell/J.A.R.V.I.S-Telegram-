"""Personal 'Second Brain' knowledge vault, semantic/hybrid RAG search, and 'Read Later' web article archiver."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

import config
from database.repository import db
from tools.base import ToolContext, tool
from tools.web import safe_get

log = logging.getLogger("jarvis.tools.knowledge")


async def _compute_embedding(text: str) -> list[float] | None:
    """Compute a semantic vector embedding via Gemini if available, or return None."""
    if not config.GEMINI_API_KEY:
        return None
    try:
        from brain.gemini import get_client

        client = get_client()
        if client is None:
            return None
        res = await asyncio.wait_for(
            client.aio.models.embed_content(
                model="text-embedding-004",
                contents=text[:6000],
            ),
            timeout=10.0,
        )
        embeddings = getattr(res, "embeddings", None)
        if embeddings and len(embeddings) > 0:
            vals = getattr(embeddings[0], "values", None)
            if vals:
                return [float(x) for x in vals]
    except Exception:
        log.debug("Embedding generation skipped/failed; using lexical fallback", exc_info=True)
    return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _tokenize(text: str) -> list[str]:
    return [tok for tok in re.findall(r"[a-z0-9]{2,}", text.lower())]


def _lexical_score(query: str, title: str, content: str, tags: str = "") -> float:
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0
    title_l = title.lower()
    content_l = content.lower()
    tags_l = tags.lower()
    score = 0.0
    if query.strip().lower() in title_l:
        score += 3.0
    elif query.strip().lower() in content_l:
        score += 1.5

    title_set = set(_tokenize(title_l))
    content_set = set(_tokenize(content_l))
    tag_set = set(_tokenize(tags_l))

    for qt in q_tokens:
        if qt in title_set:
            score += 2.0
        if qt in tag_set:
            score += 1.8
        if qt in content_set:
            score += 1.0
    return score / max(1, len(q_tokens))


def _scan_local_markdown_notes() -> list[dict[str, Any]]:
    """Read local .md and .txt files in KNOWLEDGE_LOCAL_DIR for unified Second Brain search."""
    folder: Path = config.KNOWLEDGE_LOCAL_DIR
    if not folder.exists() or not folder.is_dir():
        return []
    notes: list[dict[str, Any]] = []
    try:
        for p in sorted(folder.glob("**/*")):
            if p.is_file() and p.suffix.lower() in (".md", ".txt") and p.stat().st_size <= 500_000:
                try:
                    body = p.read_text(encoding="utf-8", errors="replace")
                    notes.append(
                        {
                            "id": f"file:{p.name}",
                            "title": p.stem,
                            "content": body[:4000],
                            "source_url": str(p),
                            "tags": "local-file",
                            "embedding": "",
                        }
                    )
                except Exception:
                    continue
    except Exception:
        pass
    return notes


@tool(
    "save_note",
    "Save a note, idea, research summary, or code snippet to the user's Second Brain knowledge vault.",
    {
        "title": {"type": "string", "description": "Short descriptive title."},
        "content": {"type": "string", "description": "Full text or markdown content to store."},
        "tags": {
            "type": "string",
            "description": "Optional comma-separated tags (e.g. 'project, python, ideas').",
        },
        "source_url": {"type": "string", "description": "Optional reference URL."},
    },
    ["title", "content"],
)
async def save_note(
    ctx: ToolContext,
    title: str,
    content: str,
    tags: str = "",
    source_url: str = "",
) -> str:
    if not content.strip():
        return "Cannot save an empty note."
    emb = await _compute_embedding(f"{title}\n{tags}\n{content}")
    note_id = db.add_knowledge_note(
        title=title,
        content=content,
        source_url=source_url,
        tags=tags,
        embedding=emb,
        user_id=ctx.user_id,
    )
    return f"Saved to Second Brain #{note_id}: '{title.strip()}'" + (
        f" [tags: {tags.strip()}]" if tags.strip() else ""
    )


@tool(
    "archive_article",
    "Fetch a web article URL ('Read Later'), extract its clean text, and archive a summary in the Second Brain vault.",
    {
        "url": {"type": "string", "description": "Web page URL to archive."},
        "tags": {"type": "string", "description": "Optional comma-separated tags."},
        "note": {"type": "string", "description": "Optional personal note to attach."},
    },
    ["url"],
)
async def archive_article(
    ctx: ToolContext, url: str, tags: str = "read-later", note: str = ""
) -> str:
    import trafilatura

    resp = await safe_get(url)
    raw_html = resp.text if hasattr(resp, "text") else str(resp)
    extracted = trafilatura.extract(
        raw_html, include_links=True, output_format="txt"
    ) or raw_html[:4000]

    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    page_title = (
        re.sub(r"\s+", " ", title_match.group(1)).strip()[:180]
        if title_match
        else url[:120]
    )

    clean_excerpt = extracted.strip()[:3500]
    full_body = f"{note.strip()}\n\n{clean_excerpt}".strip() if note.strip() else clean_excerpt
    emb = await _compute_embedding(f"{page_title}\n{tags}\n{full_body}")
    note_id = db.add_knowledge_note(
        title=page_title,
        content=full_body,
        source_url=url.strip(),
        tags=tags or "read-later",
        embedding=emb,
        user_id=ctx.user_id,
    )
    preview = clean_excerpt[:320].replace("\n", " ")
    return (
        f"Archived article #{note_id}: '{page_title}'\n"
        f"URL: {url}\n"
        f"Tags: {tags or 'read-later'}\n"
        f"Preview: {preview}..."
    )


@tool(
    "search_knowledge",
    "Search the user's Second Brain knowledge vault (saved notes, archived articles, and local markdown files) using semantic + keyword RAG.",
    {
        "query": {"type": "string", "description": "Question, topic, or keywords to search for."},
        "limit": {"type": "integer", "description": "Maximum results to return (default 5)."},
    },
    ["query"],
)
async def search_knowledge(
    ctx: ToolContext, query: str, limit: int = 5
) -> str:
    q = query.strip()
    if not q:
        return "Please provide a search query."

    db_notes = db.list_knowledge_notes(limit=200, user_id=ctx.user_id)
    local_notes = await asyncio.to_thread(_scan_local_markdown_notes)
    all_items = db_notes + local_notes
    if not all_items:
        return "Your Second Brain knowledge vault is currently empty. Use save_note or archive_article to add items."

    q_emb = await _compute_embedding(q)
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in all_items:
        lex = _lexical_score(
            q,
            str(item.get("title", "")),
            str(item.get("content", "")),
            str(item.get("tags", "")),
        )
        sem = 0.0
        if q_emb and item.get("embedding"):
            try:
                item_emb = json.loads(item["embedding"])
                sem = max(0.0, _cosine_similarity(q_emb, item_emb))
            except Exception:
                sem = 0.0
        combined = lex + (sem * 2.5)
        if combined > 0.15:
            scored.append((combined, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = [it for _, it in scored[: max(1, min(int(limit), 15))]]
    if not top:
        return f"No matching notes found in Second Brain for '{q}'."

    lines = [f"Second Brain search results for '{q}':"]
    for it in top:
        tags_str = f" [{it['tags']}]" if it.get("tags") else ""
        url_str = f"\n  Source: {it['source_url']}" if it.get("source_url") else ""
        snippet = str(it.get("content", "")).strip()[:600]
        lines.append(f"\n#{it['id']} {it['title']}{tags_str}{url_str}\n  {snippet}")
    return "\n".join(lines)


@tool(
    "list_notes",
    "List recent notes and archived articles in the user's Second Brain knowledge vault.",
    {
        "tag": {"type": "string", "description": "Optional tag filter."},
        "limit": {"type": "integer", "description": "Max items to list (default 15)."},
    },
)
async def list_notes(ctx: ToolContext, tag: str = "", limit: int = 15) -> str:
    notes = db.list_knowledge_notes(
        limit=max(1, min(int(limit), 50)), tag=tag, user_id=ctx.user_id
    )
    if not notes:
        return "No saved notes in your Second Brain vault."
    lines = ["Second Brain Notes:"]
    for n in notes:
        t_part = f" [{n['tags']}]" if n.get("tags") else ""
        u_part = f" ({n['source_url']})" if n.get("source_url") else ""
        date_s = str(n.get("created_at", ""))[:10]
        lines.append(f"#{n['id']} [{date_s}] {n['title']}{t_part}{u_part}")
    return "\n".join(lines)


@tool(
    "delete_note",
    "Delete a note or archived article from the Second Brain vault by its ID.",
    {"note_id": {"type": "integer"}},
    ["note_id"],
)
async def delete_note(ctx: ToolContext, note_id: int) -> str:
    if db.delete_knowledge_note(note_id, user_id=ctx.user_id):
        return f"Deleted Second Brain note #{note_id}."
    return f"Note #{note_id} not found."


def quick_knowledge_context(query: str, user_id: int | None = None, limit: int = 2) -> str:
    """Fast zero-network lexical recall of top matching Second Brain notes for automatic prompt injection."""
    q = (query or "").strip()
    if len(q) < 4:
        return ""
    notes = db.list_knowledge_notes(limit=80, user_id=user_id)
    if not notes:
        return ""
    scored: list[tuple[float, dict[str, Any]]] = []
    for n in notes:
        score = _lexical_score(
            q,
            str(n.get("title", "")),
            str(n.get("content", "")),
            str(n.get("tags", "")),
        )
        if score >= 0.45:
            scored.append((score, n))
    if not scored:
        return ""
    scored.sort(key=lambda pair: pair[0], reverse=True)
    lines = []
    for _, it in scored[:limit]:
        snippet = str(it.get("content", "")).strip().replace("\n", " ")[:360]
        lines.append(f"- [Note #{it['id']} {it['title']}]: {snippet}")
    return "\n".join(lines)
