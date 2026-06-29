#!/usr/bin/env python3
"""
Incremental SQLite + LanceDB indexer for agent-memory.

Full rebuild:      python agent_memory_indexer.py
Single file:       python agent_memory_indexer.py --file <abs_path>
Remove file:       python agent_memory_indexer.py --remove <abs_path>
Rebuild vectors:   python agent_memory_indexer.py --rebuild-vectors
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Vector index (LanceDB + fastembed) — optional, degrades gracefully
# ---------------------------------------------------------------------------
_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_EMBED_DIMS = 384
_embedder = None
_lance_db = None
_lance_tbl = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(_EMBED_MODEL)
    return _embedder


def _get_lance_table():
    global _lance_db, _lance_tbl
    if _lance_tbl is not None:
        return _lance_tbl
    try:
        import lancedb
        import pyarrow as pa
        _lance_db = lancedb.connect(str(INDEX / "vectors"))
        schema = pa.schema([
            pa.field("chunk_id", pa.int64()),
            pa.field("path", pa.utf8()),
            pa.field("title", pa.utf8()),
            pa.field("kind", pa.utf8()),
            pa.field("project", pa.utf8()),
            pa.field("heading", pa.utf8()),
            pa.field("content", pa.utf8()),
            pa.field("vector", pa.list_(pa.float32(), _EMBED_DIMS)),
        ])
        tbl_names = _lance_db.list_tables()
        if "chunks" in tbl_names:
            _lance_tbl = _lance_db.open_table("chunks")
        else:
            _lance_tbl = _lance_db.create_table("chunks", schema=schema, mode="overwrite")
        return _lance_tbl
    except Exception:
        return None


def _lance_delete(rel: str) -> None:
    tbl = _get_lance_table()
    if tbl is None:
        return
    try:
        safe = rel.replace("'", "''")
        tbl.delete(f"path = '{safe}'")
    except Exception:
        pass


def _lance_upsert(rel: str, title: str, kind: str, project: str,
                  chunks: list[tuple[int, str, str]]) -> None:
    """chunks: list of (chunk_id, heading, content)"""
    if not chunks:
        return
    try:
        emb = _get_embedder()
        tbl = _get_lance_table()
        if tbl is None:
            return
        texts = [f"{h} {c}"[:512] for _, h, c in chunks]
        vectors = list(emb.embed(texts))
        records = [
            {
                "chunk_id": cid,
                "path": rel,
                "title": title,
                "kind": kind,
                "project": project,
                "heading": heading,
                "content": content[:500],
                "vector": vec.tolist(),
            }
            for (cid, heading, content), vec in zip(chunks, vectors)
        ]
        tbl.add(records)
    except Exception:
        pass

VAULT = Path(os.environ.get("AGENT_MEMORY_VAULT", Path.home() / "vault"))
_works_env = os.environ.get("AGENT_MEMORY_WORKS", "")
WORK_DIRS = [Path(p) for p in _works_env.split(":") if p] if _works_env else []
INDEX = Path(os.environ.get("AGENT_MEMORY_INDEX", VAULT / ".agent-memory" / "index"))
SQLITE = INDEX / "memory.sqlite"

SKIP_PARTS = {".git", ".obsidian", ".opencode", ".trash", "node_modules", ".index", "Templates", ".agent-memory"}

PT_MONTHS = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def should_skip(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(p in SKIP_PARTS for p in rel.parts)


_MEMORY_ROOTS_ENV = os.environ.get("AGENT_MEMORY_ROOTS", "")
_MEMORY_ROOTS: list[Path] = [Path(p) for p in _MEMORY_ROOTS_ENV.split(":") if p] if _MEMORY_ROOTS_ENV else [
    VAULT / "Pessoal" / "Memória de Agentes",
    VAULT / "Trabalho" / "Memória de Agentes",
]


def classify(path: Path) -> tuple[str, str]:
    rel = str(path)
    for mem_root in _MEMORY_ROOTS:
        mem_str = str(mem_root)
        if not rel.startswith(mem_str):
            continue
        sub = path.relative_to(mem_root)
        parts = sub.parts
        if parts[0] == "Projetos":
            return "projeto", path.stem
        if parts[0] == "Demandas.md":
            return "demanda", ""
        return "memoria", ""
    if "Daily Notes" in rel:
        return "daily", ""
    if path.name in ("CLAUDE.md", "AGENTS.md"):
        proj = path.parent.name
        return "claude", proj
    return "nota", ""


def title_for(path: Path, text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def parse_note_date(path: Path) -> int:
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", path.stem)
    if m:
        dd, mm, yyyy = m.groups()
        try:
            return int(datetime(int(yyyy), int(mm), int(dd)).timestamp())
        except ValueError:
            pass
    return 0


def chunks_for(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("#"):
            if current_lines:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = line.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_heading, "\n".join(current_lines).strip()))
    return [(h, c) for h, c in sections if c]


def path_to_rel(path: Path) -> str:
    if path.is_relative_to(VAULT):
        return str(path.relative_to(VAULT))
    for work in WORK_DIRS:
        if path.is_relative_to(work):
            return str(Path("~") / "code" / work.name / path.relative_to(work))
    return str(path)


def extract_tags(text: str) -> list[str]:
    """Extract Obsidian tags from frontmatter YAML and inline #tag syntax."""
    tags: set[str] = set()
    lines = text.splitlines()
    # Frontmatter YAML
    in_frontmatter = False
    in_tags_block = False
    for line in lines:
        if line.strip() == "---":
            in_frontmatter = not in_frontmatter
            in_tags_block = False
            continue
        if in_frontmatter:
            if re.match(r'^tags\s*:', line):
                # Inline: tags: [tag1, tag2] or tags: tag1
                inline = re.findall(r'[\w/:-]+', line.split(':', 1)[1])
                tags.update(t.lower() for t in inline if t)
                in_tags_block = True
            elif in_tags_block and line.startswith('  - '):
                tags.add(line.strip().lstrip('- ').lower())
            elif in_tags_block and not line.startswith(' '):
                in_tags_block = False
    # Inline tags: #tag (not ## heading)
    for m in re.finditer(r'(?<![#\w])#([a-zA-Z][a-zA-Z0-9_/-]*)', text):
        tags.add(m.group(1).lower())
    return sorted(tags)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS documents (
      id INTEGER PRIMARY KEY,
      path TEXT UNIQUE NOT NULL,
      title TEXT NOT NULL,
      kind TEXT NOT NULL,
      project TEXT NOT NULL,
      mtime INTEGER NOT NULL,
      note_date INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS links (
      source_path TEXT NOT NULL,
      target_path TEXT NOT NULL,
      UNIQUE(source_path, target_path)
    );
    CREATE INDEX IF NOT EXISTS links_source ON links(source_path);
    CREATE INDEX IF NOT EXISTS links_target ON links(target_path);
    CREATE TABLE IF NOT EXISTS tags (
      doc_path TEXT NOT NULL,
      tag TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
    CREATE INDEX IF NOT EXISTS tags_path ON tags(doc_path);
    CREATE TABLE IF NOT EXISTS chunks (
      id INTEGER PRIMARY KEY,
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      heading TEXT NOT NULL,
      content TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
      title,
      kind,
      project,
      heading,
      content,
      tokenize="unicode61 remove_diacritics 2"
    );
    """)
    conn.commit()


def _delete_document(conn: sqlite3.Connection, rel: str) -> None:
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM documents WHERE path = ?", (rel,)).fetchone()
    if row:
        doc_id = row[0]
        chunk_ids = [r[0] for r in cur.execute(
            "SELECT id FROM chunks WHERE document_id = ?", (doc_id,)
        ).fetchall()]
        if chunk_ids:
            placeholders = ",".join("?" for _ in chunk_ids)
            cur.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", chunk_ids)
        cur.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
        cur.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        cur.execute("DELETE FROM links WHERE source_path = ?", (rel,))
        cur.execute("DELETE FROM tags WHERE doc_path = ?", (rel,))
    _lance_delete(rel)


def _index_file(conn: sqlite3.Connection, path: Path,
                by_stem: dict, by_rel: dict) -> str:
    """Index a single file. Returns 'inserted', 'updated', or 'skipped'."""
    cur = conn.cursor()
    rel = path_to_rel(path)
    mtime = int(path.stat().st_mtime)

    # Skip if unchanged
    row = cur.execute(
        "SELECT mtime FROM documents WHERE path = ?", (rel,)
    ).fetchone()
    if row and row[0] == mtime:
        return "skipped"

    try:
        text = path.read_text(errors="ignore")
    except Exception:
        return "skipped"
    if not text.strip():
        return "skipped"

    kind, project = classify(path)
    title = title_for(path, text)
    # daily notes: parse from filename; others: use mtime so `since` filter works
    note_date = parse_note_date(path) if kind == "daily" else mtime

    # Remove existing data
    _delete_document(conn, rel)

    # Insert document
    cur.execute(
        "INSERT INTO documents(path,title,kind,project,mtime,note_date) VALUES (?,?,?,?,?,?)",
        (rel, title, kind, project, mtime, note_date),
    )
    doc_id = cur.lastrowid
    inserted_chunk = False
    vec_chunks: list[tuple[int, str, str]] = []

    for heading, content in chunks_for(text):
        cur.execute(
            "INSERT INTO chunks(document_id,heading,content) VALUES (?,?,?)",
            (doc_id, heading, content),
        )
        chunk_id = cur.lastrowid
        cur.execute(
            "INSERT INTO chunks_fts(rowid,title,kind,project,heading,content) VALUES (?,?,?,?,?,?)",
            (chunk_id, title, kind, project, heading, content),
        )
        vec_chunks.append((chunk_id, heading, content))
        inserted_chunk = True

    if not inserted_chunk:
        cur.execute(
            "INSERT INTO chunks(document_id,heading,content) VALUES (?,?,?)",
            (doc_id, title, text),
        )
        chunk_id = cur.lastrowid
        cur.execute(
            "INSERT INTO chunks_fts(rowid,title,kind,project,heading,content) VALUES (?,?,?,?,?,?)",
            (chunk_id, title, kind, project, title, text),
        )
        vec_chunks.append((chunk_id, title, text))

    _lance_upsert(rel, title, kind, project, vec_chunks)

    # Re-index links for this file
    if path.is_relative_to(VAULT):
        for raw in re.findall(r"\[\[([^\]|#]+)", text):
            name = raw.strip()
            # Try exact rel path match first, then stem match
            target = by_rel.get(name) or by_rel.get(Path(name).stem)
            if target is None:
                matches = by_stem.get(Path(name).stem, [])
                target = matches[0] if len(matches) == 1 else None
            if target:
                target_rel = str(target.relative_to(VAULT))
                if target_rel != rel:  # no self-links
                    cur.execute(
                        "INSERT OR IGNORE INTO links(source_path,target_path) VALUES (?,?)",
                        (rel, target_rel),
                    )

    # Index tags
    for tag in extract_tags(text):
        cur.execute("INSERT INTO tags(doc_path, tag) VALUES (?,?)", (rel, tag))

    return "updated" if row else "inserted"


def _build_stem_maps() -> tuple[dict, dict]:
    vault_files = list(VAULT.rglob("*.md"))
    by_stem: dict = {}
    by_rel: dict = {}
    for vf in vault_files:
        by_stem.setdefault(vf.stem, []).append(vf)
        by_rel[str(vf.relative_to(VAULT))[:-3]] = vf
    return by_stem, by_rel


def collect_sources() -> list[Path]:
    sources: list[Path] = []
    for path in VAULT.rglob("*.md"):
        if not should_skip(path, VAULT):
            sources.append(path)
    for work in WORK_DIRS:
        if not work.exists():
            continue
        for proj_dir in work.iterdir():
            if not proj_dir.is_dir():
                continue
            for name in ("CLAUDE.md", "AGENTS.md"):
                p = proj_dir / name
                if p.exists() and p not in sources:
                    sources.append(p)
    return sources


def full_rebuild(conn: sqlite3.Connection) -> None:
    """Rebuild index: add new, update changed, remove deleted."""
    sources = collect_sources()
    by_stem, by_rel = _build_stem_maps()

    # Find deleted documents
    cur = conn.cursor()
    indexed_paths = {r[0] for r in cur.execute("SELECT path FROM documents").fetchall()}
    source_rels = set()
    for p in sources:
        source_rels.add(path_to_rel(p))

    deleted = indexed_paths - source_rels
    for rel in deleted:
        _delete_document(conn, rel)

    counts = {"inserted": 0, "updated": 0, "skipped": 0}
    for path in sources:
        result = _index_file(conn, path, by_stem, by_rel)
        counts[result] += 1

    conn.commit()
    print(f"inserted={counts['inserted']} updated={counts['updated']} "
          f"skipped={counts['skipped']} deleted={len(deleted)}", flush=True)


def index_single_file(conn: sqlite3.Connection, path: Path) -> None:
    by_stem, by_rel = _build_stem_maps()
    result = _index_file(conn, path, by_stem, by_rel)
    conn.commit()
    print(f"{result}: {path}", flush=True)


def remove_file(conn: sqlite3.Connection, path: Path) -> None:
    rel = path_to_rel(path)
    _delete_document(conn, rel)
    conn.commit()
    print(f"removed: {path}", flush=True)


def rebuild_vectors() -> None:
    """Rebuild LanceDB vector index from existing SQLite chunks (skips mtime check)."""
    conn = sqlite3.connect(SQLITE)
    rows = conn.execute(
        """SELECT c.id, d.path, d.title, d.kind, d.project, c.heading, c.content
           FROM chunks c JOIN documents d ON d.id = c.document_id"""
    ).fetchall()
    conn.close()

    tbl = _get_lance_table()
    if tbl is None:
        print("LanceDB not available. Install: pip install lancedb fastembed", flush=True)  # noqa: T201
        return

    # Drop and recreate to avoid duplicates
    try:
        import lancedb
        db = lancedb.connect(str(INDEX / "vectors"))
        if "chunks" in db.list_tables():
            db.drop_table("chunks")
        global _lance_tbl, _lance_db
        _lance_tbl = None
        _lance_db = None
        tbl = _get_lance_table()
    except Exception as e:
        print(f"LanceDB reset failed: {e}", flush=True)
        return

    BATCH = 64
    total = len(rows)
    embedded = 0
    emb = _get_embedder()

    for i in range(0, total, BATCH):
        batch = rows[i:i + BATCH]
        texts = [f"{h} {c}"[:512] for _, _, _, _, _, h, c in batch]
        try:
            vectors = list(emb.embed(texts))
            records = [
                {
                    "chunk_id": cid,
                    "path": path,
                    "title": title,
                    "kind": kind,
                    "project": project,
                    "heading": heading,
                    "content": content[:500],
                    "vector": vec.tolist(),
                }
                for (cid, path, title, kind, project, heading, content), vec
                in zip(batch, vectors)
            ]
            tbl.add(records)
            embedded += len(records)
            print(f"  {embedded}/{total} chunks embedded...", end="\r", flush=True)
        except Exception as e:
            print(f"\nBatch {i} failed: {e}", flush=True)

    print(f"\nVector index rebuilt: {embedded} chunks", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, help="Index single file (incremental)")
    parser.add_argument("--remove", type=Path, help="Remove file from index")
    parser.add_argument("--rebuild-vectors", action="store_true",
                        help="Rebuild LanceDB vector index from existing SQLite chunks")
    args = parser.parse_args()

    INDEX.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)

    if args.remove:
        remove_file(conn, args.remove)
    elif args.file:
        index_single_file(conn, args.file)
    elif args.rebuild_vectors:
        conn.close()
        rebuild_vectors()
        return
    else:
        full_rebuild(conn)

    conn.close()


if __name__ == "__main__":
    main()
