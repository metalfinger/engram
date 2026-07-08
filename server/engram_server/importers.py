"""Chat-export importers — parse a ChatGPT or Claude data export into PROPOSED concepts.

These are PURE functions: they take the raw exported JSON text and return a list of
proposal dicts. They never write anything — the caller (``kbstore.kb_import``) reviews
the proposals and, only on an explicit non-dry-run, files them into ``inbox/imports/``
for later triage. That keeps a large history dump reviewable before it lands in the brain.

Each conversation becomes one proposal::

    {
        "suggested_path": "inbox/imports/YYYY-MM-<slug>.md",
        "title": <conversation title>,
        "type": "imported-conversation",
        "timestamp": "YYYY-MM-DDTHH:MM:SSZ" | None,
        "body": <rendered role-prefixed transcript>,
        "source": "chatgpt" | "claude",
        "message_count": <visible turns>,
        "truncated": <bool — body was capped>,
    }

Robust by design: missing fields, weird unicode, and empty conversations are tolerated
(empty conversations are skipped, never proposed). Bodies are capped with a trailing
note so one runaway conversation cannot dump megabytes into a single concept.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .errors import KBError

log = logging.getLogger("engram.importers")

# A single proposed body is capped so a huge chat cannot balloon one concept; the
# caller is told (truncated=True) and the transcript ends with an explicit note.
_BODY_CAP = 16000
_SLUG_MAX = 60


# ------------------------------------------------------------------ shared helpers


def _load_json(json_text: str) -> Any:
    if not json_text or not json_text.strip():
        raise KBError("Empty export payload — paste the exported conversations JSON.")
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise KBError(
            f"Export payload is not valid JSON ({exc.msg} at line {exc.lineno}). Paste the "
            "raw contents of the export file (ChatGPT conversations.json / Claude "
            "conversations.json), not a screenshot or a summary."
        ) from exc


def _as_conversation_list(data: Any) -> list[dict[str, Any]]:
    """Both exports are a top-level list of conversations, but some wrap it in a dict
    ({"conversations": [...]}). Accept either; anything else is a shape error."""
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    if isinstance(data, dict):
        for key in ("conversations", "chats", "data"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [c for c in inner if isinstance(c, dict)]
    raise KBError(
        "Export payload is not a list of conversations — expected the top-level array "
        "from conversations.json (or a {\"conversations\": [...]} wrapper)."
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:_SLUG_MAX].strip("-")


def _epoch_to_iso(value: Any) -> str | None:
    """Epoch seconds (ChatGPT create_time) -> canonical UTC ISO string, or None."""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _isostr_to_iso(value: Any) -> str | None:
    """An ISO-8601 string (Claude created_at, possibly Z-suffixed) -> canonical UTC ISO."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_prefix(timestamp: str | None) -> str:
    """YYYY-MM slug prefix from a canonical timestamp; 'undated' when unknown."""
    if timestamp and len(timestamp) >= 7:
        return timestamp[:7]
    return "undated"


def _render_transcript(title: str, turns: list[tuple[str, str]]) -> tuple[str, bool]:
    """Render (role, text) turns into a role-prefixed markdown body. Returns (body,
    truncated). Each turn is ``**Role**`` followed by its text; the whole body is capped."""
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    for role, text in turns:
        clean = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not clean:
            continue
        parts.append(f"**{role}**\n\n{clean}")
    body = "\n\n".join(parts).strip()
    if len(body) <= _BODY_CAP:
        return (body + "\n" if body else ""), False
    cut = body.rfind("\n\n", 0, _BODY_CAP)
    if cut <= 0:
        cut = _BODY_CAP
    trimmed = body[:cut].rstrip()
    note = (
        f"\n\n_[transcript truncated — {cut} of {len(body)} characters shown; "
        "import the full export elsewhere if the tail matters]_\n"
    )
    return trimmed + note, True


def _proposal(
    title: str, timestamp: str | None, body: str, truncated: bool, message_count: int, source: str
) -> dict[str, Any]:
    slug = _slug(title) or "conversation"
    return {
        "suggested_path": f"inbox/imports/{_month_prefix(timestamp)}-{slug}.md",
        "title": title,
        "type": "imported-conversation",
        "timestamp": timestamp,
        "body": body,
        "source": source,
        "message_count": message_count,
        "truncated": truncated,
    }


