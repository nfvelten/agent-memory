#!/usr/bin/env python3
"""MCP server for agent-memory.

Required env vars:
  AGENT_MEMORY_VAULT   — path to Obsidian vault
  AGENT_MEMORY_INDEX   — path to SQLite+LanceDB index (default: <vault>/.agent-memory/index)
  AGENT_MEMORY_ROOTS   — colon-separated memory root dirs (default: Pessoal + Trabalho inside vault)
  AGENT_MEMORY_WORKS   — colon-separated code dirs to search for CLAUDE.md files
"""
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", Path.home() / "vault"))

# Multiple memory roots: colon-separated list, or defaults to both Pessoal + Trabalho
_roots_env = os.environ.get("AGENT_MEMORY_ROOTS", "")
if _roots_env:
    MEMORY_ROOTS = [Path(p) for p in _roots_env.split(":") if p]
else:
    MEMORY_ROOTS = [
        VAULT / "Pessoal" / "Memória de Agentes",
        VAULT / "Trabalho" / "Memória de Agentes",
    ]

# Keep MEMORY pointing to first root for backwards compat with _insert_into_section
MEMORY = MEMORY_ROOTS[0] if MEMORY_ROOTS else VAULT / "Trabalho" / "Memória de Agentes"

INDEX = Path(os.environ.get("AGENT_MEMORY_INDEX", VAULT / ".agent-memory" / "index"))
SQLITE = INDEX / "memory.sqlite"

SUMMARY_SECTIONS = {
    "Contexto útil", "Comandos", "Demandas relacionadas",
    "Aprendizados persistentes", "Aliases", "Cuidados", "Fonte principal",
}

mcp = FastMCP("agent-memory")


def _db() -> sqlite3.Connection:
    if not SQLITE.exists():
        raise FileNotFoundError(f"Index not found: {SQLITE}. Run: python agent_memory_indexer.py")
    return sqlite3.connect(SQLITE)


def _fts_query(q: str) -> str:
    terms = [t.strip().replace('"', '') for t in q.split() if t.strip()]
    return " OR ".join(f'"{t}"' for t in terms) if terms else q


def _resolve_project_file(projeto: str) -> Path:
    for root in MEMORY_ROOTS:
        f = root / "Projetos" / f"{projeto}.md"
        if f.exists():
            return f
    raise FileNotFoundError(f"Project not found: {projeto} (searched in {[str(r) for r in MEMORY_ROOTS]})")


def _insert_into_section(project_file: Path, section: str, text: str) -> None:
    content = project_file.read_text(errors="ignore")
    if f"## {section}" in content:
        lines = content.splitlines()
        out = []
        in_section = False
        inserted = False
        for line in lines:
            if line.startswith(f"## {section}"):
                in_section = True
                out.append(line)
                continue
            if in_section and line.startswith("## "):
                if not inserted:
                    out.append(f"- {text}")
                    inserted = True
                in_section = False
            if in_section and line == "- ":
                out.append(f"- {text}")
                inserted = True
                in_section = False
                continue
            out.append(line)
        if in_section and not inserted:
            out.append(f"- {text}")
        project_file.write_text("\n".join(out) + "\n")
    else:
        with project_file.open("a") as f:
            f.write(f"\n## {section}\n\n- {text}\n")


NOTES_ROOT = Path(os.environ.get("AGENT_MEMORY_NOTES", VAULT / "Trabalho"))

PT_MONTHS = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def _extract_sections(text: str, sections: list[str]) -> str:
    """Extract only the requested sections from Markdown content."""
    wanted = {s.lower() for s in sections}
    lines = text.splitlines()
    out = []
    current = None
    for line in lines:
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            current = heading if heading in wanted else None
            if current:
                out.append(line)
            continue
        if current:
            out.append(line)
    return "\n".join(out).strip()


