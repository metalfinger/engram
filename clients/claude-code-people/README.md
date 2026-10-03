# People pane (Claude Code mod)

A pane inside Claude Code that shows who in your space is working on what and where your work overlaps with theirs. You can also message them from it. It talks only to the Engram tools your own connector already exposes. It adds no server and no new endpoint.

- `/people` opens the pane. On first use it asks what you are working on.
- `/people checkin` updates your card.
- The status line shows `✉ N unread` while DMs are waiting, and a toast appears when a new one lands. Live updates come from polling `kb_messages` every 5 seconds, so they cost no model tokens.

## What it publishes

One concept, `self/now.md`: one line about what you are working on, your topics and your space. It is published `public` (any signed-in Engram user) only after you press **Publish card**. The pane shows exactly what will be published first. Topic suggestions come from your own session (folder, project pin, branch, recent prompts) via a small model call on your machine. That context is never sent to Engram; only the topics you keep are.

Message text is shown as data. It is never turned into a prompt.

## Install

Claude Code 2.1.288 or newer.

**One session:**

```
claude --plugin-dir <repo>/clients/claude-code-people
```

**Every session:** add the folder to the `env` block of `~/.claude/settings.json`. Use `:` between paths on macOS/Linux and `;` on Windows:

```json
{ "env": { "CLAUDE_CODE_PLUGIN_DIRS": "C:\\path\\to\\engram\\clients\\claude-code-people" } }
```

## Which Engram account it uses

The mod calls Engram through an MCP server that is already connected in Claude Code. You choose which one in `/config` under **Engram MCP server**:

| Setting | Account |
|---|---|
| `claude.ai engram` (default) | whoever the claude.ai connector is signed in as |
| `engram` | a local connector, e.g. `claude mcp add --transport http engram https://engram.metalfinger.xyz/mcp`, then `/mcp` → engram → Authenticate as the other account |

The pane header always shows `@handle`, so you can see at a glance which account you are signed in as. If two machines share one claude.ai login, give one of them the local `engram` connector.

**Space** (default `vibechk`): everyone whose card names the same space appears in your pane.

## Develop

```
claude plugin validate clients/claude-code-people
claude plugin test clients/claude-code-people
```
