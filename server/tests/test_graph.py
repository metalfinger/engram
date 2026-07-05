"""Interactive concept graph (/brain/graph[.json]) and live data-bound share pages.

The graph is built server-side from the bundle (every non-index .md is a node,
relative body links are edges); the graph page is a full-bleed vanilla-JS canvas.
Live-view concepts (type: live-view, view: status-wall) render a server-side,
link-free status wall on the public /share/{token} route and get a 'live' badge
in the artifacts gallery.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram_server.config import Settings
from engram_server.explorer import register as register_explorer
from engram_server.explorer.routes import _graph_data
from engram_server.kbstore import KBStore

SKELETON = Path(__file__).resolve().parents[2] / "brain-skeleton" / "brain"

LIVE_VIEW = (
    "---\ntype: live-view\nview: status-wall\ntitle: Client Status Wall\n"
    "description: An always-current status board for clients.\n"
    "share: live-view-token-0123456789\n---\n\n"
    "A live, data-bound wall. See [context](../context.md).\n"
)
ALT_CONTEXT = (
    "---\ntype: project\ntitle: Alt\ndescription: The alt project.\nstatus: active\n"
    "project: alt\n---\n# About\n\nAlt is a thing.\n\n"
    "# Current Phase\n\nShipping the v2 rollout to pilot clients.\n\n"
    "# Open Loops\n\n- [ ] TODO\n"
)


def _client(settings: Settings) -> TestClient:
    """A Starlette app carrying only the explorer's custom routes (guard included)."""
    mcp = FastMCP("test-graph")
    register_explorer(mcp, settings)
    app = Starlette(routes=mcp._custom_starlette_routes)
    return TestClient(app)


# ------------------------------------------------------------ graph data (pure)


def test_graph_data_excludes_index_and_shapes_nodes() -> None:
    data = _graph_data(SKELETON)
    ids = [n["id"] for n in data["nodes"]]
    assert ids, "skeleton must yield concept nodes"
    # index.md and .git are never nodes
    assert all(not i.endswith("index.md") for i in ids)
    assert all(".git" not in i.split("/") for i in ids)
    # a known concept is present, grouped by its top-level segment
    assert "self/bio.md" in ids
    bio = next(n for n in data["nodes"] if n["id"] == "self/bio.md")
    assert set(bio) >= {"id", "title", "type", "project", "links_out", "links_in"}
    assert bio["project"] == "self"
    # a project concept groups under the project name, not "projects"
    ctx = next(n for n in data["nodes"] if n["id"] == "projects/alt/context.md")
    assert ctx["project"] == "alt"
    assert isinstance(data["edges"], list)


def test_graph_data_edges_from_relative_links(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    dec = brain / "projects" / "alt" / "decisions"
    dec.mkdir(parents=True)
    (brain / "projects" / "alt" / "context.md").write_text(
        "---\ntype: project\ntitle: Alt\n---\n# X\n\nSee [a](decisions/a.md).\n",
        encoding="utf-8",
    )
    (dec / "index.md").write_text("# Decisions\n", encoding="utf-8")
    (dec / "a.md").write_text(
        "---\ntype: decision\ntitle: A\n---\n# A\n\nBack to [ctx](../context.md).\n",
        encoding="utf-8",
    )
    data = _graph_data(brain)
    ids = {n["id"] for n in data["nodes"]}
    assert ids == {"projects/alt/context.md", "projects/alt/decisions/a.md"}
    assert {"source": "projects/alt/context.md", "target": "projects/alt/decisions/a.md"} in data["edges"]
    assert {"source": "projects/alt/decisions/a.md", "target": "projects/alt/context.md"} in data["edges"]
    ctx = next(n for n in data["nodes"] if n["id"] == "projects/alt/context.md")
    assert ctx["links_out"] == 1 and ctx["links_in"] == 1
    a = next(n for n in data["nodes"] if n["id"] == "projects/alt/decisions/a.md")
    assert a["project"] == "alt"
    # links to index.md targets never become edges (index.md is not a node)
    assert all("index.md" not in e["target"] for e in data["edges"])


# ------------------------------------------------------------ graph routes


async def _clone_checkout(settings: Settings) -> None:
    """Materialize the brain checkout on disk (KBStore.start clones the skeleton)."""
    await KBStore(settings).start()


async def test_graph_page_renders_canvas_and_is_guarded(settings: Settings) -> None:
    await _clone_checkout(settings)
    open_settings = settings.model_copy(update={"dev_no_access": True})
    resp = _client(open_settings).get("/brain/graph")
    assert resp.status_code == 200
    html = resp.text
    assert "<canvas" in html  # the graph canvas marker
    assert "graphwrap" in html
    assert 'id="glegend"' in html  # type legend
    assert "requestAnimationFrame" in html  # the force-directed loop is inline
    # guarded: no Access JWT -> 403
    assert _client(settings).get("/brain/graph").status_code == 403


async def test_graph_json_shape_and_guarded(settings: Settings) -> None:
    await _clone_checkout(settings)
    open_settings = settings.model_copy(update={"dev_no_access": True})
    resp = _client(open_settings).get("/brain/graph.json")
    assert resp.status_code == 200
    payload = resp.json()
    assert "nodes" in payload and "edges" in payload
    assert payload["nodes"]
    assert all(not n["id"].endswith("index.md") for n in payload["nodes"])
    assert _client(settings).get("/brain/graph.json").status_code == 403


# ------------------------------------------------------------ live-view share wall


async def _seed_live_view(settings: Settings) -> str:
    store = KBStore(settings)
    await store.start()
    # give a real project a distinctive Current Phase line the wall can surface
    await store.kb_write("projects/alt/context.md", ALT_CONTEXT, "set alt phase")
    # the shareable live-view concept — its share token lives in the frontmatter
    # directly (kb_share_artifact only mints tokens for type: artifact)
    await store.kb_write("projects/alt/artifacts/status-wall.md", LIVE_VIEW, "add live view")
    return "live-view-token-0123456789"


async def test_live_view_share_renders_status_wall(settings: Settings) -> None:
    token = await _seed_live_view(settings)
    resp = _client(settings).get(f"/share/{token}")
    assert resp.status_code == 200
    html = resp.text
    # the live current-phase text is served, rendered at request time
    assert "Shipping the v2 rollout to pilot clients." in html
    assert "rendered just now" in html
    assert "Client Status Wall" in html  # the concept title
    # public: no nav, no source paths, no links into the brain
    assert "/brain/f/" not in html
    assert "Search the brain" not in html
    assert "projects/alt/context.md" not in html


async def test_gallery_marks_live_view_row(settings: Settings) -> None:
    await _seed_live_view(settings)
    open_settings = settings.model_copy(update={"dev_no_access": True})
    resp = _client(open_settings).get("/brain/artifacts")
    assert resp.status_code == 200
    html = resp.text
    assert "Client Status Wall" in html
    assert ">live<" in html  # the 'live' badge


async def test_live_view_guarded_file_view_has_note(settings: Settings) -> None:
    await _seed_live_view(settings)
    open_settings = settings.model_copy(update={"dev_no_access": True})
    resp = _client(open_settings).get("/brain/f/projects/alt/artifacts/status-wall.md")
    assert resp.status_code == 200
    html = resp.text
    assert "This is a live view" in html  # the guarded-view explainer note
    assert "Shipping the v2 rollout to pilot clients." in html  # wall preview renders
