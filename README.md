# agent-memory

MCP server and CLI that turns an Obsidian vault into persistent memory for AI agents.

Agents can search notes by keyword or semantic similarity, read daily journals, browse project context, traverse backlink graphs, capture new learnings, and more — all without touching the filesystem directly.

## How it works

```
Obsidian vault (Markdown)
        │
        ▼
  agent_memory_indexer.py   ← builds SQLite FTS5 + LanceDB vector index
        │
        ▼
  mcp_server.py             ← exposes 16 MCP tools via stdio
        │
        ▼
  AI agent (Claude, Cursor, etc.)
```

The index lives inside the vault (`<vault>/.agent-memory/index/`) and is updated incrementally on each file change via `bin/agent-memory-watch`.

## MCP tools

| Tool | Description |
|------|-------------|
| `search` | Ranked FTS5 search with temporal decay — recent notes rank higher |
| `semantic_search` | Vector similarity search via LanceDB + fastembed |
| `daily_note` | Read a daily note by date (YYYY-MM-DD), optionally filtered to specific sections |
| `week_summary` | Aggregate summary of a full week's daily notes |
| `read_section` | Read a single heading section from any note |
| `summary` | Operational summary of a project note (commands, context, caveats) |
| `projects` | List all project notes sorted by recent modification |
| `related` | Search a term scoped to a project and its linked notes |
| `backlinks` | Find all notes that link to a given note |
| `graph_traverse` | BFS traversal of the wikilink graph up to N hops |
| `search_by_tag` | Find notes by Obsidian tag (frontmatter or inline `#tag`) |
| `recent_notes` | Notes modified in the last N days |
| `inbox` | Read pending inbox items |
| `capture` | Append a new item to the inbox |
| `promote` | Add a learning to a project's persistent section |
| `promote_inbox` | Move an inbox item to a project note |

## Quick start

### 1. Install dependencies

```bash
pip install mcp fastembed lancedb
```

`fastembed` and `lancedb` are optional — only needed for `semantic_search`. All other tools work with SQLite only.

### 2. Configure

```bash
cp examples/config.example.env my.env
# Edit my.env with your vault path
source my.env
```

### 3. Build the index

```bash
# From the project root:
python agent_memory_indexer.py
```

Or incrementally (watch mode):

```bash
bin/agent-memory-watch
```

### 4. Connect to Claude Code

Add to `~/.claude.json` (or your MCP config):

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "/path/to/agent-memory/bin/agent-memory-mcp",
      "env": {
        "AGENT_MEMORY_VAULT": "/path/to/your/vault"
      }
    }
  }
}
```

For separate personal/work contexts, use `bin/agent-memory-mcp-personal` and `bin/agent-memory-mcp-work`.

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AGENT_MEMORY_VAULT` | Path to Obsidian vault | `~/vault` |
| `AGENT_MEMORY_ROOTS` | Colon-separated list of memory root dirs | `<vault>/Pessoal/Memória de Agentes:<vault>/Trabalho/Memória de Agentes` |
| `AGENT_MEMORY_WORKS` | Colon-separated list of code dirs (indexes `CLAUDE.md` files) | _(empty)_ |
| `AGENT_MEMORY_INDEX` | Index directory | `<vault>/.agent-memory/index` |

## Vault structure expected

The server is designed around an Obsidian vault with this layout, but adapts to any structure via env vars:

```
vault/
├── Daily Notes/
│   └── 2026/
│       └── June/
│           └── 29-06-2026.md
├── <Section>/
│   └── Memória de Agentes/     ← memory root
│       ├── Inbox.md
│       └── Projetos/
│           └── my-project.md
```

Project notes support structured sections: `## Useful Context`, `## Commands`, `## Related Demands`, `## Persistent Learnings`, `## Caveats`, `## Main Source`.

## CLI

The `bin/agent-memory` script wraps common operations:

```bash
bin/agent-memory summary <project>
bin/agent-memory search --project <project> <term>
bin/agent-memory links <project>
bin/agent-memory inbox
bin/agent-memory capture 'text to save'
bin/agent-memory doctor
```

## Index internals

- **SQLite FTS5** — full-text search with BM25 ranking, tags, links, and per-document timestamps
- **LanceDB + fastembed** — `paraphrase-multilingual-MiniLM-L12-v2` (384d), multilingual
- Temporal decay: BM25 scores are multiplied by an exponential decay factor per document kind (daily notes decay faster than project notes)

## License

MIT
