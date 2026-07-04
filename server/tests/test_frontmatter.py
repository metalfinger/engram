"""Pure tests for engram_server.frontmatter — tmp_path only, no git, no fixtures."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from engram_server.errors import FrontmatterError
from engram_server.frontmatter import Doc, normalize_meta, read_meta, serialize, split, validate_concept

NOW = datetime(2026, 7, 4, 12, 30, 0, tzinfo=timezone.utc)

COMPLETE = (
    "---\n"
    "type: decision\n"
    "title: Deploy Key Rotation\n"
    "description: Why we rotate the deploy key quarterly.\n"
    "timestamp: 2026-07-04T10:00:00Z\n"
    "---\n"
    "Rotate quarterly. See [runbook](../library/runbooks/rotate.md).\n"
)


# --- split ---------------------------------------------------------------


def test_split_none_without_fence():
    assert split("# About\n\nNo frontmatter here.\n") is None
    assert split("") is None


def test_split_returns_meta_and_body():
    doc = split(COMPLETE)
    assert doc is not None
    assert doc.meta["type"] == "decision"
    assert doc.body.startswith("Rotate quarterly.")


def test_split_invalid_yaml_raises():
    with pytest.raises(FrontmatterError):
        split("---\ntype: [unclosed\n---\nbody\n")


def test_split_non_mapping_raises():
    with pytest.raises(FrontmatterError):
        split("---\n- just\n- a\n- list\n---\nbody\n")


def test_split_missing_closing_fence_raises():
    with pytest.raises(FrontmatterError):
        split("---\ntype: idea\nno closing fence\n")


# --- normalize_meta / serialize ------------------------------------------


def test_normalize_meta_converts_datetimes_and_dates():
    meta = {
        "timestamp": datetime(2026, 7, 4, 21, 0, 0, tzinfo=timezone.utc),
        "expires": date(2026, 7, 10),
        "nested": {"when": [date(2026, 1, 1)]},
        "count": 3,
    }
    out = normalize_meta(meta)
    assert out["timestamp"] == "2026-07-04T21:00:00Z"
    assert out["expires"] == "2026-07-10"
    assert out["nested"] == {"when": ["2026-01-01"]}
    assert out["count"] == 3


def test_serialize_round_trips():
    doc = Doc(meta={"type": "idea", "title": "X", "description": "d"}, body="Body.\n")
    text = serialize(doc)
    again = split(text)
    assert again is not None
    assert again.meta == doc.meta
    assert again.body == doc.body


# --- validate_concept ------------------------------------------------------


def test_missing_frontmatter_raises_with_teaching_message():
    with pytest.raises(FrontmatterError, match=r"---"):
        validate_concept("Just prose, no fence.\n", rel_path="projects/alt/notes.md")


def test_missing_type_names_taxonomy():
    text = "---\ntitle: T\ndescription: d\n---\nBody.\n"
    with pytest.raises(FrontmatterError, match="runbook"):
        validate_concept(text, rel_path="projects/alt/notes.md")


def test_empty_type_rejected():
    text = "---\ntype: ''\ntitle: T\ndescription: d\n---\nBody.\n"
    with pytest.raises(FrontmatterError, match="type"):
        validate_concept(text, rel_path="projects/alt/notes.md")


def test_title_from_filename_never_from_h1():
    text = "---\ntype: project\ndescription: Alt working state.\n---\n# About\n\nAlt Inc context.\n"
    out, meta, _ = validate_concept(text, rel_path="projects/alt/context.md", now=NOW)
    assert meta["title"] == "Context"  # NOT "About"
    assert "title: Context" in out


def test_title_kebab_case_to_title_case():
    text = "---\ntype: runbook\ndescription: d\n---\nBody.\n"
    _, meta, _ = validate_concept(text, rel_path="library/runbooks/deploy-key-rotation.md", now=NOW)
    assert meta["title"] == "Deploy Key Rotation"


def test_description_frontmatter_wins_over_arg():
    text = "---\ntype: idea\ntitle: T\ndescription: from frontmatter\n---\nBody.\n"
    _, meta, _ = validate_concept(text, rel_path="a/b.md", description_arg="from arg", now=NOW)
    assert meta["description"] == "from frontmatter"


def test_description_arg_fallback():
    text = "---\ntype: idea\ntitle: T\n---\nBody.\n"
    out, meta, _ = validate_concept(text, rel_path="a/b.md", description_arg="from arg", now=NOW)
    assert meta["description"] == "from arg"
    assert "description: from arg" in out


def test_description_missing_everywhere_raises():
    text = "---\ntype: idea\ntitle: T\n---\nBody.\n"
    with pytest.raises(FrontmatterError, match="description"):
        validate_concept(text, rel_path="a/b.md")


def test_timestamp_filled_from_now():
    text = "---\ntype: idea\ntitle: T\ndescription: d\n---\nBody.\n"
    out, meta, warnings = validate_concept(text, rel_path="a/b.md", now=NOW)
    assert meta["timestamp"] == "2026-07-04T12:30:00Z"
    assert "timestamp: '2026-07-04T12:30:00Z'" in out or "timestamp: 2026-07-04T12:30:00Z" in out
    assert warnings == []


def test_timestamp_unparseable_kept_verbatim_with_warning():
    text = "---\ntype: idea\ntitle: T\ndescription: d\ntimestamp: last tuesday\n---\nBody.\n"
    out, meta, warnings = validate_concept(text, rel_path="a/b.md", now=NOW)
    assert meta["timestamp"] == "last tuesday"
    assert any("timestamp" in w for w in warnings)
    assert out == text  # nothing changed semantically -> verbatim


def test_yaml_datetime_timestamp_normalized_in_meta_but_text_verbatim():
    _, meta, warnings = validate_concept(COMPLETE, rel_path="projects/alt/decisions/2026-07-rotate.md")
    assert meta["timestamp"] == "2026-07-04T10:00:00Z"
    assert isinstance(meta["timestamp"], str)
    assert warnings == []


def test_message_defaults_status_and_to():
    text = "---\ntype: message\ntitle: Ping\ndescription: d\n---\nDo the thing.\n"
    out, meta, _ = validate_concept(text, rel_path="projects/alt/messages/ping.md", now=NOW)
    assert meta["status"] == "unread"
    assert meta["to"] == "any"
    assert "status: unread" in out
    assert "to: any" in out


def test_message_explicit_fields_not_overridden():
    text = (
        "---\ntype: message\ntitle: Ping\ndescription: d\n"
        "timestamp: 2026-07-04T10:00:00Z\nstatus: read\nto: claude-code\n---\nBody.\n"
    )
    _, meta, _ = validate_concept(text, rel_path="projects/alt/messages/ping.md", now=NOW)
    assert meta["status"] == "read"
    assert meta["to"] == "claude-code"


def test_extra_keys_pass_through_untouched():
    text = (
        "---\ntype: decision\ntitle: T\ndescription: d\n"
        "timestamp: 2026-07-04T10:00:00Z\nconfidence: settled\ntags:\n- infra\n---\nBody.\n"
    )
    out, meta, _ = validate_concept(text, rel_path="a/b.md", now=NOW)
    assert meta["confidence"] == "settled"
    assert meta["tags"] == ["infra"]
    assert out == text  # complete doc: verbatim


def test_wikilink_warning():
    text = "---\ntype: idea\ntitle: T\ndescription: d\ntimestamp: 2026-07-04T10:00:00Z\n---\nSee [[Other Note]].\n"
    _, _, warnings = validate_concept(text, rel_path="a/b.md", now=NOW)
    assert any("wikilink" in w.lower() for w in warnings)


def test_crlf_normalized_to_lf():
    text = COMPLETE.replace("\n", "\r\n")
    out, _, _ = validate_concept(text, rel_path="projects/alt/decisions/2026-07-rotate.md", now=NOW)
    assert "\r" not in out
    assert out.endswith(".md).\n")
    assert not out.endswith("\n\n")


def test_trailing_newlines_collapsed_to_one():
    text = COMPLETE + "\n\n"
    out, _, _ = validate_concept(text, rel_path="a/rotate.md", now=NOW)
    assert out.endswith(".md).\n")
    assert not out.endswith("\n\n")


def test_missing_trailing_newline_added():
    text = COMPLETE.rstrip("\n")
    out, _, _ = validate_concept(text, rel_path="a/rotate.md", now=NOW)
    assert out.endswith(".md).\n")


def test_verbatim_preservation_when_already_complete():
    # Odd-but-valid author formatting (comment, quirky quoting) must survive untouched.
    text = (
        "---\n"
        "type: decision\n"
        'title: "Deploy  Key"   # double space is intentional\n'
        "description: d\n"
        "timestamp: 2026-07-04T10:00:00Z\n"
        "---\n"
        "Body with *emphasis*.\n"
    )
    out, _, warnings = validate_concept(text, rel_path="a/b.md", now=NOW)
    assert out == text
    assert warnings == []


# --- read_meta -------------------------------------------------------------


def test_read_meta_happy_path(tmp_path):
    p = tmp_path / "concept.md"
    p.write_text(COMPLETE, encoding="utf-8", newline="\n")
    meta = read_meta(p)
    assert meta["type"] == "decision"
    assert meta["timestamp"] == "2026-07-04T10:00:00Z"  # normalized to string


def test_read_meta_no_frontmatter(tmp_path):
    p = tmp_path / "index.md"
    p.write_text("# Index\n\n* [X](x.md) - thing\n", encoding="utf-8", newline="\n")
    assert read_meta(p) == {}


def test_read_meta_missing_file(tmp_path):
    assert read_meta(tmp_path / "nope.md") == {}


def test_read_meta_unclosed_fence(tmp_path):
    p = tmp_path / "broken.md"
    p.write_text("---\ntype: idea\nno close\n", encoding="utf-8", newline="\n")
    assert read_meta(p) == {}


def test_read_meta_invalid_yaml(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text("---\ntype: [unclosed\n---\nbody\n", encoding="utf-8", newline="\n")
    assert read_meta(p) == {}
