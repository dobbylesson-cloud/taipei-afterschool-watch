"""SQLite 儲存層：公告去重、首次發現/最後更新時間、執行紀錄。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    url           TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    school        TEXT NOT NULL,
    title         TEXT NOT NULL,
    published     TEXT,
    content_text  TEXT,
    content_hash  TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    notice_url  TEXT NOT NULL,
    url         TEXT NOT NULL,
    filename    TEXT,
    sha256      TEXT,
    text        TEXT,
    ok          INTEGER DEFAULT 0,
    error       TEXT,
    first_seen  TEXT,
    PRIMARY KEY (notice_url, url)
);
CREATE TABLE IF NOT EXISTS extractions (
    notice_url   TEXT PRIMARY KEY,
    start_date   TEXT,
    end_date     TEXT,
    evidence     TEXT,
    confidence   REAL DEFAULT 0,
    rule         TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    new_count      INTEGER DEFAULT 0,
    updated_count  INTEGER DEFAULT 0,
    total_count    INTEGER DEFAULT 0,
    ok_sources     INTEGER DEFAULT 0,
    failed_sources INTEGER DEFAULT 0,
    note           TEXT
);
CREATE TABLE IF NOT EXISTS source_runs (
    run_id     INTEGER NOT NULL,
    source_id  TEXT NOT NULL,
    name       TEXT,
    status     TEXT,
    found      INTEGER DEFAULT 0,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ext_end ON extractions(end_date);
"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str):
        self.db_path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- 執行紀錄 ----------
    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now_iso(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, *, new_count: int, updated_count: int, total_count: int,
                   ok_sources: int, failed_sources: int, note: str = ""):
        self.conn.execute(
            "UPDATE runs SET finished_at=?, new_count=?, updated_count=?, total_count=?,"
            " ok_sources=?, failed_sources=?, note=? WHERE id=?",
            (now_iso(), new_count, updated_count, total_count, ok_sources, failed_sources, note, run_id),
        )
        self.conn.commit()

    def log_source(self, run_id: int, source_id: str, name: str, status: str, found: int = 0, error: str = ""):
        self.conn.execute(
            "INSERT INTO source_runs (run_id, source_id, name, status, found, error) VALUES (?,?,?,?,?,?)",
            (run_id, source_id, name, status, found, error),
        )
        self.conn.commit()

    # ---------- 公告 ----------
    def upsert_notice(self, notice, content_hash: str) -> tuple[bool, bool]:
        """回傳 (是否為新公告, 內容是否更新)。"""
        row = self.conn.execute(
            "SELECT content_hash FROM notices WHERE url=?", (notice.url,)
        ).fetchone()
        ts = now_iso()
        pub = notice.published.isoformat() if isinstance(notice.published, date) else (notice.published or "")
        if row is None:
            self.conn.execute(
                "INSERT INTO notices (url, source_id, school, title, published, content_text,"
                " content_hash, first_seen, last_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (notice.url, notice.source_id, notice.school, notice.title, pub,
                 notice.content_text[:200000], content_hash, ts, ts, ts),
            )
            self.conn.commit()
            return True, False
        changed = row["content_hash"] != content_hash
        self.conn.execute(
            "UPDATE notices SET title=?, published=?, content_text=?, content_hash=?, last_seen=?,"
            " updated_at=COALESCE(updated_at, first_seen) WHERE url=?",
            (notice.title, pub, notice.content_text[:200000], content_hash, ts, notice.url),
        )
        if changed:
            self.conn.execute("UPDATE notices SET updated_at=? WHERE url=?", (ts, notice.url))
        self.conn.commit()
        return False, changed

    def upsert_attachment(self, notice_url: str, att) -> None:
        self.conn.execute(
            "INSERT INTO attachments (notice_url, url, filename, sha256, text, ok, error, first_seen)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(notice_url, url) DO UPDATE SET filename=excluded.filename, sha256=excluded.sha256,"
            " text=excluded.text, ok=excluded.ok, error=excluded.error",
            (notice_url, att.url, att.filename, att.sha256, (att.text or "")[:200000],
             1 if att.ok else 0, att.error, now_iso()),
        )
        self.conn.commit()

    def save_extraction(self, notice_url: str, res) -> None:
        self.conn.execute(
            "INSERT INTO extractions (notice_url, start_date, end_date, evidence, confidence, rule, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(notice_url) DO UPDATE SET"
            " start_date=excluded.start_date, end_date=excluded.end_date, evidence=excluded.evidence,"
            " confidence=excluded.confidence, rule=excluded.rule, updated_at=excluded.updated_at",
            (
                notice_url,
                res.start.isoformat() if res.start else None,
                res.end.isoformat() if res.end else None,
                res.evidence,
                float(res.confidence or 0),
                res.rule,
                now_iso(),
            ),
        )
        self.conn.commit()

    def count_notices(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM notices").fetchone()["c"]

    def known_attachment(self, notice_url: str, url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM attachments WHERE notice_url=? AND url=? AND ok=1", (notice_url, url)
        ).fetchone()
        return row is not None

    def notices_for_reextract(self) -> list[dict]:
        """重新抽取用：取出所有公告的標題/內文/附件文字。"""
        out = []
        for r in self.conn.execute(
            "SELECT url, title, published, content_text FROM notices"
        ):
            att_text = "\n".join(
                a["text"] or ""
                for a in self.conn.execute(
                    "SELECT text FROM attachments WHERE notice_url=? AND ok=1", (r["url"],)
                )
            )
            out.append(
                {
                    "url": r["url"],
                    "title": r["title"],
                    "published": r["published"],
                    "text": "\n".join(x for x in (r["title"], r["content_text"], att_text) if x),
                }
            )
        return out

    def rows_for_render(self) -> list[dict]:
        q = """
        SELECT n.url, n.source_id, n.school, n.title, n.published,
               n.first_seen, n.last_seen,
               e.start_date, e.end_date, e.evidence, e.confidence, e.rule
        FROM notices n LEFT JOIN extractions e ON e.notice_url = n.url
        ORDER BY COALESCE(e.end_date, '9999-12-31') ASC, n.published DESC
        """
        out = []
        for r in self.conn.execute(q):
            atts = self.conn.execute(
                "SELECT url, filename, ok FROM attachments WHERE notice_url=? ORDER BY filename", (r["url"],)
            ).fetchall()
            d = dict(r)
            d["attachments"] = [dict(a) for a in atts]
            out.append(d)
        return out

    def last_run(self) -> dict | None:
        r = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return None
        d = dict(r)
        d["sources"] = [
            dict(x) for x in self.conn.execute(
                "SELECT source_id, name, status, found, error FROM source_runs WHERE run_id=? ORDER BY status, name",
                (r["id"],),
            )
        ]
        return d

    def close(self):
        self.conn.close()

    # ---------- NSS 設定快取 ----------
    def get_meta(self, key: str) -> dict | None:
        path = os.path.join(os.path.dirname(self.db_path), "meta.json")
        if not os.path.exists(path):
            return None
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            return None
        return data.get(key)

    def set_meta(self, key: str, value: dict) -> None:
        path = os.path.join(os.path.dirname(self.db_path), "meta.json")
        data = {}
        if os.path.exists(path):
            try:
                data = json.load(open(path, encoding="utf-8"))
            except Exception:
                data = {}
        data[key] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
