# -*- coding: utf-8 -*-
"""ledger.py —— 收藏台账（SQLite）：每条收藏处于哪个处理阶段。

这个文件做什么：维护 APP_HOME/ledger.sqlite3，记录每条收藏从"扫描发现"
到"整理完成"的完整过程。一次运行（runner）就是按台账推进：
    discovered（发现）→ downloaded（已下载）→ parsed（已解析）
    → ingested（已入库）→ distilled（已整理，成功终态）
另有 deferred（内容不足，终态）、skipped（首次范围跳过的旧收藏，终态）
和 failed:<环节>（重试超限后停在失败）。

在整体里的位置：基础层之一，被 scan/runner/控制台使用；
表结构见 docs/architecture.md §2。

安全要点（红线）：
- 带 xsec_token 的签名链接只存在 signed_links 表里。get()/all_notes()/
  next_batch() 返回的字段里没有它，本模块的异常信息和 __repr__ 也不含它；
- 数据库文件权限 0600，目录由 config.app_home() 保证 0700；
- 并发安全：开 WAL 模式 + busy_timeout=10000，多个进程（守护进程、
  运行子进程、控制台）同时读写时靠 SQLite 自己的锁排队。

术语说明（给非程序员）：
- "WAL 模式"：SQLite 的一种日志模式（Write-Ahead Logging），写的时候
  不阻塞读，多个进程同时用同一个数据库更不容易互相卡住。
- "busy_timeout"：数据库被别的进程锁住时，最多先等多少毫秒再报错。
- "upsert"：存在就更新、不存在就插入的写法。
"""

import os
import sqlite3
from pathlib import Path

from .store import now_beijing

# 正常推进的阶段，顺序固定。索引即位置：discovered 是第 0 步。
STAGES = ("discovered", "downloaded", "parsed", "ingested", "distilled")

# 终态之一：内容不足，来源页保留但不整理。
STAGE_DEFERRED = "deferred"

# 首次导入按范围跳过的旧收藏单独计数，不能混入 discovered 的待下载队列。
STAGE_SKIPPED = "skipped"
# 不传首次范围参数的普通登记不建立基线；显式传 None 表示首次选了全部。
_NO_IMPORT_BASELINE = object()

# 失败阶段的前缀，完整值形如 "failed:download"。
FAILED_PREFIX = "failed:"

# 处理环节名（用在 failed:<环节> 里）→ 失败时笔记所处的阶段。
# 例：下载环节失败时，笔记还停留在 discovered；解析环节失败时停在
# downloaded。方向和 STAGES 相同：环节的输入阶段。
OP_TO_STAGE = {
    "download": "discovered",
    "parse": "downloaded",
    "ingest": "parsed",
    "distill": "ingested",
}
# 反向映射：retry 时把 failed:<环节> 退回到失败前的阶段。
STAGE_TO_OP = {stage: op for op, stage in OP_TO_STAGE.items()}

# 每个阶段默认最多尝试 3 次（含第一次），超过就停在 failed:<环节>。
MAX_ATTEMPTS = 3

# advance(note_id, stage, **fields) 里 fields 允许更新的列。
# 只放开业务列，防止调用方误写 stage/attempts 之类破坏状态机。
UPDATABLE_COLUMNS = frozenset(
    {
        "title",
        "url",
        "list_position",
        "media_kind",
        "package_sha256",
        "engine",
        "engine_ref",
        "deferred_reason",
        "cleanup_status",
        "cleaned_at",
        "freed_bytes",
    }
)

