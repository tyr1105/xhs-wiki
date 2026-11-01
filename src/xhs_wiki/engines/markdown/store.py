"""轻量引擎的状态存储：KB_ROOT/.xhs-wiki/engine.sqlite3 的读写。

这个文件在整体架构（docs/architecture.md §6.1）里属于引擎的"出错可以恢复"部分：
整理过程的每一步中间结果（笔记卡片、主题、主题成员、待重写的主题页）都先落库，
这样任何一步中断后再次执行，都能从断点继续，不会重复调用已经完成的模型步骤。

表结构：
- note_cards：每条收藏一张的笔记卡片（ingest 的产物），含来源页文件名；
- topics / topic_members：主题和成员关系（distill 第一步的产物）；
- pending_pages：等待重写主题页的主题队列（distill 第二步的断点记录）。

注意：数据库文件里没有任何签名链接或密钥，只有整理产物本身。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 建表语句。schema_version 记在 meta 表里，将来结构变化时做迁移。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS note_cards (
  note_id TEXT PRIMARY KEY,
  card_json TEXT,
  status TEXT NOT NULL,             -- ok | deferred
  error TEXT,
  insufficient_reason TEXT,
  title TEXT,
  url TEXT,
  author TEXT,
  published_at TEXT,
  list_position INTEGER,
  source_file TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  norm_name TEXT NOT NULL UNIQUE,   -- 规范化后的名称，用来判重名
  description TEXT,
  category TEXT,
  created_at TEXT,
  updated_at TEXT,
  page_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS topic_members (
  topic_id INTEGER NOT NULL,
  note_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',
  assigned_at TEXT,
  PRIMARY KEY (topic_id, note_id)
);
CREATE TABLE IF NOT EXISTS pending_pages (
  topic_id INTEGER PRIMARY KEY,
  queued_at TEXT
);
"""


def norm_topic_name(name: str) -> str:
    """主题名规范化：去掉空白和常见中英文标点后转小写。

    归类时模型新建的主题可能和已有主题只差一个标点或空格，
    用这个函数把名称压成统一形式再做相等比较，避免重复建主题。
    """

    kept = []
    for ch in str(name):
        # 只保留文字类字符（中文、字母、数字），其余一律丢弃
        if ch.isalnum():
            kept.append(ch.lower())
    return "".join(kept)