@mcp.tool()
def daily_note(date: str, sections: list[str] | None = None) -> str:
    """Return the content of a daily note for a specific date.

    Args:
        date: Date in YYYY-MM-DD format (e.g. 2026-05-21).
        sections: Optional list of headings to extract (e.g. ["Quick Notes", "Tasks: #work"]).
                  When omitted, returns the full note. Use to reduce token usage when only
                  part of the note is relevant.

    Common sections: "Main Focus", "Tasks: #personal", "Tasks: #work",
                     "Quick Notes", "Day Highlight", "Incomplete tasks"
    """
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid format: {date}. Use YYYY-MM-DD.")

    filename = dt.strftime("%d-%m-%Y") + ".md"
    month_name = PT_MONTHS[dt.month]
    year = dt.year

    candidates = [
        VAULT / "Daily Notes" / str(year) / month_name / filename,
        VAULT / "Daily Notes" / month_name / filename,
    ]
    for path in candidates:
        if path.exists():
            content = path.read_text(errors="ignore")
            if sections:
                content = _extract_sections(content, sections)
                if not content:
                    raise ValueError(f"None of the sections {sections} found in {date}")
            return content

    raise FileNotFoundError(f"Daily note not found for {date}: tried {[str(c) for c in candidates]}")


@mcp.tool()
def read_section(path: str, heading: str) -> str:
    """Return a single heading section from a note.

    Use after search() when a snippet points to a relevant section but lacks enough context.

    Args:
        path: Path relative to the vault root (e.g. "Daily Notes/2026/May/21-05-2026.md").
        heading: Heading text without # (e.g. "Quick Notes" or "Persistent Learnings").
    """
    full_path = VAULT / path
    if not full_path.exists():
        raise FileNotFoundError(f"Note not found: {path}")

    lines = full_path.read_text(errors="ignore").splitlines()
    out = []
    in_section = False
    heading_lower = heading.lower()

    for line in lines:
        if line.startswith("#"):
            current = line.lstrip("#").strip().lower()
            if current == heading_lower:
                in_section = True
                out.append(line)
                continue
            elif in_section:
                break
        if in_section:
            out.append(line)

    if not out:
        raise ValueError(f"Section '{heading}' not found in {path}")
    return "\n".join(out)


@mcp.tool()
def summary(projeto: str) -> str:
    """Return operational summary of a project note: commands, context, caveats, learnings."""
    pf = _resolve_project_file(projeto)
    lines = pf.read_text(errors="ignore").splitlines()
    out = [lines[0], ""]
    current = None
    for line in lines[1:]:
        if line.startswith("## "):
            heading = line[3:].strip()
            current = heading if heading in SUMMARY_SECTIONS else None
            if current:
                out.append(line)
            continue
        if current:
            out.append(line)
    return "\n".join(out).strip()


import math
from datetime import timezone

_HALF_LIFE_DAYS: dict[str, float] = {
    "daily":   14.0,
    "nota":    45.0,
    "demanda": 90.0,
    "memoria": 180.0,
    "projeto": 365.0,
    "claude":  365.0,
}
_DEFAULT_HALF_LIFE = 60.0


def _decay_factor(ts: int, kind: str) -> float:
    """Exponential decay: 1.0 (fresh) → 0.0 (very old). Half-life varies by kind."""
    if ts <= 0:
        return 1.0
    age_days = (datetime.now().timestamp() - ts) / 86400
    if age_days <= 0:
        return 1.0
    hl = _HALF_LIFE_DAYS.get(kind, _DEFAULT_HALF_LIFE)
    return math.exp(-math.log(2) * age_days / hl)


_sem_embedder = None


def _sem_embedder_get():
    global _sem_embedder
    if _sem_embedder is None:
        from fastembed import TextEmbedding
        _sem_embedder = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    return _sem_embedder


