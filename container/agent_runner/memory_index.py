from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

try:
    import tiktoken  # type: ignore
    _HAS_TIKTOKEN = True
except ImportError:  # pragma: no cover 容器内必有
    tiktoken = None  # type: ignore
    _HAS_TIKTOKEN = False

try:
    import numpy as _np  # type: ignore
except ImportError:  # pragma: no cover - 容器内必有；主机测试走纯 Python 兜底
    _np = None  # type: ignore


CHUNK_TARGET_TOKENS = 400
CHUNK_OVERLAP_TOKENS = 80
SNIPPET_CHARS = 700
DEFAULT_MAX_RESULTS = 6
EMBEDDING_PROVIDER = "openai"
EMBED_BATCH_SIZE = 64
EMBED_BUDGET_FAST = 64
EMBED_BUDGET_FULL = 1024
VECTOR_WEIGHT = 0.7
TEXT_WEIGHT = 0.3
MIN_SCORE = 0.35
DECAY_HALF_LIFE_DAYS = 30.0
DECAY_SOURCES = {"memory", "session"}   # knowledge/local 永不衰减

_FINGERPRINT_RAW = (
    f"v1|chunk={CHUNK_TARGET_TOKENS}|overlap={CHUNK_OVERLAP_TOKENS}|tok=trigram"
)

SOURCE_ROOTS = [
    ("CLAUDE.local.md", "local"),
    ("memory", "memory"),
    ("sessions", "session"),
    ("knowledge", "knowledge"),
]

INDEXED_EXTENSIONS = {".md", ".txt"}

_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  path       TEXT PRIMARY KEY,
  source     TEXT NOT NULL,
  sha256     TEXT NOT NULL,
  mtime      REAL NOT NULL,
  size       INTEGER NOT NULL,
  indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  id         INTEGER PRIMARY KEY,
  path       TEXT NOT NULL,
  source     TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,
  hash       TEXT NOT NULL,
  text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);

