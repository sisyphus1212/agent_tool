"""
hhist - Hermes SQLite history manager.

Design:
  * Hermes state.db is treated as the canonical history store.
  * hhist local metadata is stored in a sidecar DB: ~/.hermes/hhist.db.
  * archive / soft delete / tags / notes modify only hhist.db.
  * delete = soft delete in hhist.db only.
  * delete --hard = physical delete via official `hermes sessions delete`.
  * no --yes / --dry-run / --backup flow; commands execute directly.

Compatibility:
  hhist --list
  hhist -l
  hhist <session_id>
  hhist --search keyword

Primary commands:
  hhist list [--all|--active|--archived|--deleted]
  hhist show <session_id>
  hhist search <keyword> [--session <session_id>]
  hhist archive <session_id> [--group|--children]
  hhist restore <session_id> [--group|--children]
  hhist delete <session_id> [--group|--children]
  hhist delete <session_id> --hard [--group|--children]
  hhist dump <session_id>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
DEFAULT_DB = HERMES_HOME / "state.db"
DEFAULT_META_DB = Path(os.environ.get("HHIST_META_DB", str(HERMES_HOME / "hhist.db")))
CONTENT_JSON_PREFIX = "\x00json:"
MAX_SQL_VARS = 900


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw.isdigit():
        die(f"Invalid {name}: {raw}")
    return int(raw)


DEFAULT_LIMIT = env_int("HHIST_LIMIT", 50)
DEFAULT_SEARCH_LIMIT = env_int("HHIST_SEARCH_LIMIT", 100)


@dataclass
class Schema:
    columns: Dict[str, Set[str]]

    def has_table(self, table: str) -> bool:
        return table in self.columns

    def has(self, table: str, column: str) -> bool:
        return column in self.columns.get(table, set())


@dataclass
class MetaState:
    session_id: str
    archived: int = 0
    pinned: int = 0
    deleted_at: Optional[float] = None
    hard_deleted_at: Optional[float] = None
    archived_at: Optional[float] = None
    restored_at: Optional[float] = None
    note: str = ""
    tags: str = ""
    updated_at: float = 0.0


@dataclass
class Session:
    id: str
    parent_session_id: str
    root_id: str
    depth: int
    source: str
    started_at: float
    ended_at: Optional[float]
    end_reason: str
    last_active: float
    message_count: int
    title: str
    cwd: str = ""
    preview: str = ""
    archived: int = 0
    pinned: int = 0
    deleted_at: Optional[float] = None
    hard_deleted_at: Optional[float] = None
    note: str = ""
    tags: str = ""


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def now_ts() -> float:
    return datetime.now().timestamp()


def validate_sid(sid: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", sid):
        die(f"Invalid session id: {sid}")


def fmt_time(ts: Optional[float], short_mode: bool = False) -> str:
    if ts is None:
        return ""
    try:
        text = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
        return text[:16] if short_mode else text
    except Exception:
        return ""


def shorten(text: Optional[str], width: int = 22) -> str:
    text = text or ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def strip_title_suffix(title: str) -> str:
    return re.sub(r" #\d+$", "", title or "")


def chunks(values: Sequence[Any], size: int = MAX_SQL_VARS) -> Iterable[Sequence[Any]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def connect_hermes_db(path: Path, writable: bool = False) -> sqlite3.Connection:
    if not path.exists():
        die(f"Hermes DB not found: {path}")
    mode = "rw" if writable else "ro"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_meta_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    ensure_meta_schema(conn)
    return conn


def ensure_meta_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS hhist_session_state (
          session_id TEXT PRIMARY KEY,
          archived INTEGER NOT NULL DEFAULT 0,
          pinned INTEGER NOT NULL DEFAULT 0,
          deleted_at REAL,
          hard_deleted_at REAL,
          archived_at REAL,
          restored_at REAL,
          note TEXT,
          tags TEXT,
          updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hhist_operation_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts REAL NOT NULL,
          op TEXT NOT NULL,
          session_id TEXT NOT NULL,
          scope TEXT NOT NULL,
          detail TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_hhist_state_archived
          ON hhist_session_state(archived);

        CREATE INDEX IF NOT EXISTS idx_hhist_state_deleted
          ON hhist_session_state(deleted_at);

        CREATE INDEX IF NOT EXISTS idx_hhist_state_hard_deleted
          ON hhist_session_state(hard_deleted_at);

        CREATE INDEX IF NOT EXISTS idx_hhist_state_pinned
          ON hhist_session_state(pinned);

        CREATE INDEX IF NOT EXISTS idx_hhist_log_session
          ON hhist_operation_log(session_id, ts DESC);
        """
    )
    migrate_meta_schema(conn)
    conn.commit()


def sqlite_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({qident(table)})")}
    except sqlite3.DatabaseError:
        return set()


def migrate_meta_schema(conn: sqlite3.Connection) -> None:
    """Make older hhist.db files compatible without touching Hermes state.db."""
    state_cols = sqlite_columns(conn, "hhist_session_state")
    state_add = {
        "archived": "INTEGER NOT NULL DEFAULT 0",
        "pinned": "INTEGER NOT NULL DEFAULT 0",
        "deleted_at": "REAL",
        "hard_deleted_at": "REAL",
        "archived_at": "REAL",
        "restored_at": "REAL",
        "note": "TEXT",
        "tags": "TEXT",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    }
    for col, ddl in state_add.items():
        if col not in state_cols:
            conn.execute(f"ALTER TABLE hhist_session_state ADD COLUMN {qident(col)} {ddl}")

    log_cols = sqlite_columns(conn, "hhist_operation_log")
    log_add = {
        "ts": "REAL NOT NULL DEFAULT 0",
        "op": "TEXT NOT NULL DEFAULT ''",
        "session_id": "TEXT NOT NULL DEFAULT ''",
        "scope": "TEXT NOT NULL DEFAULT 'single'",
        "detail": "TEXT",
    }
    for col, ddl in log_add.items():
        if col not in log_cols:
            conn.execute(f"ALTER TABLE hhist_operation_log ADD COLUMN {qident(col)} {ddl}")