@mcp.tool()
def semantic_search(
    query: str,
    kind: str = "",
    projeto: str = "",
    limit: int = 10,
) -> list[dict]:
    """Vector similarity search via LanceDB + fastembed (multilingual).

    Use when search() fails due to vocabulary mismatch.
    Example: query "timeout service" finds "high latency work order".

    Args:
        query: Natural language query (any language supported by the model).
        kind: Filter by type — projeto|memoria|demanda|daily|claude|nota.
        projeto: Filter by project name.
        limit: Maximum results (default 10).
    """
    try:
        import lancedb
    except ImportError:
        return [{"error": "lancedb not installed. Run: pip install lancedb fastembed"}]

    lance_dir = INDEX / "vectors"
    if not lance_dir.exists():
        return [{"error": "Vector index not found. Run: python agent_memory_indexer.py"}]

    db = lancedb.connect(str(lance_dir))
    if "chunks" not in db.table_names():
        return [{"error": "Chunks table not found. Run: python agent_memory_indexer.py"}]

    tbl = db.open_table("chunks")
    emb = _sem_embedder_get()
    vec = list(emb.embed([query]))[0]

    q = tbl.search(vec).limit(limit)
    if kind:
        safe_kind = kind.replace("'", "''")
        q = q.where(f"kind = '{safe_kind}'")
    if projeto:
        safe_proj = projeto.replace("'", "''")
        q = q.where(f"project = '{safe_proj}'")

    rows = q.to_list()
    return [
        {
            "path": r["path"],
            "title": r["title"],
            "kind": r["kind"],
            "project": r["project"],
            "heading": r["heading"],
            "snippet": r["content"],
            "distance": round(float(r.get("_distance", 0)), 4),
        }
        for r in rows
    ]


@mcp.tool()
def search(
    query: str,
    projeto: str = "",
    kind: str = "",
    since: str = "",
    tag: str = "",
    limit: int = 10,
    decay: bool = True,
) -> list[dict]:
    """Ranked full-text search over the SQLite FTS5 index with optional temporal decay.

    Args:
        query: Search terms.
        projeto: Scope search to a project (searches note + linked notes + CLAUDE.md).
        kind: Filter by type — projeto|memoria|demanda|daily|claude|nota.
        since: Minimum date filter YYYY-MM-DD (uses filename date for daily notes).
        tag: Filter by Obsidian tag (e.g. "task", "work").
        limit: Maximum results (default 10).
        decay: Apply temporal decay (default True). Old daily notes rank below recent ones.
    """
    conn = _db()
    cur = conn.cursor()

    where = ["chunks_fts MATCH ?"]
    params: list = [_fts_query(query)]

    if projeto:
        allowed: set[str] = set()
        # Search across all memory roots
        for root in MEMORY_ROOTS:
            pf = root / "Projetos" / f"{projeto}.md"
            if pf.exists():
                pf_rel = str(pf.relative_to(VAULT))
                allowed.add(pf_rel)
                for (tp,) in cur.execute(
                    "SELECT target_path FROM links WHERE source_path = ?", (pf_rel,)
                ).fetchall():
                    allowed.add(tp)
        # CLAUDE.md from all work dirs
        work_dirs = [Path(p) for p in os.environ.get("AGENT_MEMORY_WORKS", "").split(":") if p] or \
                    []
        for work in work_dirs:
            claude = work / projeto / "CLAUDE.md"
            if claude.exists():
                allowed.add(str(Path("~") / "code" / work.name / projeto / "CLAUDE.md"))
        if allowed:
            placeholders = ",".join("?" for _ in allowed)
            where.append(f"(documents.project = ? OR documents.path IN ({placeholders}))")
            params.append(projeto)
            params.extend(sorted(allowed))
        else:
            where.append("documents.project = ?")
            params.append(projeto)

    if kind:
        where.append("documents.kind = ?")
        params.append(kind)

    if since:
        try:
            ts = int(datetime.strptime(since, "%Y-%m-%d").timestamp())
            where.append("COALESCE(NULLIF(documents.note_date,0), documents.mtime) >= ?")
            params.append(ts)
        except ValueError:
            pass

    if tag:
        where.append("documents.path IN (SELECT doc_path FROM tags WHERE tag LIKE ?)")
        params.append(f"{tag}%")

    params.append(limit)

    fetch_limit = limit * 3 if decay else limit
    params.append(fetch_limit)

    sql = f"""
    SELECT documents.path, documents.title, documents.kind, documents.project,
           chunks.heading,
           snippet(chunks_fts, 4, '[', ']', ' ... ', 24) AS snippet,
           bm25(chunks_fts) AS rank,
           COALESCE(NULLIF(documents.note_date, 0), documents.mtime) AS ts
    FROM chunks_fts
    JOIN chunks ON chunks_fts.rowid = chunks.id
    JOIN documents ON documents.id = chunks.document_id
    WHERE {' AND '.join(where)}
    ORDER BY rank
    LIMIT ?
    """
    rows = cur.execute(sql, params).fetchall()
    conn.close()

    results = [
        {"path": path, "title": title, "kind": k, "project": proj,
         "heading": heading, "snippet": snippet, "rank": rank, "ts": ts}
        for path, title, k, proj, heading, snippet, rank, ts in rows
    ]

    if decay:
        for r in results:
            d = _decay_factor(r["ts"], r["kind"])
            r["rank"] = r["rank"] * d  # BM25 negative: *decay makes less negative = worse
        results.sort(key=lambda r: r["rank"])
        results = results[:limit]

    for r in results:
        r.pop("ts", None)
    return results