CREATE TABLE IF NOT EXISTS embedding_cache (
  provider   TEXT NOT NULL,
  model      TEXT NOT NULL,
  hash       TEXT NOT NULL,
  dims       INTEGER NOT NULL,
  vector     BLOB NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (provider, model, hash)
);
"""


def _log(msg: str) -> None:
    print(f"[memory-index] {msg}", file=sys.stderr, flush=True)

def agent_dir() -> str:
    return os.environ.get("CLAWSIDE_AGENT_DIR", "/workspace/agent")

def _db_path() -> str:
    return os.path.join(agent_dir(), "memory.db")

def open_index_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None

def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
    )

def ensure_schema(conn: sqlite3.Connection) -> str:
    conn.executescript(_SCHEMA)
    fts_mode = _get_meta(conn, "fts_mode")
    if fts_mode is None:
        for mode in ("trigram", "unicode61"):
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                    "text, content='chunks', content_rowid='id', "
                    f"tokenize='{mode}')"
                )
                fts_mode = mode
                break
            except sqlite3.OperationalError as e:
                _log(f"fts5 tokenizer {mode!r} unavailable: {e}")
        if fts_mode is None:
            fts_mode = "none"
            _log("FTS5 unavailable — search degrades to LIKE scan")
        _set_meta(conn, "fts_mode", fts_mode)
    conn.commit()
    return fts_mode


def _fingerprint() -> str:
    return hashlib.sha256(_FINGERPRINT_RAW.encode("utf-8")).hexdigest()


def _wipe_index(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM files")
    try:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
    except sqlite3.OperationalError:
        pass
    conn.execute("DELETE FROM meta WHERE key = 'fingerprint'")


def _check_fingerprint(conn: sqlite3.Connection) -> None:
    """fingerprint 不匹配 → 清空索引（派生物任性重建，不做迁移）。"""
    fp = _fingerprint()
    stored = _get_meta(conn, "fingerprint")
    if stored == fp:
        return
    if stored is not None:
        _log("fingerprint changed — dropping index for full rebuild")
        _wipe_index(conn)
    _set_meta(conn, "fingerprint", fp)
    conn.commit()

_encoder = None
_encoder_loaded = False


def _count_tokens(s: str) -> int:
    global _encoder, _encoder_loaded
    if not _encoder_loaded:
        _encoder_loaded = True
        if _HAS_TIKTOKEN:
            try:
                _encoder = tiktoken.get_encoding("cl100k_base")
            except Exception:
                _encoder = None
    if _encoder is not None:
        try:
            return len(_encoder.encode(s))
        except Exception:
            pass
    return max(1, len(s) // 4)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class Chunk:
    start_line: int
    end_line: int
    text: str
    hash: str


def chunk_text(text: str) -> list[Chunk]:
    """带 overlap 的行感知滑动窗口
    """
    lines = text.split("\n")
    n = len(lines)
    chunks: list[Chunk] = []
    i = 0
    while i < n:
        tok = 0
        j = i
        while j < n and tok < CHUNK_TARGET_TOKENS:
            tok += _count_tokens(lines[j]) + 1
            j += 1
        block = "\n".join(lines[i:j])
        if block.strip():
            chunks.append(
                Chunk(start_line=i + 1, end_line=j, text=block, hash=_sha256(block))
            )
        if j >= n:
            break
        back = 0
        k = j
        while k > i + 1 and back < CHUNK_OVERLAP_TOKENS:
            k -= 1
            back += _count_tokens(lines[k]) + 1
        i = k
    return chunks


_embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None
_embed_disabled = False   # 进程内熔断 一次失败后不再反复重试


def _embedding_model() -> str:
    return os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small").strip()


def _disable_embedding(reason: str) -> None:
    global _embed_disabled
    _embed_disabled = True
    _log(f"embedding disabled for this process: {reason}")


def _get_embed_fn() -> Optional[Callable[[list[str]], list[list[float]]]]:
    global _embed_fn
    if _embed_disabled:
        return None
    if _embed_fn is not None:
        return _embed_fn
    model = _embedding_model()
    if not model:
        _disable_embedding("EMBEDDING_MODEL is empty")
        return None
    if not os.environ.get("OPENAI_API_KEY"):
        _disable_embedding("no OPENAI_API_KEY")
        return None
    try:
        from openai import OpenAI  # 容器内必有 主机测试环境可能没有
    except ImportError:
        _disable_embedding("openai package unavailable")
        return None
    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        timeout=20.0,
        max_retries=2,
    )

    def _fn(texts: list[str]) -> list[list[float]]:
        resp = client.embeddings.create(model=model, input=texts)
        data = sorted(resp.data, key=lambda d: d.index)
        return [d.embedding for d in data]

    _embed_fn = _fn
    return _fn


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine_scores(qvec: list[float], blobs: list[bytes]) -> list[float]:
    """query 向量 × 每个候选 blob 的余弦相似度。numpy 为主，纯 Python 兜底"""
    if _np is not None:
        q = _np.asarray(qvec, dtype=_np.float32)
        qn = float(_np.linalg.norm(q))
        if qn == 0.0:
            return [0.0] * len(blobs)
        m = _np.vstack([_np.frombuffer(b, dtype=_np.float32) for b in blobs])
        norms = _np.linalg.norm(m, axis=1)
        norms[norms == 0] = 1.0
        return ((m @ q) / (norms * qn)).tolist()
    qn = math.sqrt(sum(x * x for x in qvec))
    if qn == 0.0:
        return [0.0] * len(blobs)
    out: list[float] = []
    for b in blobs:
        v = _unpack_vector(b)
        vn = math.sqrt(sum(y * y for y in v)) or 1.0
        dot = sum(x * y for x, y in zip(qvec, v))
        out.append(dot / (vn * qn))
    return out


def _backfill_embeddings(conn: sqlite3.Connection, budget: int) -> tuple[int, int]:
    fn = _get_embed_fn()
    if fn is None:
        return 0, 0
    model = _embedding_model()
    rows = conn.execute(
        """
        SELECT DISTINCT c.hash, c.text FROM chunks c
        LEFT JOIN embedding_cache e
          ON e.hash = c.hash AND e.provider = ? AND e.model = ?
        WHERE e.hash IS NULL
        """,
        (EMBEDDING_PROVIDER, model),
    ).fetchall()
    if not rows:
        return 0, 0
    todo = rows[:budget]
    embedded = 0
    for i in range(0, len(todo), EMBED_BATCH_SIZE):
        batch = todo[i : i + EMBED_BATCH_SIZE]
        try:
            vectors = fn([r["text"] for r in batch])
        except Exception as e:
            _disable_embedding(f"embed batch failed: {e!r}")
            break
        if len(vectors) != len(batch):
            _disable_embedding("embed batch returned wrong count")
            break
        now = datetime.now(timezone.utc).isoformat()
        for r, vec in zip(batch, vectors):
            conn.execute(
                "INSERT OR REPLACE INTO embedding_cache "
                "(provider, model, hash, dims, vector, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (EMBEDDING_PROVIDER, model, r["hash"], len(vec),
                 _pack_vector(list(vec)), now),
            )
        conn.commit()
        embedded += len(batch)
    return embedded, len(rows) - embedded

@dataclass
class SyncStats:
    indexed: int = 0
    skipped: int = 0
    deleted: int = 0
    fts_mode: str = "none"
    embedded: int = 0
    embed_pending: int = 0


def _discover_files() -> list[tuple[str, str]]:
    base = agent_dir()
    out: list[tuple[str, str]] = []
    for root, source in SOURCE_ROOTS:
        abs_root = os.path.join(base, root)
        if os.path.isfile(abs_root):
            out.append((root, source))
            continue
        if not os.path.isdir(abs_root):
            continue
        for dirpath, dirnames, filenames in os.walk(abs_root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in sorted(filenames):
                if os.path.splitext(fn)[1].lower() not in INDEXED_EXTENSIONS:
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), base)
                out.append((rel.replace(os.sep, "/"), source))
    return out


def _fts_delete_rows(conn: sqlite3.Connection, path: str) -> None:
    rows = conn.execute(
        "SELECT id, text FROM chunks WHERE path = ?", (path,)
    ).fetchall()
    for r in rows:
        try:
            conn.execute(
                "INSERT INTO chunks_fts(chunks_fts, rowid, text) "
                "VALUES('delete', ?, ?)",
                (r["id"], r["text"]),
            )
        except sqlite3.OperationalError:
            return  # FTS 不可用 —— 没有需要删的索引行

def _reindex_file(
    conn: sqlite3.Connection,
    rel: str,
    source: str,
    text: str,
    sha: str,
    mtime: float,
    size: int,
) -> None:
    try:
        _fts_delete_rows(conn, rel)
        conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        for c in chunk_text(text):
            cur = conn.execute(
                "INSERT INTO chunks (path, source, start_line, end_line, hash, text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rel, source, c.start_line, c.end_line, c.hash, c.text),
            )
            try:
                conn.execute(
                    "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
                    (cur.lastrowid, c.text),
                )
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "INSERT INTO files (path, source, sha256, mtime, size, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "source=excluded.source, sha256=excluded.sha256, "
            "mtime=excluded.mtime, size=excluded.size, "
            "indexed_at=excluded.indexed_at",
            (rel, source, sha, mtime, size,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

def _delete_file(conn: sqlite3.Connection, path: str) -> None:
    try:
        _fts_delete_rows(conn, path)
        conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
        conn.execute("DELETE FROM files WHERE path = ?", (path,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

def sync(full: bool = False) -> SyncStats:
    conn = open_index_db()
    try:
        stats = SyncStats(fts_mode=ensure_schema(conn))
        _check_fingerprint(conn)

        disk = _discover_files()
        disk_paths = {p for p, _ in disk}
        existing = {
            r["path"]: r for r in conn.execute("SELECT * FROM files").fetchall()
        }

        base = agent_dir()
        for rel, source in disk:
            abs_p = os.path.join(base, rel.replace("/", os.sep))
            try:
                st = os.stat(abs_p)
            except OSError:
                continue
            row = existing.get(rel)
            if (
                row is not None
                and not full
                and row["size"] == st.st_size
                and abs(row["mtime"] - st.st_mtime) < 1e-6
            ):
                stats.skipped += 1
                continue
            try:
                with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                _log(f"could not read {rel}: {e!r}")
                continue
            sha = _sha256(text)
            if row is not None and row["sha256"] == sha:
                # 内容没变（mtime 抖动）→ 只刷新 stat。
                conn.execute(
                    "UPDATE files SET mtime = ?, size = ? WHERE path = ?",
                    (st.st_mtime, st.st_size, rel),
                )
                conn.commit()
                stats.skipped += 1
                continue
            _reindex_file(conn, rel, source, text, sha, st.st_mtime, st.st_size)
            stats.indexed += 1

        for path in set(existing) - disk_paths:
            _delete_file(conn, path)
            stats.deleted += 1

        if full:
            try:
                conn.execute(
                    "DELETE FROM embedding_cache "
                    "WHERE hash NOT IN (SELECT DISTINCT hash FROM chunks)"
                )
                conn.commit()
            except sqlite3.Error as e:
                _log(f"embedding cache prune failed: {e!r}")

        stats.embedded, stats.embed_pending = _backfill_embeddings(
            conn, EMBED_BUDGET_FULL if full else EMBED_BUDGET_FAST
        )

        return stats
    finally:
        conn.close()


def drop_index() -> None:
    """清空索引含 fingerprint 下次 sync 全量重建"""
    conn = open_index_db()
    try:
        ensure_schema(conn)
        _wipe_index(conn)
        conn.commit()
    finally:
        conn.close()


@dataclass
class SearchHit:
    path: str
    source: str
    start_line: int
    end_line: int
    score: float
    snippet: str

def _escape_like(term: str) -> str:
    return (
        term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )

def _make_snippet(text: str, terms: list[str], limit: int = SNIPPET_CHARS) -> str:
    lowered = text.lower()
    pos = -1
    for t in terms:
        p = lowered.find(t.lower())
        if p != -1 and (pos == -1 or p < pos):
            pos = p
    if pos <= limit // 2:
        start = 0
    else:
        start = pos - limit // 2
    snippet = text[start : start + limit]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + limit < len(text) else ""
    return prefix + " ".join(snippet.split()) + suffix


def search(
    query: str,
    k: int = DEFAULT_MAX_RESULTS,
    sources: Optional[list[str]] = None,
) -> list[SearchHit]:

    terms = [t for t in (query or "").split() if t]
    if not terms:
        return []
    k = max(1, int(k))

    conn = open_index_db()
    try:
        fts_mode = ensure_schema(conn)

        src_sql = ""
        src_params: list[str] = []
        if sources:
            src_sql = f" AND c.source IN ({','.join('?' * len(sources))})"
            src_params = list(sources)

        ordered: list[sqlite3.Row] = []
        seen: set[int] = set()

        fts_terms = [t for t in terms if len(t) >= 3]
        like_terms = [t for t in terms if len(t) < 3]

        if fts_terms and fts_mode != "none":
            match_expr = " OR ".join(
                '"' + t.replace('"', '""') + '"' for t in fts_terms
            )
            try:
                rows = conn.execute(
                    "SELECT c.id, c.path, c.source, c.start_line, c.end_line, c.text "
                    "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
                    f"WHERE chunks_fts MATCH ?{src_sql} "
                    "ORDER BY rank LIMIT ?",
                    (match_expr, *src_params, k * 3),
                ).fetchall()
            except sqlite3.OperationalError as e:
                _log(f"fts query failed, falling back to LIKE: {e}")
                rows = []
                like_terms = terms  # 全部词降级走 LIKE
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    ordered.append(r)
        elif fts_terms:
            like_terms = terms  # FTS 整体不可用

        for t in like_terms:
            rows = conn.execute(
                "SELECT c.id, c.path, c.source, c.start_line, c.end_line, c.text "
                "FROM chunks c "
                f"WHERE c.text LIKE ? ESCAPE '\\'{src_sql} LIMIT ?",
                (f"%{_escape_like(t)}%", *src_params, k * 3),
            ).fetchall()
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    ordered.append(r)

        vec_scores: dict[int, float] = {}
        rows_by_id: dict[int, sqlite3.Row] = {r["id"]: r for r in ordered}
        fn = _get_embed_fn()
        if fn is not None:
            model = _embedding_model()
            has_vectors = conn.execute(
                "SELECT 1 FROM embedding_cache "
                "WHERE provider = ? AND model = ? LIMIT 1",
                (EMBEDDING_PROVIDER, model),
            ).fetchone() is not None
            qvec: Optional[list[float]] = None
            if has_vectors:
                try:
                    qvec = fn([query])[0]
                except Exception as e:
                    _disable_embedding(f"query embed failed: {e!r}")
            if qvec:
                vrows = conn.execute(
                    "SELECT c.id, c.path, c.source, c.start_line, c.end_line, "
                    "c.text, e.vector AS vector "
                    "FROM chunks c JOIN embedding_cache e "
                    "ON e.hash = c.hash AND e.provider = ? AND e.model = ? "
                    f"WHERE 1=1{src_sql}",
                    (EMBEDDING_PROVIDER, model, *src_params),
                ).fetchall()
                if vrows:
                    sims = _cosine_scores(qvec, [r["vector"] for r in vrows])
                    paired = sorted(zip(vrows, sims), key=lambda p: -p[1])[: k * 3]
                    for r, s in paired:
                        if s <= 0.0:
                            continue
                        vec_scores[r["id"]] = max(0.0, min(1.0, float(s)))
                        rows_by_id.setdefault(r["id"], r)

        if not vec_scores:
            return [
                SearchHit(
                    path=r["path"],
                    source=r["source"],
                    start_line=r["start_line"],
                    end_line=r["end_line"],
                    score=1.0 / (1.0 + i),
                    snippet=_make_snippet(r["text"], terms),
                )
                for i, r in enumerate(ordered[:k])
            ]

        text_rank = {r["id"]: 1.0 / (1.0 + pos) for pos, r in enumerate(ordered)}
        mtimes = {
            r["path"]: r["mtime"]
            for r in conn.execute("SELECT path, mtime FROM files").fetchall()
        }
        now_s = time.time()
        scored: list[tuple[float, int]] = []
        for cid, row in rows_by_id.items():
            s = (
                VECTOR_WEIGHT * vec_scores.get(cid, 0.0)
                + TEXT_WEIGHT * text_rank.get(cid, 0.0)
            )
            if row["source"] in DECAY_SOURCES:
                age_days = max(
                    0.0, (now_s - mtimes.get(row["path"], now_s)) / 86400.0
                )
                s *= 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)
            if s < MIN_SCORE:
                continue
            scored.append((s, cid))
        scored.sort(key=lambda p: (-p[0], p[1]))

        hits: list[SearchHit] = []
        for s, cid in scored[:k]:
            r = rows_by_id[cid]
            hits.append(
                SearchHit(
                    path=r["path"],
                    source=r["source"],
                    start_line=r["start_line"],
                    end_line=r["end_line"],
                    score=round(s, 4),
                    snippet=_make_snippet(r["text"], terms),
                )
            )
        return hits
    finally:
        conn.close()