def load_schema(conn: sqlite3.Connection) -> Schema:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    columns: Dict[str, Set[str]] = {}
    for row in rows:
        table = row["name"]
        try:
            columns[table] = {
                c["name"] for c in conn.execute(f"PRAGMA table_info({qident(table)})")
            }
        except sqlite3.DatabaseError:
            columns[table] = set()
    return Schema(columns)


def optional_session_expr(schema: Schema, column: str, default_sql: str, alias: str = "s") -> str:
    if schema.has("sessions", column):
        return f"COALESCE({alias}.{column}, {default_sql}) AS {column}"
    return f"{default_sql} AS {column}"


def active_clause(schema: Schema, alias: str = "m") -> str:
    if schema.has("messages", "active"):
        return f"AND COALESCE({alias}.active, 1) = 1"
    return ""


def decode_content(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(CONTENT_JSON_PREFIX):
        try:
            return json.loads(value[len(CONTENT_JSON_PREFIX) :])
        except Exception:
            return value
    return value


def flatten_text(value: Any) -> str:
    value = decode_content(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def state_label(session: Session) -> str:
    if session.hard_deleted_at:
        return "HARDDEL"
    if session.deleted_at:
        return "DELETED"
    if session.archived:
        return "ARCHIVED"
    return "ACTIVE"


def load_meta_states(meta: sqlite3.Connection) -> Dict[str, MetaState]:
    rows = meta.execute("SELECT * FROM hhist_session_state").fetchall()
    out: Dict[str, MetaState] = {}
    for row in rows:
        out[row["session_id"]] = MetaState(
            session_id=row["session_id"],
            archived=int(row["archived"] or 0),
            pinned=int(row["pinned"] or 0),
            deleted_at=float(row["deleted_at"]) if row["deleted_at"] is not None else None,
            hard_deleted_at=float(row["hard_deleted_at"]) if row["hard_deleted_at"] is not None else None,
            archived_at=float(row["archived_at"]) if row["archived_at"] is not None else None,
            restored_at=float(row["restored_at"]) if row["restored_at"] is not None else None,
            note=row["note"] or "",
            tags=row["tags"] or "",
            updated_at=float(row["updated_at"] or 0),
        )
    return out


def apply_meta(session: Session, meta_state: Optional[MetaState]) -> None:
    if not meta_state:
        return
    session.archived = meta_state.archived
    session.pinned = meta_state.pinned
    session.deleted_at = meta_state.deleted_at
    session.hard_deleted_at = meta_state.hard_deleted_at
    session.note = meta_state.note
    session.tags = meta_state.tags


def fetch_sessions(
    conn: sqlite3.Connection,
    schema: Schema,
    meta: Optional[sqlite3.Connection] = None,
) -> Dict[str, Session]:
    if not schema.has_table("sessions"):
        die("sessions table not found")

    parent_expr = optional_session_expr(schema, "parent_session_id", "''")
    cwd_expr = optional_session_expr(schema, "cwd", "''")

    if schema.has_table("messages") and schema.has("messages", "session_id"):
        last_msg_cte = """
        WITH last_msg AS (
          SELECT session_id, MAX(timestamp) AS last_active
          FROM messages
          GROUP BY session_id
        ),
        preview_msg AS (
          SELECT session_id, content AS preview
          FROM messages
          WHERE id IN (
            SELECT MIN(id)
            FROM messages
            WHERE role = 'user'
              AND COALESCE(content, '') != ''
            GROUP BY session_id
          )
        )
        """
        joins = """
        LEFT JOIN last_msg l ON l.session_id = s.id
        LEFT JOIN preview_msg p ON p.session_id = s.id
        """
        last_active_expr = "COALESCE(l.last_active, s.started_at) AS last_active"
        preview_expr = "COALESCE(p.preview, '') AS preview"
    else:
        last_msg_cte = ""
        joins = ""
        last_active_expr = "s.started_at AS last_active"
        preview_expr = "'' AS preview"

    rows = conn.execute(
        f"""
        {last_msg_cte}
        SELECT
          s.id,
          {parent_expr},
          COALESCE(s.source, '') AS source,
          s.started_at,
          s.ended_at,
          COALESCE(s.end_reason, '') AS end_reason,
          {last_active_expr},
          COALESCE(s.message_count, 0) AS message_count,
          COALESCE(s.title, '') AS title,
          {cwd_expr},
          {preview_expr}
        FROM sessions s
        {joins}
        """
    ).fetchall()

    meta_states = load_meta_states(meta) if meta is not None else {}
    sessions: Dict[str, Session] = {}

    for row in rows:
        sid = row["id"]
        preview = normalize_space(flatten_text(row["preview"]))
        session = Session(
            id=sid,
            parent_session_id=row["parent_session_id"] or "",
            root_id="",
            depth=0,
            source=row["source"] or "",
            started_at=float(row["started_at"] or 0),
            ended_at=float(row["ended_at"]) if row["ended_at"] is not None else None,
            end_reason=row["end_reason"] or "",
            last_active=float(row["last_active"] or row["started_at"] or 0),
            message_count=int(row["message_count"] or 0),
            title=row["title"] or "",
            cwd=row["cwd"] or "",
            preview=preview,
        )
        apply_meta(session, meta_states.get(sid))
        sessions[sid] = session

    assign_roots(sessions)
    return sessions


def root_and_depth(sid: str, sessions: Dict[str, Session]) -> Tuple[str, int]:
    seen: Set[str] = set()
    cur = sid
    depth = 0
    while True:
        if cur in seen:
            return sid, 0
        seen.add(cur)
        session = sessions.get(cur)
        if not session:
            return sid, depth
        parent = session.parent_session_id
        if not parent or parent not in sessions:
            return cur, depth
        cur = parent
        depth += 1


def assign_roots(sessions: Dict[str, Session]) -> None:
    for sid, session in sessions.items():
        root, depth = root_and_depth(sid, sessions)
        session.root_id = root
        session.depth = depth


def group_items(root_id: str, sessions: Dict[str, Session]) -> List[Session]:
    children: Dict[str, List[Session]] = {}
    for session in sessions.values():
        if session.root_id != root_id:
            continue
        children.setdefault(session.parent_session_id, []).append(session)
    for item in children.values():
        item.sort(key=lambda x: (x.started_at, x.id))

    result: List[Session] = []

    def walk(session: Session) -> None:
        result.append(session)
        for child in children.get(session.id, []):
            walk(child)

    root = sessions.get(root_id)
    if root:
        walk(root)
    seen = {s.id for s in result}
    for session in sorted(
        [s for s in sessions.values() if s.root_id == root_id and s.id not in seen],
        key=lambda x: (x.started_at, x.id),
    ):
        result.append(session)
    return result


def child_tree_items(sid: str, sessions: Dict[str, Session]) -> List[Session]:
    if sid not in sessions:
        die(f"Session not found: {sid}")
    children: Dict[str, List[Session]] = {}
    for session in sessions.values():
        children.setdefault(session.parent_session_id, []).append(session)
    for item in children.values():
        item.sort(key=lambda x: (x.started_at, x.id))
    result: List[Session] = []

    def walk(session: Session) -> None:
        result.append(session)
        for child in children.get(session.id, []):
            walk(child)

    walk(sessions[sid])
    return result


def grouped_sessions(sessions: Dict[str, Session]) -> List[Tuple[Session, List[Session]]]:
    groups: List[Tuple[Session, List[Session]]] = []
    for root_id in sorted({s.root_id or s.id for s in sessions.values()}):
        items = group_items(root_id, sessions)
        if not items:
            continue
        root = sessions.get(root_id) or items[0]
        groups.append((root, items))
    groups.sort(key=lambda pair: (max(s.pinned for s in pair[1]), max(s.last_active for s in pair[1])), reverse=True)
    return groups


def group_title(root: Session, items: List[Session]) -> str:
    if root.title:
        return strip_title_suffix(root.title)
    for session in items:
        if session.title:
            return strip_title_suffix(session.title)
    return "(untitled)"


def resolve_sid(conn: sqlite3.Connection, sid_or_prefix: str) -> str:
    validate_sid(sid_or_prefix)
    row = conn.execute("SELECT id FROM sessions WHERE id = ?", (sid_or_prefix,)).fetchone()
    if row:
        return row["id"]
    rows = conn.execute(
        "SELECT id FROM sessions WHERE id LIKE ? ORDER BY started_at DESC LIMIT 2",
        (sid_or_prefix + "%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        die(f"Ambiguous session id prefix: {sid_or_prefix}")
    die(f"Session not found: {sid_or_prefix}")


def target_sessions(
    conn: sqlite3.Connection,
    schema: Schema,
    meta: sqlite3.Connection,
    sid_or_prefix: str,
    group: bool = False,
    children: bool = False,
) -> List[Session]:
    if group and children:
        die("--group and --children cannot be used together")
    sid = resolve_sid(conn, sid_or_prefix)
    sessions = fetch_sessions(conn, schema, meta)
    if sid not in sessions:
        die(f"Session not found: {sid}")
    if group:
        return group_items(sessions[sid].root_id or sid, sessions)
    if children:
        return child_tree_items(sid, sessions)
    return [sessions[sid]]


def session_group(conn: sqlite3.Connection, schema: Schema, meta: sqlite3.Connection, sid: str) -> List[Session]:
    return target_sessions(conn, schema, meta, sid, group=True)


def visible_by_state(session: Session, state_filter: str) -> bool:
    label = state_label(session)
    if state_filter == "all":
        return label != "HARDDEL"
    if state_filter == "active":
        return label == "ACTIVE"
    if state_filter == "archived":
        return label == "ARCHIVED"
    if state_filter == "deleted":
        return label == "DELETED"
    return True


def print_list(
    conn: sqlite3.Connection,
    schema: Schema,
    meta: sqlite3.Connection,
    limit: int,
    state_filter: str = "active",
    flat: bool = False,
) -> None:
    sessions = fetch_sessions(conn, schema, meta)
    groups: List[Tuple[Session, List[Session]]] = []

    for root, items in grouped_sessions(sessions):
        filtered = [s for s in items if visible_by_state(s, state_filter)]
        if filtered:
            groups.append((root, filtered))

    groups = groups[:limit]

    if flat:
        print(
            f"{'No':<4} {'State':<8} {'SessionID':<22} {'ParentSessionID':<22} "
            f"{'Msgs':>5} {'LastActive':<16} {'Title':<32} Preview"
        )
        print("-" * 150)
    else:
        print(
            f"{'No':<4} {'State':<8} {'RootSessionID':<22} {'SessionID':<22} "
            f"{'Msgs':>5} {'LastActive':<16} {'Title':<32} Preview"
        )
        print("-" * 150)

    idx = 1
    for root, items in groups:
        for item_idx, session in enumerate(items):
            root_col = root.id if item_idx == 0 and not flat else ""
            title = session.title or "—"
            preview = session.preview or ""
            if flat:
                print(
                    f"{idx:<4} {state_label(session):<8} {session.id:<22} "
                    f"{shorten(session.parent_session_id):<22} {session.message_count:>5} "
                    f"{fmt_time(session.last_active, short_mode=True):<16} "
                    f"{shorten(title, 32):<32} {shorten(preview, 80)}"
                )
            else:
                print(
                    f"{idx:<4} {state_label(session):<8} {root_col:<22} {session.id:<22} "
                    f"{session.message_count:>5} {fmt_time(session.last_active, short_mode=True):<16} "
                    f"{shorten(title, 32):<32} {shorten(preview, 80)}"
                )
            idx += 1


def fetch_messages(
    conn: sqlite3.Connection,
    schema: Schema,
    ids: Iterable[str],
    include_inactive: bool = False,
) -> List[sqlite3.Row]:
    ids = list(ids)
    if not ids or not schema.has_table("messages"):
        return []
    active = "" if include_inactive else active_clause(schema, "m")
    out: List[sqlite3.Row] = []
    for batch in chunks(ids):
        placeholders = ",".join("?" for _ in batch)
        out.extend(
            conn.execute(
                f"""
                SELECT m.*
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                WHERE m.session_id IN ({placeholders})
                  {active}
                ORDER BY s.started_at, m.id
                """,
                batch,
            ).fetchall()
        )
    return out


def message_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    if "content" in data:
        data["content"] = decode_content(data["content"])
    return data


def render_message(row: sqlite3.Row) -> str:
    data = message_to_dict(row)
    parts = [f"[{data.get('role', '')}]"]
    content = data.get("content")
    if content not in (None, ""):
        if isinstance(content, (list, dict)):
            parts.append(json.dumps(content, ensure_ascii=False, indent=2))
        else:
            parts.append(str(content))
    if data.get("tool_name"):
        parts.append(f"[tool] {data.get('tool_name')}")
    if data.get("tool_calls"):
        parts.append("[tool_calls]")
        parts.append(str(data.get("tool_calls")))
    if data.get("reasoning_content"):
        parts.append("[reasoning_content]")
        parts.append(str(data.get("reasoning_content")))
    parts.append("---")
    return "\n".join(parts)


def page(text: str) -> None:
    if shutil.which("less") and sys.stdout.isatty():
        proc = subprocess.Popen(["less", "-R"], stdin=subprocess.PIPE, text=True)
        try:
            proc.communicate(text)
        except BrokenPipeError:
            pass
    else:
        print(text)


def show_conversation(
    conn: sqlite3.Connection,
    schema: Schema,
    meta: sqlite3.Connection,
    sid: str,
    include_inactive: bool = False,
) -> None:
    items = session_group(conn, schema, meta, sid)
    ids = [s.id for s in items]
    messages = fetch_messages(conn, schema, ids, include_inactive=include_inactive)

    states = {s.id: state_label(s) for s in items}
    titles = {s.id: s.title for s in items}
    archived_count = sum(1 for s in items if state_label(s) == "ARCHIVED")
    deleted_count = sum(1 for s in items if state_label(s) == "DELETED")
    active_count = sum(1 for s in items if state_label(s) == "ACTIVE")

    lines = [
        f"Root      : {items[0].root_id or items[0].id}",
        f"Sessions  : {len(items)}  ACTIVE={active_count}  ARCHIVED={archived_count}  DELETED={deleted_count}",
        f"Msgs      : {sum(s.message_count for s in items)}",
        f"Title     : {group_title(items[0], items)}",
        f"SessionIDs: {', '.join(ids)}",
        "-" * 100,
    ]

    current_sid = None
    for message in messages:
        sid2 = message["session_id"]
        if sid2 != current_sid:
            current_sid = sid2
            lines.append("")
            lines.append("=" * 100)
            lines.append(f"SessionID: {sid2}")
            lines.append(f"State    : {states.get(sid2, 'ACTIVE')}")
            lines.append(f"Title    : {titles.get(sid2, '')}")
            lines.append("=" * 100)
        lines.append(render_message(message))

    page("\n".join(lines))


def conversation_payload(
    conn: sqlite3.Connection,
    schema: Schema,
    meta: sqlite3.Connection,
    sid: str,
    include_inactive: bool,
) -> Dict[str, Any]:
    items = session_group(conn, schema, meta, sid)
    ids = [s.id for s in items]
    messages = fetch_messages(conn, schema, ids, include_inactive=include_inactive)
    return {
        "type": "parent_session_group",
        "root_session_id": items[0].root_id or items[0].id,
        "session_ids": ids,
        "sessions": [asdict(s) for s in items],
        "messages": [message_to_dict(m) for m in messages],
    }


def dump_json(
    conn: sqlite3.Connection,
    schema: Schema,
    meta: sqlite3.Connection,
    sid: str,
    include_inactive: bool,
) -> None:
    print(json.dumps(conversation_payload(conn, schema, meta, sid, include_inactive), ensure_ascii=False, indent=2))



def print_schema(conn: sqlite3.Connection, meta: sqlite3.Connection) -> None:
    version = None
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        version = row["version"] if row else None
    except sqlite3.DatabaseError:
        version = None

    print(f"Hermes schema_version: {version if version is not None else '(none)'}")
    for db_name, db_conn in (("Hermes", conn), ("hhist", meta)):
        print(f"\n## {db_name} tables")
        tables = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for table_row in tables:
            table = table_row["name"]
            print(f"\n[{table}]")
            for row in db_conn.execute(f"PRAGMA table_info({qident(table)})"):
                pk = " PK" if row["pk"] else ""
                nn = " NOTNULL" if row["notnull"] else ""
                default = f" DEFAULT {row['dflt_value']}" if row["dflt_value"] is not None else ""
                print(f"  {row['name']}: {row['type']}{pk}{nn}{default}")


def make_snippet(text: str, keyword: str, width: int = 220) -> str:
    text = normalize_space(text)
    if not text:
        return ""
    pos = text.lower().find(keyword.lower())
    if pos < 0:
        return text[:width]
    start = max(0, pos - 80)
    end = min(len(text), pos + width - 80)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def has_fts(schema: Schema) -> bool:
    return schema.has_table("messages_fts")


def has_trigram(schema: Schema) -> bool:
    return schema.has_table("messages_fts_trigram")


def fts_search_rows(
    conn: sqlite3.Connection,
    schema: Schema,
    keyword: str,
    limit: int,
    scope_ids: Optional[List[str]],
    include_inactive: bool,
    use_trigram: bool = False,
) -> Optional[List[sqlite3.Row]]:
    table = "messages_fts_trigram" if use_trigram else "messages_fts"
    if not schema.has_table(table):
        return None

    active = "" if include_inactive else active_clause(schema, "m")
    scope_sql = ""
    scope_params: List[Any] = []
    if scope_ids:
        placeholders = ",".join("?" for _ in scope_ids)
        scope_sql = f"AND m.session_id IN ({placeholders})"
        scope_params.extend(scope_ids)

    try:
        return conn.execute(
            f"""
            WITH last_msg AS (
              SELECT session_id, MAX(timestamp) AS last_active
              FROM messages
              GROUP BY session_id
            )
            SELECT
              m.id AS message_id,
              m.session_id,
              m.timestamp,
              m.role,
              m.content,
              m.tool_name,
              m.tool_calls,
              COALESCE(s.title, '') AS title,
              COALESCE(s.message_count, 0) AS message_count,
              COALESCE(l.last_active, s.started_at) AS last_active,
              snippet({table}, 0, '[[', ']]', '...', 24) AS fts_snippet
            FROM {table}
            JOIN messages m ON m.id = {table}.rowid
            JOIN sessions s ON s.id = m.session_id
            LEFT JOIN last_msg l ON l.session_id = s.id
            WHERE {table} MATCH ?
              {active}
              {scope_sql}
            ORDER BY last_active DESC, m.session_id, m.id
            LIMIT ?
            """,
            (*scope_params, keyword, limit),
        ).fetchall()
    except sqlite3.DatabaseError:
        return None


def like_search_rows(
    conn: sqlite3.Connection,
    schema: Schema,
    keyword: str,
    limit: int,
    scope_ids: Optional[List[str]],
    include_inactive: bool,
) -> List[sqlite3.Row]:
    active = "" if include_inactive else active_clause(schema, "m")
    scope_sql = ""
    scope_params: List[Any] = []
    if scope_ids:
        placeholders = ",".join("?" for _ in scope_ids)
        scope_sql = f"AND m.session_id IN ({placeholders})"
        scope_params.extend(scope_ids)

    predicates: List[str] = []
    params: List[Any] = []
    like = f"%{keyword}%"
    for expr in (
        "COALESCE(m.content, '') LIKE ?" if schema.has("messages", "content") else "",
        "COALESCE(m.tool_name, '') LIKE ?" if schema.has("messages", "tool_name") else "",
        "COALESCE(m.tool_calls, '') LIKE ?" if schema.has("messages", "tool_calls") else "",
        "COALESCE(s.title, '') LIKE ?" if schema.has("sessions", "title") else "",
        "s.id LIKE ?",
        "COALESCE(s.parent_session_id, '') LIKE ?" if schema.has("sessions", "parent_session_id") else "",
    ):
        if expr:
            predicates.append(expr)
            params.append(like)

    return conn.execute(
        f"""
        WITH last_msg AS (
          SELECT session_id, MAX(timestamp) AS last_active
          FROM messages
          GROUP BY session_id
        )
        SELECT
          m.id AS message_id,
          m.session_id,
          m.timestamp,
          m.role,
          m.content,
          m.tool_name,
          m.tool_calls,
          COALESCE(s.title, '') AS title,
          COALESCE(s.message_count, 0) AS message_count,
          COALESCE(l.last_active, s.started_at) AS last_active,
          '' AS fts_snippet
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        LEFT JOIN last_msg l ON l.session_id = s.id
        WHERE 1=1
          {active}
          {scope_sql}
          AND ({' OR '.join(predicates)})
        ORDER BY last_active DESC, m.session_id, m.id
        LIMIT ?
        """,
        (*scope_params, *params, limit),
    ).fetchall()


def search_messages(
    conn: sqlite3.Connection,
    schema: Schema,
    meta: sqlite3.Connection,
    keyword: str,
    session_id: Optional[str],
    limit: int,
    include_inactive: bool = False,
    state_filter: str = "active",
    engine: str = "auto",
) -> None:
    if not keyword:
        die("Usage: hhist search [--session <session_id>] <keyword>")
    if not schema.has_table("messages"):
        die("messages table not found")

    sessions = fetch_sessions(conn, schema, meta)
    scope_ids: Optional[List[str]] = None
    if session_id:
        items = session_group(conn, schema, meta, session_id)
        scope_ids = [s.id for s in items]

    rows: Optional[List[sqlite3.Row]] = None
    used_engine = "LIKE"

    if engine in ("auto", "fts"):
        rows = fts_search_rows(conn, schema, keyword, limit, scope_ids, include_inactive, use_trigram=False)
        if rows is not None:
            used_engine = "FTS"

    if (rows is None or not rows) and engine in ("auto", "trigram"):
        rows = fts_search_rows(conn, schema, keyword, limit, scope_ids, include_inactive, use_trigram=True)
        if rows is not None and rows:
            used_engine = "TRIGRAM"

    if rows is None or (not rows and engine == "auto") or engine == "like":
        rows = like_search_rows(conn, schema, keyword, limit, scope_ids, include_inactive)
        used_engine = "LIKE"

    filtered = []
    for row in rows or []:
        sid = row["session_id"]
        session = sessions.get(sid)
        if not session:
            continue
        if not visible_by_state(session, state_filter):
            continue
        filtered.append(row)

    if not filtered:
        if session_id:
            print(f"No matched message in session group {session_id!r} for: {keyword}")
        else:
            print(f"No matched message for: {keyword}")
        return

    print(f"SearchEngine: {used_engine}")
    print(
        f"{'State':<8} {'RootSessionID':<22} {'SessionID':<22} "
        f"{'MsgID':>6} {'Time':<16} {'Role':<10} Snippet"
    )
    print("-" * 150)

    for row in filtered:
        sid = row["session_id"]
        session = sessions[sid]
        root = session.root_id or sid
        fts_snip = row["fts_snippet"] if "fts_snippet" in row.keys() else ""

        if fts_snip:
            snippet = normalize_space(fts_snip)
        else:
            text_candidates = [
                flatten_text(row["content"]),
                flatten_text(row["tool_calls"]),
                flatten_text(row["tool_name"]),
                flatten_text(row["title"]),
            ]
            snippet = ""
            for candidate in text_candidates:
                if keyword.lower() in candidate.lower():
                    snippet = make_snippet(candidate, keyword)
                    break
            if not snippet:
                snippet = make_snippet(" ".join(text_candidates), keyword)

        print(
            f"{state_label(session):<8} "
            f"{shorten(root):<22} "
            f"{shorten(sid):<22} "
            f"{row['message_id']:>6} "
            f"{fmt_time(row['timestamp'], short_mode=True):<16} "
            f"{str(row['role'] or ''):<10} "
            f"{snippet}"
        )


def print_target_summary(action: str, items: List[Session]) -> None:
    counts: Dict[str, int] = {}
    for s in items:
        counts[state_label(s)] = counts.get(state_label(s), 0) + 1
    messages = sum(s.message_count for s in items)
    print(f"{action}: sessions={len(items)} messages={messages} states={counts}")
    print(
        f"{'State':<8} {'SessionID':<22} {'ParentSessionID':<22} "
        f"{'Msgs':>5} {'LastActive':<16} Title"
    )
    print("-" * 115)
    for s in items:
        print(
            f"{state_label(s):<8} {s.id:<22} {shorten(s.parent_session_id):<22} "
            f"{s.message_count:>5} {fmt_time(s.last_active, short_mode=True):<16} {s.title}"
        )


def log_operation(
    meta: sqlite3.Connection,
    op: str,
    session_id: str,
    scope: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    with meta:
        meta.execute(
            """
            INSERT INTO hhist_operation_log(ts, op, session_id, scope, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (now_ts(), op, session_id, scope, json.dumps(detail or {}, ensure_ascii=False)),
        )


def upsert_archive(meta: sqlite3.Connection, items: List[Session]) -> int:
    ts = now_ts()
    with meta:
        for s in items:
            meta.execute(
                """
                INSERT INTO hhist_session_state(
                  session_id, archived, pinned, deleted_at, archived_at, restored_at,
                  note, tags, updated_at
                )
                VALUES (?, 1, 0, NULL, ?, NULL, '', '', ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  archived = 1,
                  deleted_at = NULL,
                  archived_at = excluded.archived_at,
                  updated_at = excluded.updated_at
                """,
                (s.id, ts, ts),
            )
    return len(items)


def upsert_restore(meta: sqlite3.Connection, items: List[Session]) -> int:
    ts = now_ts()
    with meta:
        for s in items:
            meta.execute(
                """
                INSERT INTO hhist_session_state(
                  session_id, archived, pinned, deleted_at, hard_deleted_at, restored_at,
                  note, tags, updated_at
                )
                VALUES (?, 0, 0, NULL, NULL, ?, '', '', ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  archived = 0,
                  deleted_at = NULL,
                  hard_deleted_at = NULL,
                  restored_at = excluded.restored_at,
                  updated_at = excluded.updated_at
                """,
                (s.id, ts, ts),
            )
    return len(items)


def upsert_soft_delete(meta: sqlite3.Connection, items: List[Session]) -> int:
    ts = now_ts()
    with meta:
        for s in items:
            meta.execute(
                """
                INSERT INTO hhist_session_state(
                  session_id, archived, pinned, deleted_at, archived_at, restored_at,
                  note, tags, updated_at
                )
                VALUES (?, 1, 0, ?, ?, NULL, '', '', ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  archived = 1,
                  deleted_at = excluded.deleted_at,
                  archived_at = excluded.archived_at,
                  updated_at = excluded.updated_at
                """,
                (s.id, ts, ts, ts),
            )
    return len(items)


def set_note(meta: sqlite3.Connection, sid: str, note: str) -> None:
    ts = now_ts()
    with meta:
        meta.execute(
            """
            INSERT INTO hhist_session_state(session_id, note, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
              note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (sid, note, ts),
        )


def set_tags(meta: sqlite3.Connection, sid: str, tags: str) -> None:
    ts = now_ts()
    with meta:
        meta.execute(
            """
            INSERT INTO hhist_session_state(session_id, tags, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
              tags = excluded.tags,
              updated_at = excluded.updated_at
            """,
            (sid, tags, ts),
        )


def mark_hard_deleted(meta: sqlite3.Connection, items: List[Session]) -> None:
    ts = now_ts()
    with meta:
        for s in items:
            meta.execute(
                """
                INSERT INTO hhist_session_state(
                  session_id, archived, deleted_at, hard_deleted_at, updated_at
                )
                VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  archived = 1,
                  deleted_at = excluded.deleted_at,
                  hard_deleted_at = excluded.hard_deleted_at,
                  updated_at = excluded.updated_at
                """,
                (s.id, ts, ts, ts),
            )


def call_hermes_delete(session_id: str) -> None:
    hermes = shutil.which("hermes")
    if not hermes:
        die("Cannot find `hermes` in PATH. Hard delete aborted.")

    cmd = [hermes, "sessions", "delete", session_id]
    proc = subprocess.run(cmd, text=True)
    if proc.returncode != 0:
        die(f"Hermes delete failed for {session_id}")


def run_archive(args: argparse.Namespace, conn: sqlite3.Connection, schema: Schema, meta: sqlite3.Connection) -> None:
    items = target_sessions(conn, schema, meta, args.session_id, args.group, args.children)
    print_target_summary("ARCHIVE", items)
    changed = upsert_archive(meta, items)
    for s in items:
        log_operation(meta, "archive", s.id, scope_name(args))
    print(f"Archived sessions: {changed}")


def run_restore(args: argparse.Namespace, conn: sqlite3.Connection, schema: Schema, meta: sqlite3.Connection) -> None:
    items = target_sessions(conn, schema, meta, args.session_id, args.group, args.children)
    print_target_summary("RESTORE", items)
    changed = upsert_restore(meta, items)
    for s in items:
        log_operation(meta, "restore", s.id, scope_name(args))
    print(f"Restored sessions: {changed}")


def scope_name(args: argparse.Namespace) -> str:
    if getattr(args, "group", False):
        return "group"
    if getattr(args, "children", False):
        return "children"
    return "single"


def run_delete(args: argparse.Namespace, conn: sqlite3.Connection, schema: Schema, meta: sqlite3.Connection) -> None:
    items = target_sessions(conn, schema, meta, args.session_id, args.group, args.children)
    action = "HARD_DELETE" if args.hard else "SOFT_DELETE"
    print_target_summary(action, items)

    if args.hard:
        for s in sorted(items, key=lambda x: x.depth, reverse=True):
            call_hermes_delete(s.id)
        mark_hard_deleted(meta, items)
        for s in items:
            log_operation(meta, "hard_delete", s.id, scope_name(args))
        print(f"Hard deleted sessions via hermes CLI: {len(items)}")
        return

    changed = upsert_soft_delete(meta, items)
    for s in items:
        log_operation(meta, "soft_delete", s.id, scope_name(args))
    print(f"Soft-deleted sessions: {changed}")


def print_graveyard(meta: sqlite3.Connection, limit: int) -> None:
    rows = meta.execute(
        """
        SELECT *
        FROM hhist_session_state
        WHERE hard_deleted_at IS NOT NULL
        ORDER BY hard_deleted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    print(f"{'SessionID':<22} {'HardDeletedAt':<19}")
    print("-" * 50)
    for row in rows:
        print(
            f"{row['session_id']:<22} "
            f"{fmt_time(row['hard_deleted_at']):<19}"
        )


def print_ops(meta: sqlite3.Connection, session_id: Optional[str], limit: int) -> None:
    if session_id:
        rows = meta.execute(
            """
            SELECT * FROM hhist_operation_log
            WHERE session_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    else:
        rows = meta.execute(
            "SELECT * FROM hhist_operation_log ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    print(f"{'Time':<19} {'Op':<12} {'SessionID':<22} {'Scope':<8} Detail")
    print("-" * 120)
    for row in rows:
        print(
            f"{fmt_time(row['ts']):<19} "
            f"{row['op']:<12} "
            f"{row['session_id']:<22} "
            f"{row['scope']:<8} "
            f"{row['detail'] or ''}"
        )


def add_state_filter_args(parser: argparse.ArgumentParser, default: str = "active") -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", dest="state_filter", action="store_const", const="all", help="include active, archived and soft-deleted sessions")
    group.add_argument("--active", dest="state_filter", action="store_const", const="active", help="include only active sessions")
    group.add_argument("--archived", dest="state_filter", action="store_const", const="archived", help="include only archived sessions")
    group.add_argument("--deleted", dest="state_filter", action="store_const", const="deleted", help="include only soft-deleted sessions")
    parser.set_defaults(state_filter=default)


def add_scope_args(parser: argparse.ArgumentParser) -> None:
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--group", action="store_true", help="operate on all sessions in the same root conversation group")
    scope.add_argument("--children", action="store_true", help="operate on this session and its child sessions")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hhist",
        description="Hermes SQLite history viewer with sidecar archive/delete/search management.",
    )

    parser.add_argument("--db", default=str(DEFAULT_DB), help="path to Hermes state.db")
    parser.add_argument("--meta-db", default=str(DEFAULT_META_DB), help="path to hhist sidecar DB")
    parser.add_argument("--schema", action="store_true", help="print detected Hermes and hhist schemas")
    parser.add_argument("--list", action="store_true", help="compat: list sessions")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="compat list limit")

    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="list sessions grouped by root conversation")
    p_list.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p_list.add_argument("--flat", action="store_true", help="show flat session list instead of grouped root list")
    add_state_filter_args(p_list, default="active")

    p_show = sub.add_parser("show", help="show one full conversation group by session id")
    p_show.add_argument("session_id")
    p_show.add_argument("--include-inactive", action="store_true", help="include inactive messages if DB has messages.active")

    p_search = sub.add_parser("search", help="search messages globally or inside one session group")
    p_search.add_argument("--session", "-S", dest="session_id", help="restrict search to the parent-chain group containing this session id")
    p_search.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    p_search.add_argument("--include-inactive", action="store_true", help="include inactive messages if DB has messages.active")
    p_search.add_argument("--engine", choices=["auto", "fts", "trigram", "like"], default="auto")
    add_state_filter_args(p_search, default="active")
    p_search.add_argument("keyword", nargs="+")

    p_dump = sub.add_parser("dump", help="dump one full conversation group as JSON")
    p_dump.add_argument("session_id")
    p_dump.add_argument("--include-inactive", action="store_true", help="include inactive messages if DB has messages.active")

    p_archive = sub.add_parser("archive", help="archive locally: hidden from default list/search, still searchable with --archived")
    p_archive.add_argument("session_id")
    add_scope_args(p_archive)
    p_restore = sub.add_parser("restore", help="restore archived or soft-deleted sessions in hhist sidecar DB")
    p_restore.add_argument("session_id")
    add_scope_args(p_restore)
    p_delete = sub.add_parser("delete", help="soft delete by default; --hard physically deletes via official hermes CLI")
    p_delete.add_argument("session_id")
    add_scope_args(p_delete)
    p_delete.add_argument("--hard", action="store_true", help="physically delete via official hermes CLI")
    p_note = sub.add_parser("note", help="set local note for a session")
    p_note.add_argument("session_id")
    p_note.add_argument("note", nargs="+")

    p_tags = sub.add_parser("tags", help="set local comma-separated tags for a session")
    p_tags.add_argument("session_id")
    p_tags.add_argument("tags")

    p_grave = sub.add_parser("graveyard", help="list hard-delete tombstones")
    p_grave.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    p_ops = sub.add_parser("ops", help="show hhist operation log")
    p_ops.add_argument("--session", dest="session_id")
    p_ops.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    parser.add_argument("legacy_session_id", nargs="?", help="compat: hhist <session_id> is same as hhist show <session_id>")
    return parser


def normalize_legacy_args(argv: List[str]) -> List[str]:
    if argv == ["-l"]:
        return ["--list"]
    if argv and argv[0] in ("--search", "-s"):
        return ["search"] + argv[1:]
    return argv


def main() -> None:
    argv = normalize_legacy_args(sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)

    hermes_db = Path(args.db)
    meta_db = Path(args.meta_db)

    conn = connect_hermes_db(hermes_db, writable=False)
    meta = connect_meta_db(meta_db)
    schema = load_schema(conn)

    if args.schema:
        print_schema(conn, meta)
        return

    if args.cmd == "list" or args.list:
        print_list(
            conn,
            schema,
            meta,
            limit=getattr(args, "limit", DEFAULT_LIMIT),
            state_filter=getattr(args, "state_filter", "active"),
            flat=getattr(args, "flat", False),
        )
        return

    if args.cmd == "show":
        show_conversation(conn, schema, meta, args.session_id, include_inactive=args.include_inactive)
        return

    if args.cmd == "search":
        search_messages(
            conn,
            schema,
            meta,
            " ".join(args.keyword),
            session_id=args.session_id,
            limit=args.limit,
            include_inactive=args.include_inactive,
            state_filter=args.state_filter,
            engine=args.engine,
        )
        return

    if args.cmd == "dump":
        dump_json(conn, schema, meta, args.session_id, include_inactive=args.include_inactive)
        return

    if args.cmd == "archive":
        run_archive(args, conn, schema, meta)
        return

    if args.cmd == "restore":
        run_restore(args, conn, schema, meta)
        return

    if args.cmd == "delete":
        run_delete(args, conn, schema, meta)
        return

    if args.cmd == "note":
        sid = resolve_sid(conn, args.session_id)
        set_note(meta, sid, " ".join(args.note))
        log_operation(meta, "note", sid, "single", {"note": " ".join(args.note)})
        print(f"Note updated: {sid}")
        return

    if args.cmd == "tags":
        sid = resolve_sid(conn, args.session_id)
        set_tags(meta, sid, args.tags)
        log_operation(meta, "tags", sid, "single", False, {"tags": args.tags})
        print(f"Tags updated: {sid}")
        return

    if args.cmd == "graveyard":
        print_graveyard(meta, args.limit)
        return

    if args.cmd == "ops":
        print_ops(meta, args.session_id, args.limit)
        return

    if args.legacy_session_id:
        show_conversation(conn, schema, meta, args.legacy_session_id)
        return

    print_list(conn, schema, meta, args.limit, state_filter="active")


if __name__ == "__main__":
    main()