@mcp.tool()
def related(projeto: str, query: str) -> list[dict]:
    """Search a term scoped to a project: its note, linked notes, and CLAUDE.md.

    Use when search() returns broad results and you need exact occurrences
    within the context of a specific project.

    Args:
        projeto: Project name (stem of the file under Projetos/).
        query: Term to search (case-insensitive).

    Returns:
        List of {path, line, text} for each match found.
    """
    pf = MEMORY / "Projetos" / f"{projeto}.md"
    if not pf.exists():
        raise FileNotFoundError(f"Project not found: {projeto}")

    pf_rel = str(pf.relative_to(VAULT))

    # Collect files to search: project note + linked notes + CLAUDE.md/AGENTS.md
    files: list[Path] = [pf]
    conn = _db()
    for (target,) in conn.execute(
        "SELECT target_path FROM links WHERE source_path = ?", (pf_rel,)
    ).fetchall():
        candidate = VAULT / target
        if candidate.exists():
            files.append(candidate)
    conn.close()

    work_dirs = [Path(p) for p in os.environ.get("AGENT_MEMORY_WORKS", "").split(":") if p] or \
                []
    extras: list[Path] = []
    for work in work_dirs:
        extras += [work / projeto / "CLAUDE.md", work / projeto / "AGENTS.md",
                   work / "CLAUDE.md", work / "AGENTS.md"]
    for root in MEMORY_ROOTS:
        extras.append(root / "Demandas.md")
    for extra in extras:
        if extra.exists():
            files.append(extra)

    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []
    seen_paths: set[str] = set()

    for f in files:
        f_str = str(f)
        if f_str in seen_paths:
            continue
        seen_paths.add(f_str)
        try:
            rel = str(f.relative_to(VAULT))
        except ValueError:
            rel = f_str
        for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            if pattern.search(line):
                results.append({"path": rel, "line": i, "text": line.strip()})

    return results


@mcp.tool()
def backlinks(note: str) -> list[dict]:
    """Return all notes that link to a given note (backlinks).

    Useful for discovering which notes reference a concept, project, or person.

    Args:
        note: Filename without extension (e.g. "my-project") or
              path relative to vault root (e.g. "Work/my-project.md").

    Returns:
        List of {source_path, title} sorted by path.
    """
    conn = _db()
    cur = conn.cursor()

    # Match by stem or partial path
    rows = cur.execute(
        """
        SELECT DISTINCT l.source_path, COALESCE(d.title, l.source_path) as title
        FROM links l
        LEFT JOIN documents d ON d.path = l.source_path
        WHERE l.target_path LIKE ? OR l.target_path LIKE ?
        ORDER BY l.source_path
        """,
        (f"%/{note}.md", f"%{note}%"),
    ).fetchall()
    conn.close()
    return [{"source_path": src, "title": title} for src, title in rows]