class Store:
    """engine.sqlite3 的封装。所有方法都在事务里完成，多进程访问靠 SQLite 锁。"""

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 引擎目录是私有状态，按规范收紧权限（目录 0700）
        try:
            os.chmod(db_path.parent, 0o700)
        except OSError:
            pass  # 权限修改失败不阻塞功能（比如某些文件系统不支持）
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        # 私有数据库文件权限 0600
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        with self.conn:
            self.conn.executescript(_SCHEMA)
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
            )

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # 笔记卡片
    # ------------------------------------------------------------------

    def has_card(self, note_id: str) -> bool:
        """这条收藏是否已经生成过卡片（成功或暂缓都算）。"""

        row = self.conn.execute(
            "SELECT 1 FROM note_cards WHERE note_id = ?", (note_id,)
        ).fetchone()
        return row is not None

    def get_card(self, note_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM note_cards WHERE note_id = ?", (note_id,)
        ).fetchone()

    def save_card(self, **fields) -> None:
        """插入或覆盖一张卡片。字段名和 note_cards 列名一致。"""

        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.conn:
            self.conn.execute(
                f"INSERT OR REPLACE INTO note_cards ({cols}) VALUES ({marks})",
                tuple(fields.values()),
            )

    def cards_pending_assignment(self, note_ids: list[str] | None = None) -> list[sqlite3.Row]:
        """待归类的卡片：卡片状态 ok（不是暂缓、不是失败），且还没进任何主题。

        note_ids 不为空时只在这些 id 里挑（runner 按 batch 推进时用），
        为空时返回全部待归类卡片（断点恢复时用）。
        """

        sql = (
            "SELECT c.* FROM note_cards c "
            "WHERE c.status = 'ok' AND c.note_id NOT IN "
            "  (SELECT note_id FROM topic_members)"
        )
        params: tuple = ()
        if note_ids is not None:
            if not note_ids:
                return []
            marks = ", ".join("?" for _ in note_ids)
            sql += f" AND c.note_id IN ({marks})"
            params = tuple(note_ids)
        sql += " ORDER BY c.list_position, c.note_id"
        return self.conn.execute(sql, params).fetchall()

    def deferred_cards(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM note_cards WHERE status = 'deferred' "
            "ORDER BY list_position, note_id"
        ).fetchall()

    def recent_cards(self, limit: int = 20) -> list[sqlite3.Row]:
        """最近的收藏卡片，list_position 越小越新（收藏列表最前面的最新）。"""

        return self.conn.execute(
            "SELECT * FROM note_cards ORDER BY list_position, created_at DESC, note_id "
            "LIMIT ?",
            (limit,),
        ).fetchall()

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------

    def list_topics(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM topics ORDER BY id").fetchall()

    def topic_by_norm(self, norm_name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM topics WHERE norm_name = ?", (norm_name,)
        ).fetchone()

    def create_topic(
        self,
        name: str,
        description: str,
        category: str,
        now: str,
    ) -> int:
        """新建主题，返回 id。规范化名称冲突由调用方先查 topic_by_norm 避免。"""

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO topics(name, norm_name, description, category, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, norm_topic_name(name), description, category, now, now),
            )
        return int(cur.lastrowid)

    def add_member(self, topic_id: int, note_id: str, now: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO topic_members(topic_id, note_id, role, assigned_at)"
                " VALUES (?, ?, 'member', ?)",
                (topic_id, note_id, now),
            )

    def members_with_cards(self, topic_id: int) -> list[sqlite3.Row]:
        """主题的全部成员，连同它们的卡片和来源页文件名。list_position 升序（新→旧）。"""

        return self.conn.execute(
            "SELECT c.* FROM topic_members m JOIN note_cards c ON c.note_id = m.note_id "
            "WHERE m.topic_id = ? ORDER BY c.list_position, c.note_id",
            (topic_id,),
        ).fetchall()

    def member_note_ids(self, topic_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT note_id FROM topic_members WHERE topic_id = ?", (topic_id,)
        ).fetchall()
        return {r["note_id"] for r in rows}

    def note_topic_ids(self, note_ids: list[str] | None = None) -> dict[str, set[int]]:
        """读取已成功入库笔记的主题关系，供整理恢复时核对已有成员。

        返回每条笔记的全部主题编号；正常归类只有一个主题，用集合保留已有
        数据可能含多个主题的情况，必须等所有待重写主题完成才能确认成功。
        """

        sql = ("SELECT m.note_id, m.topic_id FROM topic_members m "
               "JOIN note_cards c ON c.note_id = m.note_id WHERE c.status = 'ok'")
        params: tuple = ()
        if note_ids is not None:
            if not note_ids:
                return {}
            sql += " AND m.note_id IN (" + ", ".join("?" for _ in note_ids) + ")"
            params = tuple(note_ids)
        result: dict[str, set[int]] = {}
        for row in self.conn.execute(sql, params):
            result.setdefault(row["note_id"], set()).add(int(row["topic_id"]))
        return result

    def topic_member_counts(self) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT topic_id, COUNT(*) AS n FROM topic_members GROUP BY topic_id"
        ).fetchall()
        return {r["topic_id"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    # 待重写的主题页（断点队列）
    # ------------------------------------------------------------------

    def queue_page(self, topic_id: int, now: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO pending_pages(topic_id, queued_at) VALUES (?, ?)",
                (topic_id, now),
            )

    def pending_page_ids(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT topic_id FROM pending_pages ORDER BY queued_at, topic_id"
        ).fetchall()
        return [r["topic_id"] for r in rows]

    def finish_page(self, topic_id: int, page_sha256: str | None = None) -> None:
        """一页处理完（无论成功失败）都出队：成功记录 sha256，失败保留旧页面。"""

        with self.conn:
            self.conn.execute(
                "DELETE FROM pending_pages WHERE topic_id = ?", (topic_id,)
            )
            if page_sha256 is not None:
                self.conn.execute(
                    "UPDATE topics SET page_sha256 = ?, updated_at = updated_at "
                    "WHERE id = ?",
                    (page_sha256, topic_id),
                )

    def set_topic_updated(self, topic_id: int, now: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE topics SET updated_at = ? WHERE id = ?", (now, topic_id)
            )
