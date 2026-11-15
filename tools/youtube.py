"""YouTube video transcript and metadata extraction tool."""

from __future__ import annotations

import asyncio
import html
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx

from tools.base import ToolContext, tool
from tools.web import HEADERS


def extract_video_id(url_or_id: str) -> str | None:
    """Extract an 11-character YouTube video ID from a URL or raw ID."""
    cleaned = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", cleaned):
        return cleaned
    try:
        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").lower()
        if host in ("youtu.be", "www.youtu.be"):
            vid = parsed.path.lstrip("/").split("/")[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                return vid
        if "youtube.com" in host:
            if parsed.path == "/watch":
                qs = parse_qs(parsed.query)
                vid = (qs.get("v") or [""])[0]
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                    return vid
            for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
                if parsed.path.startswith(prefix):
                    vid = parsed.path[len(prefix) :].split("/")[0]
                    if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                        return vid
    except Exception:
        pass
    match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", cleaned)
    return match.group(1) if match else None


def _fmt_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    mins, secs = divmod(total, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _fetch_via_library(video_id: str) -> list[dict] | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            return YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-US", "en-GB"])
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        return [
            {"start": getattr(s, "start", 0.0), "text": getattr(s, "text", "")}
            for s in fetched
        ]
    except Exception:
        return None


async def _fetch_via_httpx(video_id: str) -> tuple[str, str, list[dict]]:
    """Fetch video title, description, and captions directly from YouTube watch page."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20.0) as client:
        resp = await client.get(url)
        page = resp.text

        title_match = re.search(r"<title>(.*?)</title>", page, re.S)
        title = html.unescape(title_match.group(1)).replace(" - YouTube", "").strip() if title_match else video_id

        desc = ""
        desc_match = re.search(r'"shortDescription":"((?:\\.|[^"\\])*)"', page)
        if desc_match:
            try:
                desc = json.loads(f'"{desc_match.group(1)}"')
            except Exception:
                desc = desc_match.group(1)

        segments: list[dict] = []
        tracks_match = re.search(r'"captionTracks":(\[.*?\])', page)
        if tracks_match:
            try:
                tracks = json.loads(tracks_match.group(1))
                chosen = next(
                    (t for t in tracks if str(t.get("languageCode", "")).startswith("en")),
                    tracks[0] if tracks else None,
                )
                if chosen and chosen.get("baseUrl"):
                    cap_resp = await client.get(chosen["baseUrl"] + "&fmt=json3")
                    if cap_resp.status_code == 200:
                        data = cap_resp.json()
                        for ev in data.get("events", []):
                            segs = ev.get("segs")
                            if not segs:
                                continue
                            text = "".join(s.get("utf8", "") for s in segs).strip()
                            if text:
                                start_sec = float(ev.get("tStartMs", 0)) / 1000.0
                                segments.append({"start": start_sec, "text": text})
            except Exception:
                pass

        return title, desc, segments


@tool(
    "youtube_transcript",
    "Fetch the title, description, and timestamped transcript of a YouTube video for summarization or Q&A.",
    {"url": {"type": "string", "description": "YouTube video URL or 11-char video ID."}},
    ["url"],
)
async def youtube_transcript(ctx: ToolContext, url: str) -> str:
    video_id = extract_video_id(url)
    if not video_id:
        return "Invalid YouTube URL or video ID."

    lib_segments = await asyncio.to_thread(_fetch_via_library, video_id)
    title, desc, web_segments = await _fetch_via_httpx(video_id)
    segments = lib_segments or web_segments

    lines = [
        f"TITLE: {title}",
        f"URL: https://www.youtube.com/watch?v={video_id}",
    ]
    if desc:
        lines.append(f"DESCRIPTION: {desc[:600]}")

    if segments:
        lines.append("\nTRANSCRIPT:")
        transcript_lines = []
        for seg in segments:
            ts = _fmt_timestamp(float(seg.get("start", 0.0)))
            clean_txt = re.sub(r"\s+", " ", str(seg.get("text", ""))).strip()
            if clean_txt:
                transcript_lines.append(f"[{ts}] {clean_txt}")
        joined = "\n".join(transcript_lines)
        lines.append(joined[:1800])
    else:
        lines.append(
            "\n(No closed captions available on this video; summarize using the title and description above.)"
        )

    return "\n".join(lines)