@mcp.tool()
def graph_traverse(note: str, hops: int = 2) -> dict:
    """BFS traversal of the wikilink graph starting from a note, up to N hops.

    Returns all reachable nodes via wikilinks, with distance from the starting note.
    Useful for discovering related knowledge clusters.

    Args:
        note: Filename without extension (e.g. "my-project").
        hops: Maximum traversal depth (default 2, max 4).

    Returns:
        {nodes: [{path, title, distance}], edges: [{source, target}]}
    """
    hops = min(hops, 4)
    conn = _db()
    cur = conn.cursor()

    # Find starting node — match by stem or partial path
    start_rows = cur.execute(
        "SELECT path FROM documents WHERE path LIKE ? OR path LIKE ? LIMIT 1",
        (f"%/{note}.md", f"%{note}%"),
    ).fetchall()

    if not start_rows:
        conn.close()
        return {"nodes": [], "edges": [], "error": f"Note '{note}' not found"}

    start_path = start_rows[0][0]

    # Recursive CTE: traverse bidirectional graph up to N hops
    rows = cur.execute(
        f"""
        WITH RECURSIVE traverse(path, distance) AS (
          SELECT ?, 0
          UNION
          SELECT l.target_path, t.distance + 1
          FROM traverse t JOIN links l ON l.source_path = t.path
          WHERE t.distance < ?
          UNION
          SELECT l.source_path, t.distance + 1
          FROM traverse t JOIN links l ON l.target_path = t.path
          WHERE t.distance < ?
        )
        SELECT t.path, MIN(t.distance), d.title
        FROM traverse t
        LEFT JOIN documents d ON d.path = t.path
        GROUP BY t.path
        ORDER BY MIN(t.distance)
        """,
        (start_path, hops, hops),
    ).fetchall()

    visited = {r[0] for r in rows}
    node_list = [
        {"path": path, "distance": dist, "title": title or path.split("/")[-1]}
        for path, dist, title in rows
    ]

    # Fetch edges within the traversed set
    edge_rows = cur.execute(
        f"""
        SELECT DISTINCT source_path, target_path FROM links
        WHERE source_path IN ({','.join('?' for _ in visited)})
          AND target_path IN ({','.join('?' for _ in visited)})
        """,
        list(visited) + list(visited),
    ).fetchall() if visited else []

    conn.close()
    return {
        "nodes": node_list,
        "edges": [{"source": s, "target": t} for s, t in edge_rows],
    }


