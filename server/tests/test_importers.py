"""Chat-export importer tests — pure parsing + the kb_import dry-run/write contract.

Fixtures mimic the REAL export shapes: ChatGPT's mapping tree of message nodes and
Claude's flat chat_messages list. No network, no model.
"""

from __future__ import annotations

import json

import pytest

from engram_server.config import Settings
from engram_server.importers import parse_chatgpt_export, parse_claude_export
from engram_server.kbstore import KBStore


# ------------------------------------------------------------------ fixtures


def _chatgpt_export() -> str:
    return json.dumps(
        [
            {
                "title": "Vector DB choice",
                "create_time": 1719792000.0,  # 2024-07-01 UTC
                "update_time": 1719795600.0,
                "mapping": {
                    "root": {"id": "root", "parent": None, "children": ["a"], "message": None},
                    "a": {
                        "id": "a",
                        "parent": "root",
                        "children": ["b"],
                        "message": {
                            "id": "a",
                            "author": {"role": "user"},
                            "create_time": 1719792000.0,
                            "content": {"content_type": "text", "parts": ["Which vector db should I use?"]},
                            "metadata": {},
                        },
                    },
                    "b": {
                        "id": "b",
                        "parent": "a",
                        "children": ["c"],
                        "message": {
                            "id": "b",
                            "author": {"role": "assistant"},
                            "create_time": 1719792100.0,
                            "content": {"content_type": "text", "parts": ["Qdrant is a solid managed pick."]},
                            "metadata": {},
                        },
                    },
                    # A hidden system node — must be dropped from the transcript.
                    "c": {
                        "id": "c",
                        "parent": "b",
                        "children": [],
                        "message": {
                            "id": "c",
                            "author": {"role": "system"},
                            "content": {"content_type": "text", "parts": ["hidden preamble"]},
                            "metadata": {"is_visually_hidden_from_conversation": True},
                        },
                    },
                },
            },
            {
                # Empty conversation (no message nodes) — must be skipped entirely.
                "title": "Empty one",
                "create_time": 1719792000.0,
                "mapping": {"r": {"id": "r", "parent": None, "children": [], "message": None}},
            },
        ]
    )


def _claude_export() -> str:
    return json.dumps(
        [
            {
                "name": "Design chat",
                "created_at": "2026-06-15T10:00:00Z",
                "updated_at": "2026-06-15T11:00:00Z",
                "chat_messages": [
                    {
                        "sender": "human",
                        "created_at": "2026-06-15T10:00:00Z",
                        "text": "How should we structure the brain?",
                        "content": [{"type": "text", "text": "How should we structure the brain?"}],
                        "attachments": [],
                    },
                    {
                        "sender": "assistant",
                        "created_at": "2026-06-15T10:01:00Z",
                        "text": "",
                        "content": [{"type": "text", "text": "Rules over schema — few anchors."}],
                        "attachments": [{"file_name": "notes.txt"}],
                    },
                ],
            },
            {
                # Blank conversation — skipped.
                "name": "Blank",
                "created_at": "2026-06-15T10:00:00Z",
                "chat_messages": [],
            },
        ]
    )


# ------------------------------------------------------------------ parsing


def test_parse_chatgpt_renders_roles_and_skips_empty() -> None:
    proposals = parse_chatgpt_export(_chatgpt_export())
    assert len(proposals) == 1  # empty conversation skipped
    p = proposals[0]
    assert p["source"] == "chatgpt"
    assert p["type"] == "imported-conversation"
    assert p["title"] == "Vector DB choice"
    assert p["message_count"] == 2  # hidden system node excluded
    assert "**User**" in p["body"] and "**Assistant**" in p["body"]
    assert "Which vector db" in p["body"] and "Qdrant" in p["body"]
    assert "hidden preamble" not in p["body"]
    assert p["timestamp"] and p["timestamp"].startswith("2024-07-")
    assert p["suggested_path"] == f"inbox/imports/{p['timestamp'][:7]}-vector-db-choice.md"
    assert p["truncated"] is False