def _dedupe_paths(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two conversations in the same month with the same slug would collide on one path.
    Disambiguate deterministically (-2, -3, ...) so every proposal owns a distinct path."""
    seen: dict[str, int] = {}
    for p in proposals:
        base = p["suggested_path"]
        if base not in seen:
            seen[base] = 1
            continue
        seen[base] += 1
        stem, _dot, ext = base.rpartition(".")
        p["suggested_path"] = f"{stem}-{seen[base]}.{ext}"
    return proposals


# ------------------------------------------------------------------ ChatGPT


def _chatgpt_message_text(message: dict[str, Any]) -> str:
    """Extract clean text from one ChatGPT message node's content payload."""
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    ctype = content.get("content_type")
    if ctype == "text":
        parts = content.get("parts") or []
        return "\n".join(str(p) for p in parts if isinstance(p, str) and p)
    if ctype == "code":
        lang = content.get("language") or ""
        return f"```{lang}\n{content.get('text', '')}\n```"
    if ctype == "multimodal_text":
        parts = content.get("parts") or []
        texts = [str(p) for p in parts if isinstance(p, str) and p.strip()]
        return "\n".join(texts)
    # Unknown/other content types (images, tether, etc.): salvage any string parts.
    parts = content.get("parts")
    if isinstance(parts, list):
        return "\n".join(str(p) for p in parts if isinstance(p, str) and p.strip())
    return ""


def _chatgpt_ordered_messages(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the ChatGPT mapping tree from its root, returning message nodes in
    conversation order. Iterative (deep chats won't blow the stack); tolerant of
    malformed nodes and missing roots."""
    if not isinstance(mapping, dict) or not mapping:
        return []
    root_id = None
    for node_id, node in mapping.items():
        if isinstance(node, dict) and node.get("parent") is None:
            root_id = node_id
            break
    if root_id is None:  # no explicit root — fall back to insertion order
        root_id = next(iter(mapping))
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack = [root_id]
    while stack:
        node_id = stack.pop()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if isinstance(message, dict):
            ordered.append(message)
        children = node.get("children") or []
        for child_id in reversed(children):
            stack.append(child_id)
    return ordered


def _chatgpt_conversation(conv: dict[str, Any]) -> dict[str, Any] | None:
    title = str(conv.get("title") or "").strip() or "Untitled conversation"
    mapping = conv.get("mapping")
    messages = _chatgpt_ordered_messages(mapping if isinstance(mapping, dict) else {})
    turns: list[tuple[str, str]] = []
    for message in messages:
        meta = message.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("is_visually_hidden_from_conversation"):
            continue
        author = message.get("author") or {}
        role = str(author.get("role") or "").strip() if isinstance(author, dict) else ""
        text = _chatgpt_message_text(message)
        if not text.strip():
            continue
        if role == "system":  # system preamble carries no conversational value
            continue
        turns.append((role.title() or "Message", text))
    if not turns:
        return None
    timestamp = _epoch_to_iso(conv.get("create_time"))
    if timestamp is None:
        for message in messages:
            timestamp = _epoch_to_iso(message.get("create_time"))
            if timestamp:
                break
    body, truncated = _render_transcript(title, turns)
    return _proposal(title, timestamp, body, truncated, len(turns), "chatgpt")


def parse_chatgpt_export(json_text: str) -> list[dict[str, Any]]:
    """Parse a ChatGPT ``conversations.json`` export into proposed concepts.

    The export is a list of conversations, each a ``mapping`` of message-tree nodes.
    Empty conversations (no visible turns) are skipped. Never writes anything."""
    conversations = _as_conversation_list(_load_json(json_text))
    proposals: list[dict[str, Any]] = []
    for conv in conversations:
        try:
            proposal = _chatgpt_conversation(conv)
        except Exception:  # noqa: BLE001 — one malformed conversation must not sink the batch
            log.warning("importers: skipped a malformed ChatGPT conversation", exc_info=True)
            continue
        if proposal is not None:
            proposals.append(proposal)
    return _dedupe_paths(proposals)


# ------------------------------------------------------------------ Claude


def _claude_message_text(message: dict[str, Any]) -> str:
    """Extract text from one Claude chat_message — prefer the structured ``content``
    blocks, fall back to the flat ``text`` field."""
    content = message.get("content")
    if isinstance(content, list):
        texts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("text")
        ]
        joined = "\n".join(t for t in texts if t.strip())
        if joined.strip():
            return joined
    return str(message.get("text") or "")


def _claude_conversation(conv: dict[str, Any]) -> dict[str, Any] | None:
    title = str(conv.get("name") or "").strip()
    if not title:
        title = f"Conversation {conv.get('uuid', 'untitled')}"
    messages = conv.get("chat_messages")
    if not isinstance(messages, list):
        return None
    turns: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = str(message.get("sender") or "").strip()
        text = _claude_message_text(message)
        attachments = message.get("attachments") or []
        extra: list[str] = []
        if isinstance(attachments, list):
            for att in attachments:
                if isinstance(att, dict) and att.get("file_name"):
                    extra.append(f"_[attachment: {att['file_name']}]_")
        combined = "\n\n".join(p for p in [text, "\n".join(extra)] if p.strip())
        if not combined.strip():
            continue
        role = {"human": "User", "assistant": "Assistant"}.get(sender, sender.title() or "Message")
        turns.append((role, combined))
    if not turns:
        return None
    timestamp = _isostr_to_iso(conv.get("created_at"))
    body, truncated = _render_transcript(title, turns)
    return _proposal(title, timestamp, body, truncated, len(turns), "claude")


def parse_claude_export(json_text: str) -> list[dict[str, Any]]:
    """Parse a Claude ``conversations.json`` export into proposed concepts.

    The export is a list of conversations, each with a flat ``chat_messages`` list.
    Empty conversations are skipped. Never writes anything."""
    conversations = _as_conversation_list(_load_json(json_text))
    proposals: list[dict[str, Any]] = []
    for conv in conversations:
        try:
            proposal = _claude_conversation(conv)
        except Exception:  # noqa: BLE001 — tolerate one malformed conversation
            log.warning("importers: skipped a malformed Claude conversation", exc_info=True)
            continue
        if proposal is not None:
            proposals.append(proposal)
    return _dedupe_paths(proposals)


# ------------------------------------------------------------------ dispatch

_PARSERS = {"chatgpt": parse_chatgpt_export, "claude": parse_claude_export}


def parse_export(source: str, json_text: str) -> list[dict[str, Any]]:
    """Dispatch to the parser for ``source`` ('chatgpt' | 'claude')."""
    parser = _PARSERS.get((source or "").strip().lower())
    if parser is None:
        raise KBError(
            f"Unknown import source {source!r} — supported sources are 'chatgpt' and 'claude'."
        )
    return parser(json_text)