@mcp.tool()
def search_by_tag(tag: str, since: str = "", limit: int = 20) -> list[dict]:
    """Find notes by Obsidian tag (frontmatter YAML or inline #tag).

    Args:
        tag: Tag without # (e.g. "work", "task", "personal").
             Supports prefix matching: "task" finds "task", "task/done", etc.
        since: Minimum date YYYY-MM-DD (filters by note date or mtime).
        limit: Maximum results (default 20).

    Returns:
        List of {path, title, kind, tags} sorted by date descending.
    """
    conn = _db()
    cur = conn.cursor()

    params: list = [f"{tag}%"]
    where = "t.tag LIKE ?"

    if since:
        try:
            ts = int(datetime.strptime(since, "%Y-%m-%d").timestamp())
            where += " AND COALESCE(NULLIF(d.note_date,0), d.mtime) >= ?"
            params.append(ts)
        except ValueError:
            pass

    params.append(limit)

    rows = cur.execute(
        f"""
        SELECT DISTINCT d.path, d.title, d.kind,
               GROUP_CONCAT(t2.tag, ', ') as all_tags
        FROM tags t
        JOIN documents d ON d.path = t.doc_path
        LEFT JOIN tags t2 ON t2.doc_path = d.path
        WHERE {where}
        GROUP BY d.path
        ORDER BY COALESCE(NULLIF(d.note_date,0), d.mtime) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    conn.close()
    return [
        {"path": path, "title": title, "kind": kind, "tags": tags}
        for path, title, kind, tags in rows
    ]


@mcp.tool()
def projects() -> list[dict]:
    """List all projects with a memory note, sorted by most recently modified.

    Returns:
        List of {name, mtime, scope} sorted newest to oldest.
    """
    result = []
    seen: set[str] = set()
    for root in MEMORY_ROOTS:
        projetos_dir = root / "Projetos"
        if not projetos_dir.exists():
            continue
        scope = root.parent.name  # "Pessoal" or "Trabalho"
        for p in projetos_dir.glob("*.md"):
            if p.stem not in seen:
                seen.add(p.stem)
                result.append({
                    "name": p.stem,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d"),
                    "scope": scope,
                })
    return sorted(result, key=lambda x: x["mtime"], reverse=True)


@mcp.tool()
def recent_notes(days: int = 7, kind: str = "", limit: int = 20) -> list[dict]:
    """Return notes modified recently in the vault.

    Useful for resuming context after a break or seeing what changed.

    Args:
        days: Look-back window in days (default 7).
        kind: Filter by type — projeto|memoria|demanda|daily|claude|nota.
        limit: Maximum results (default 20).

    Returns:
        List of {path, title, kind, modified} sorted newest first.
    """
    from datetime import timedelta
    conn = _db()
    since_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    where = "note_date >= ?"
    params: list = [since_ts]
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT path, title, kind,
               datetime(note_date, 'unixepoch', 'localtime') as modified
        FROM documents
        WHERE {where}
        ORDER BY note_date DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    conn.close()
    return [
        {"path": path, "title": title, "kind": kind, "modified": modified}
        for path, title, kind, modified in rows
    ]


@mcp.tool()
def week_summary(date: str) -> str:
    """Return a compressed summary of the week containing the given date.

    Aggregates daily notes from up to 7 days into a compact view.
    Use instead of calling daily_note() 5-7 times when the question is about
    weekly activity.

    Args:
        date: Any date within the target week in YYYY-MM-DD format.
    """
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid format: {date}. Use YYYY-MM-DD.")

    from datetime import timedelta

    SUMMARY_SECTIONS = ["Main Focus", "Tasks: #work", "Tasks: #personal", "Quick Notes", "Day Highlight"]

    days = []
    for i in range(6, -1, -1):
        day = dt - timedelta(days=dt.weekday()) + timedelta(days=i)
        # Clamp to not go past the requested date
        if day > dt:
            continue
        filename = day.strftime("%d-%m-%Y") + ".md"
        month_name = PT_MONTHS[day.month]
        for path in [
            VAULT / "Daily Notes" / str(day.year) / month_name / filename,
            VAULT / "Daily Notes" / month_name / filename,
        ]:
            if path.exists():
                content = _extract_sections(path.read_text(errors="ignore"), SUMMARY_SECTIONS)
                if content.strip():
                    days.append(f"### {day.strftime('%d/%m')} ({['Seg','Ter','Qua','Qui','Sex','Sáb','Dom'][day.weekday()]})\n{content}")
                break

    if not days:
        return "No daily notes found for that week."
    return "\n\n".join(days)


@mcp.tool()
def inbox() -> list[dict]:
    """Return numbered inbox items. Use the item number with promote_inbox."""
    inbox_file = MEMORY / "Inbox.md"
    if not inbox_file.exists():
        return []
    items = []
    n = 0
    section = ""
    for line in inbox_file.read_text(errors="ignore").splitlines():
        if re.match(r'^## \d{2}-\d{2}-\d{4}$', line):
            section = line[3:]
        elif line.startswith("- "):
            n += 1
            items.append({"n": n, "date": section, "text": line[2:]})
    return items


@mcp.tool()
def capture(text: str) -> str:
    """Append a new item to the inbox for later review."""
    inbox_file = MEMORY / "Inbox.md"
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%d-%m-%Y")
    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    content = inbox_file.read_text(errors="ignore") if inbox_file.exists() else ""
    if f"## {today}" not in content:
        with inbox_file.open("a") as f:
            f.write(f"\n## {today}\n\n")
    with inbox_file.open("a") as f:
        f.write(f"- {timestamp} - {text}\n")
    return f"Capturado em {inbox_file}"


@mcp.tool()
def promote(projeto: str, text: str) -> str:
    """Add a persistent learning to the project note's 'Persistent Learnings' section."""
    pf = _resolve_project_file(projeto)
    _insert_into_section(pf, "Aprendizados persistentes", text)
    return f"Adicionado em {pf}"


@mcp.tool()
def promote_inbox(n: int, projeto: str) -> str:
    """Move inbox item N to the project's 'Persistent Learnings' section and remove it from the inbox."""
    inbox_file = MEMORY / "Inbox.md"
    if not inbox_file.exists():
        raise FileNotFoundError("Inbox not found.")
    pf = _resolve_project_file(projeto)

    lines = inbox_file.read_text(errors="ignore").splitlines()
    count = 0
    item_text = ""
    new_lines = []
    for line in lines:
        if line.startswith("- "):
            count += 1
            if count == n:
                item_text = line[2:]
                continue
        new_lines.append(line)

    if not item_text:
        raise ValueError(f"Item {n} not found in inbox.")

    inbox_file.write_text("\n".join(new_lines) + "\n")
    _insert_into_section(pf, "Aprendizados persistentes", item_text)
    return f"Movido inbox[{n}] → {pf}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