def test_parse_claude_renders_roles_and_attachments() -> None:
    proposals = parse_claude_export(_claude_export())
    assert len(proposals) == 1  # blank skipped
    p = proposals[0]
    assert p["source"] == "claude"
    assert p["title"] == "Design chat"
    assert p["message_count"] == 2
    assert "**User**" in p["body"] and "**Assistant**" in p["body"]
    assert "structure the brain" in p["body"] and "Rules over schema" in p["body"]
    assert "notes.txt" in p["body"]  # attachment noted
    assert p["timestamp"] == "2026-06-15T10:00:00Z"
    assert p["suggested_path"] == "inbox/imports/2026-06-design-chat.md"


def test_parse_truncates_huge_conversation_with_note() -> None:
    big = "x " * 20000  # ~40k chars, well over the body cap
    export = json.dumps(
        [
            {
                "title": "Huge",
                "create_time": 1719792000.0,
                "mapping": {
                    "root": {"id": "root", "parent": None, "children": ["a"], "message": None},
                    "a": {
                        "id": "a",
                        "parent": "root",
                        "children": [],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": [big]},
                            "metadata": {},
                        },
                    },
                },
            }
        ]
    )
    p = parse_chatgpt_export(export)[0]
    assert p["truncated"] is True
    assert "truncated" in p["body"]
    assert len(p["body"]) < len(big)


def test_parse_dedupes_colliding_paths() -> None:
    # Two conversations, same month + same title -> same base slug; paths must differ.
    export = json.dumps(
        [
            {
                "title": "Same Title",
                "create_time": 1719792000.0,
                "mapping": {
                    "root": {"id": "root", "parent": None, "children": ["a"], "message": None},
                    "a": {"id": "a", "parent": "root", "children": [], "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["one"]}, "metadata": {}}},
                },
            },
            {
                "title": "Same Title",
                "create_time": 1719792000.0,
                "mapping": {
                    "root": {"id": "root", "parent": None, "children": ["a"], "message": None},
                    "a": {"id": "a", "parent": "root", "children": [], "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["two"]}, "metadata": {}}},
                },
            },
        ]
    )
    paths = [p["suggested_path"] for p in parse_chatgpt_export(export)]
    assert len(paths) == len(set(paths)) == 2


def test_parse_bad_json_raises_teaching_error() -> None:
    from engram_server.errors import KBError

    with pytest.raises(KBError):
        parse_chatgpt_export("not json at all {")


# ------------------------------------------------------------------ kb_import


@pytest.fixture
async def store(settings: Settings) -> KBStore:
    s = KBStore(settings)
    await s.start()
    return s


async def test_kb_import_dry_run_writes_nothing(store: KBStore) -> None:
    result = await store.kb_import("claude", _claude_export(), dry_run=True)
    assert result["source"] == "claude"
    assert len(result["proposed"]) == 1
    assert result["imported"] == [] and result["skipped"] == []
    prop = result["proposed"][0]
    assert set(prop) == {"path", "title", "timestamp", "message_count", "truncated"}
    # Nothing landed on disk.
    assert not (store.root / prop["path"]).exists()


async def test_kb_import_writes_and_skips_existing(store: KBStore) -> None:
    first = await store.kb_import("claude", _claude_export(), dry_run=False)
    assert first["imported"] == ["inbox/imports/2026-06-design-chat.md"]
    assert first["skipped"] == []
    landed = store.root / "inbox/imports/2026-06-design-chat.md"
    assert landed.is_file()
    from engram_server.frontmatter import read_meta

    meta = read_meta(landed)
    assert meta["type"] == "imported-conversation"
    assert meta["source"] == "claude"
    assert meta["status"] == "untriaged"
    # inbox/imports/ index chain was created.
    assert (store.root / "inbox/imports/index.md").is_file()

    # Re-import the same export: idempotent-ish, the existing path is skipped.
    second = await store.kb_import("claude", _claude_export(), dry_run=False)
    assert second["imported"] == []
    assert second["skipped"] == ["inbox/imports/2026-06-design-chat.md"]


async def test_kb_import_rejects_unknown_source(store: KBStore) -> None:
    from engram_server.errors import KBError

    with pytest.raises(KBError):
        await store.kb_import("slack", "[]", dry_run=True)