# 建表语句（docs/architecture.md §2）。signed_links 单独一张表：
# 签名链接是敏感信息，和普通字段隔离开，任何对外输出都不带它。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  note_id TEXT PRIMARY KEY,
  title TEXT, url TEXT,
  first_seen_at TEXT, list_position INTEGER,
  stage TEXT NOT NULL,
  stage_updated_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  media_kind TEXT,
  package_sha256 TEXT,
  engine TEXT, engine_ref TEXT,
  deferred_reason TEXT,
  cleanup_status TEXT,
  cleaned_at TEXT, freed_bytes INTEGER
);
CREATE TABLE IF NOT EXISTS signed_links (
  note_id TEXT PRIMARY KEY, url TEXT NOT NULL, captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Ledger:
    """台账数据库的读写入口。

    每个进程各自建一个实例（持有各自的 SQLite 连接）；构造时完成：
    开 WAL、设 busy_timeout、建表、写入 schema_version=1、文件权限 0600。
    时间函数 now_fn 可以注入（默认北京时间），测试时传固定时间。
    """

    def __init__(self, path: Path, *, now_fn=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now_fn or now_beijing
        # check_same_thread=False：控制台线程和运行线程可能共用一个实例。
        self._conn = sqlite3.connect(str(self.path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', '1') "
                "ON CONFLICT(key) DO NOTHING"
            )
        # 台账里有笔记标题和链接，收紧到只有当前用户可读写。
        os.chmod(self.path, 0o600)

    # repr 只出现路径，绝不出现数据库内容（防签名链接泄漏）。
    def __repr__(self):
        return f"Ledger({self.path.name})"

    def close(self) -> None:
        """关闭数据库连接（进程退出前调用；忘记调用也会被 GC 兜底）。"""
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------- 扫描登记 ----------------

    def upsert_discovered(self, items, *, first_import_limit=_NO_IMPORT_BASELINE,
                          scan_complete=True) -> list:
        """登记一次扫描发现的收藏，返回**新增**的笔记编号列表。

        items 每项形如 {"note_id","title","url","signed_url","list_position"}：
        - 新笔记：插入 notes（阶段 discovered、attempts=0），并写 signed_links；
        - 已有笔记：不改阶段（它可能已经处理到一半），但更新 signed_links
          （链接签名 xsec_token 每次扫描都会换新，旧的已经失效）和
          list_position（收藏位置会变）。

        整批放在一个事务里：要么全部登记成功，要么全部不动。
        signed_url 允许缺省（None 时只更新 list_position）。

        runner 在成功扫描后显式传 first_import_limit（以及这次扫描是否完整
        scan_complete）：首次范围的基线和跳过旧收藏都在同一个事务里完成，
        避免中断后按变动的位置重新划范围。规则见 _apply_first_import。
        省略 first_import_limit 的底层登记保持原行为（不涉及首次范围）。
        """
        if (first_import_limit is not _NO_IMPORT_BASELINE
                and first_import_limit is not None
                and (type(first_import_limit) is not int or first_import_limit <= 0)):
            raise ValueError("first_import_limit_invalid")
        now = self._now().isoformat()
        added = []
        # 本次扫描里：新插入的笔记 (编号, 位置)，以及已有笔记的位置。
        added_positions = []
        known_positions = []
        with self._conn:
            for item in items:
                note_id = item.get("note_id")
                if not isinstance(note_id, str) or not note_id:
                    raise ValueError("note_id_invalid")
                # 原子的"插入或忽略"：两个进程同时登记同一条笔记时，
                # SELECT 再 INSERT 会有竞态（两边都查到"不存在"，后插的
                # 撞唯一约束）。ON CONFLICT DO NOTHING 让插入本身成为
                # 判断依据——rowcount 为 1 说明这次真的插进去了（新笔记），
                # 为 0 说明别的进程（或本次更早的扫描）已经插过（旧笔记）。
                cur = self._conn.execute(
                    "INSERT INTO notes(note_id, title, url, first_seen_at,"
                    " list_position, stage, stage_updated_at, attempts)"
                    " VALUES(?,?,?,?,?,?,?,0)"
                    " ON CONFLICT(note_id) DO NOTHING",
                    (
                        note_id,
                        item.get("title"),
                        item.get("url"),
                        now,
                        item.get("list_position"),
                        STAGES[0],
                        now,
                    ),
                )
                position = item.get("list_position")
                if cur.rowcount == 1:
                    added.append(note_id)
                    added_positions.append((note_id, position))
                else:
                    # 已有笔记：只更新收藏位置（阶段不动）。
                    self._conn.execute(
                        "UPDATE notes SET list_position = ? WHERE note_id = ?",
                        (position, note_id),
                    )
                    known_positions.append(position)
                signed_url = item.get("signed_url")
                if signed_url:
                    self._conn.execute(
                        "INSERT INTO signed_links(note_id, url, captured_at)"
                        " VALUES(?,?,?)"
                        " ON CONFLICT(note_id) DO UPDATE SET url=excluded.url,"
                        " captured_at=excluded.captured_at",
                        (note_id, signed_url, now),
                    )
            if first_import_limit is not _NO_IMPORT_BASELINE:
                self._apply_first_import(
                    first_import_limit, scan_complete=scan_complete,
                    item_count=len(items), added_positions=added_positions,
                    known_positions=known_positions, now=now)
        return added

    def _apply_first_import(self, limit, *, scan_complete, item_count,
                            added_positions, known_positions, now) -> None:
        """首次导入范围（"最近 N 条"）的一次性基线。

        必须在 upsert_discovered 的事务里调用：基线标记和阶段变更一起提交，
        中途崩溃一起回滚。规则分三种情况：

        1. 还没建立基线，而且这次扫描**看不全前 N 条**（扫描不完整、扫到的
           条数也不到 N）：不建立基线、也不跳过任何笔记。以前首扫只拿到
           首屏 10 条就把基线占掉了，之后更深的扫描里冒出来的几百条旧收藏
           全被当成"新收藏"导入，"最近 20 条"变成了全量导入（花用户的钱）。
           这时看到的都在前 N 条之内，照常处理；等能看全前 N 条时再建基线。
        2. 还没建立基线，这次能看全前 N 条（扫描完整，或扫到的条数 ≥ N）：
           建立基线，记下当时的选择（N 或"全部"）；选了 N 时，把位置 ≥ N、
           还没开始处理的笔记移到 skipped（已开始处理的保留进度）。
        3. 基线已建立、当时选的是 N、现在仍是 N：本次**新插入**的笔记如果
           排在本次扫描里**最靠后的那条已知笔记**之后，说明它比我们见过的
           所有收藏都更早——是基线扫描没看到的旧收藏，标为 skipped。
           新收藏一定出现在列表最前面，不受影响。
           为什么比较"最靠后的已知笔记"而不是"最靠前的"：用户取消再重新
           收藏一条旧笔记时，它会跳到列表最前面；如果拿最靠前的已知笔记
           比较，当天真正的新收藏会被误判成旧收藏跳过。
           当前选"全部"、或首次选的就是"全部"时，不做任何跳过（选全部时
           runner 在运行开始还会把 skipped 恢复，见 restore_skipped）。
        """
        claimed = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'first_import_baseline'"
        ).fetchone() is not None
        if not claimed:
            if limit is not None and not scan_complete and item_count < limit:
                return  # 情况 1：前 N 条还没看全，先不建基线。
            # 情况 2：INSERT 抢占唯一标记（并发时只有一方成功）。
            took = self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('first_import_baseline', ?)"
                " ON CONFLICT(key) DO NOTHING", (now,),
            ).rowcount
            if not took:
                return
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('first_import_baseline_limit', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("all" if limit is None else str(limit),),
            )
            if limit is not None:
                # 已开始处理的记录保留进度；只有尚待下载的旧记录被跳过。
                self._conn.execute(
                    "UPDATE notes SET stage = ?, stage_updated_at = ?"
                    " WHERE stage = 'discovered' AND list_position >= ?",
                    (STAGE_SKIPPED, now, limit),
                )
            return
        # 情况 3。没有记录首次选择的旧台账（这次修复之前建的基线）按"选了
        # N"处理：宁可少导入、让用户在控制台改选全部恢复，也不默默多花钱。
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'first_import_baseline_limit'"
        ).fetchone()
        if limit is None or (row is not None and row["value"] == "all"):
            return
        known = [p for p in known_positions if isinstance(p, int)]
        if not known:
            return
        deepest_known = max(known)
        for note_id, position in added_positions:
            if isinstance(position, int) and position > deepest_known:
                self._conn.execute(
                    "UPDATE notes SET stage = ?, stage_updated_at = ?"
                    " WHERE note_id = ? AND stage = 'discovered'",
                    (STAGE_SKIPPED, now, note_id),
                )

    def restore_skipped(self) -> int:
        """用户改选全部时恢复旧收藏，但保留一次性基线，避免以后再次跳过。

        runner 在运行开始时调用；只恢复 skipped，不动失败或处理中记录。
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE notes SET stage = 'discovered', stage_updated_at = ?,"
                " attempts = 0, last_error = NULL WHERE stage = ?",
                (self._now().isoformat(), STAGE_SKIPPED),
            )
        return cur.rowcount

    # ---------------- 查询 ----------------

    def _row_to_dict(self, row) -> dict:
        """把一行转成 dict。只转 notes 表的行——天然不含签名链接。"""
        return {key: row[key] for key in row.keys()}

    def next_batch(self, stage, limit, max_attempts=MAX_ATTEMPTS) -> list:
        """取出处于某个阶段、还能重试（attempts < max_attempts）的笔记。

        按 list_position 从小到大排序——收藏列表的位置越靠前表示越是
        最近收藏的，所以最新收藏的排在前面。
        返回 dict 列表（不含签名链接）。
        """
        if stage not in STAGES and stage not in (STAGE_DEFERRED, STAGE_SKIPPED):
            raise ValueError(f"unknown_stage:{stage}")
        cur = self._conn.execute(
            "SELECT * FROM notes WHERE stage = ? AND attempts < ?"
            " ORDER BY list_position ASC LIMIT ?",
            (stage, max_attempts, limit),
        )
        return [self._row_to_dict(r) for r in cur.fetchall()]

    def get(self, note_id: str) -> "dict | None":
        """读一条笔记的完整台账行（不含签名链接）；不存在返回 None。"""
        cur = self._conn.execute(
            "SELECT * FROM notes WHERE note_id = ?", (note_id,)
        )
        row = cur.fetchone()
        return self._row_to_dict(row) if row is not None else None

    def all_notes(self, stage=None) -> list:
        """列出全部笔记（或某个阶段的笔记），按收藏位置排序。"""
        if stage is None:
            cur = self._conn.execute(
                "SELECT * FROM notes ORDER BY list_position ASC"
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM notes WHERE stage = ? ORDER BY list_position ASC",
                (stage,),
            )
        return [self._row_to_dict(r) for r in cur.fetchall()]

    def signed_url(self, note_id: str) -> "str | None":
        """读某条笔记最新的签名链接（只有下载环节需要它）。

        这是本模块**唯一**对外提供签名链接的出口；调用方拿到后绝不能
        写进日志或状态。
        """
        cur = self._conn.execute(
            "SELECT url FROM signed_links WHERE note_id = ?", (note_id,)
        )
        row = cur.fetchone()
        return row["url"] if row is not None else None

    def counts(self) -> dict:
        """按阶段统计条数，形如 {"discovered": 3, "failed:download": 1}。"""
        cur = self._conn.execute(
            "SELECT stage, COUNT(*) AS n FROM notes GROUP BY stage ORDER BY stage"
        )
        return {row["stage"]: row["n"] for row in cur.fetchall()}

    # ---------------- 状态推进 ----------------

    def advance(self, note_id: str, stage, **fields) -> None:
        """把笔记推进到下一阶段，attempts 清零、清掉 last_error。

        fields 可顺带更新业务列（media_kind、package_sha256、engine……），
        列名不在 UPDATABLE_COLUMNS 里就抛 ValueError（防误写状态机字段）。
        """
        if stage not in STAGES and stage not in (STAGE_DEFERRED, STAGE_SKIPPED):
            raise ValueError(f"unknown_stage:{stage}")
        for key in fields:
            if key not in UPDATABLE_COLUMNS:
                raise ValueError(f"unknown_field:{key}")
        sets = ["stage = ?", "stage_updated_at = ?", "attempts = 0", "last_error = NULL"]
        params = [stage, self._now().isoformat()]
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(value)
        params.append(note_id)
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE notes SET {', '.join(sets)} WHERE note_id = ?", params
            )
        if cur.rowcount == 0:
            # 用错误代码报错，不把任何行内容放进异常。
            raise ValueError("note_not_found")

    def fail(self, note_id: str, failed_stage: str, code: str) -> None:
        """记录某个环节的一次失败。

        failed_stage 是**失败时笔记所处的阶段**（如下载失败时是 discovered）。
        规则（docs/architecture.md §2）：
        - attempts 加 1，记下 last_error（错误代码，不含链接）；
        - attempts 还没到上限：阶段退回（保持在）failed_stage，下次还会被
          next_batch 取出来重试；
        - attempts 达到上限（默认 3）：阶段改成 failed:<环节> 并停在那里，
          等用户在控制台单条重试。
        例：下载失败 1 次后笔记仍是 discovered、attempts=1；
        失败 3 次后变成 failed:download。
        """
        op = STAGE_TO_OP.get(failed_stage)
        if op is None:
            raise ValueError(f"unknown_stage:{failed_stage}")
        now = self._now().isoformat()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE notes SET attempts = attempts + 1, last_error = ?,"
                " stage_updated_at = ? WHERE note_id = ?",
                (code, now, note_id),
            )
            if cur.rowcount == 0:
                raise ValueError("note_not_found")
            attempts = self._conn.execute(
                "SELECT attempts FROM notes WHERE note_id = ?", (note_id,)
            ).fetchone()["attempts"]
            if attempts >= MAX_ATTEMPTS:
                self._conn.execute(
                    "UPDATE notes SET stage = ?, stage_updated_at = ?"
                    " WHERE note_id = ?",
                    (FAILED_PREFIX + op, now, note_id),
                )

    def retry(self, note_id: str) -> None:
        """把停在 failed:<环节> 的笔记退回到失败前的阶段，attempts 清零。

        只对失败终态有效；笔记不在失败状态时抛 ValueError。
        """
        row = self._conn.execute(
            "SELECT stage FROM notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        if row is None:
            raise ValueError("note_not_found")
        stage = row["stage"]
        if not stage.startswith(FAILED_PREFIX):
            raise ValueError("note_not_failed")
        op = stage[len(FAILED_PREFIX):]
        back_to = OP_TO_STAGE.get(op)
        if back_to is None:
            raise ValueError("unknown_stage:" + FAILED_PREFIX + op)
        with self._conn:
            self._conn.execute(
                "UPDATE notes SET stage = ?, attempts = 0, last_error = NULL,"
                " stage_updated_at = ? WHERE note_id = ?",
                (back_to, self._now().isoformat(), note_id),
            )
