# -*- coding: utf-8 -*-
"""engines/llmwiki/journal.py —— 写请求的请求日志（防止重复写入 / 丢失结果）。

这个文件做什么：把每个发往后台的 POST 请求登记在 APP_HOME 下的
sqlite 数据库里（llmwiki-journal.sqlite3，绝不放进知识库目录）。

为什么需要它：后台的幂等键缓存只在内存里、不跨重启；失败结果也会被
缓存（拿原 key 重发只会拿回同一个失败）。我们自己落盘记录每个操作
"用了哪个 key、发的是什么的签名、拿到了什么回执"，才能安全地重试。

规则：
- 每个操作用固定的"操作名"（如 <note_id>-import-commit、<group_id>-update），
  对应一个 uuid 幂等键和一个请求签名（请求体的 sha256）；
- 先把 key 存盘、再发请求——崩溃恢复后不会盲目造出第二个导入/任务；
- 同名同签名 → 复用原 key；签名变了 → ValueError("operation_input_changed")；
- 有请求、没有成功回执、又不能确定结果 → ValueError(
  "uncertain_api_write_requires_reconciliation")（除非调用方用
  durable=True 声明"结果可以用读接口核对"，比如导入流程会先查导入会话）；
- archive_and_rekey(name, reason)：把原请求移进历史记录、换一个新
  key 重来；每种原因最多 2 次，防止无限重打同一个失败操作。

在整体里的位置：引擎的导入/整理流程每个写请求都经过 Journal.run。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from xhs_wiki import config

from .client import ApiError

# 每种归档原因允许的最大次数。
MAX_ARCHIVE_PER_REASON = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    name        TEXT PRIMARY KEY,
    key         TEXT NOT NULL,
    signature   TEXT NOT NULL,
    path        TEXT NOT NULL,
    http_status INTEGER,
    error_code  TEXT,
    response    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operation_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    key         TEXT NOT NULL,
    signature   TEXT NOT NULL,
    path        TEXT NOT NULL,
    http_status INTEGER,
    error_code  TEXT,
    response    TEXT,
    reason      TEXT NOT NULL,
    archived_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_counts (
    reason TEXT PRIMARY KEY,
    count  INTEGER NOT NULL
);
"""


def _signature(path: str, body) -> str:
    """请求签名：路径 + 请求体的规范化 JSON 的 sha256。

    把 path 也放进签名——同一个操作名如果
    指向了不同的接口路径，同样算"输入变了"。
    """

    material = json.dumps([path, body], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Journal:
    """基于 sqlite 的请求日志。path 参数留空时用
    APP_HOME/llmwiki-journal.sqlite3（测试可注入临时路径）。"""

    def __init__(self, path=None) -> None:
        if path is None:
            path = config.app_home() / "llmwiki-journal.sqlite3"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)  # 私有目录权限 0700
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- 内部：数据库读写 ------------------------------------------------

    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _get(self, name: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM operations WHERE name = ?", (name,)
        ).fetchone()

    def _insert(self, name: str, key: str, signature: str, path: str) -> None:
        now = self._now()
        self._conn.execute(
            "INSERT INTO operations (name, key, signature, path, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, key, signature, path, now, now),
        )
        self._conn.commit()  # 关键：发送前就落盘

    # ---- 对外接口 ---------------------------------------------------------

    def key_of(self, name: str) -> str | None:
        """这个操作当前的幂等键；没有登记过返回 None。"""

        row = self._get(name)
        return row["key"] if row else None

    def outcome_of(self, name: str) -> dict | None:
        """这个操作最近一次发送的结果：{"http_status", "error_code",
        "has_response"}；没发过就是 None。"""

        row = self._get(name)
        if row is None:
            return None
        return {
            "http_status": row["http_status"],
            "error_code": row["error_code"],
            "has_response": row["response"] is not None,
        }

    def run(self, name: str, path: str, body, client, *, durable: bool = False):
        """执行（或重放）一个写操作，返回后台回执里的 data。

        流程：
        1. 算签名；已有登记则核对签名（不一致 → operation_input_changed）；
        2. 已有成功回执 → 直接返回缓存的回执，不再发请求；
        3. 发过但没有回执且调用方声明不能自行核对结果 → 抛
           uncertain_api_write_requires_reconciliation；
        4. 新操作：生成 uuid key，先落盘再发送；
        5. 发送失败（ApiError）记下状态码和错误码后原样上抛；
           成功则把回执存盘。
        """

        signature = _signature(path, body)
        row = self._get(name)
        if row is not None:
            if row["signature"] != signature:
                raise ValueError("operation_input_changed")
            if row["response"] is not None:
                return json.loads(row["response"])
            if not durable:
                raise ValueError("uncertain_api_write_requires_reconciliation")
            key = row["key"]  # durable 重发：输入没变，复用原 key
        else:
            key = str(uuid.uuid4())
            self._insert(name, key, signature, path)  # 先存盘，再发送
        try:
            status, payload = client.request("POST", path, body, key)
        except ApiError as error:
            self._record_failure(name, error.status, error.code)
            raise
        data = payload.get("data") if isinstance(payload, dict) else None
        self._record_response(name, data)
        return data

    def _record_failure(self, name: str, status: int, code: str) -> None:
        self._conn.execute(
            "UPDATE operations SET http_status = ?, error_code = ?,"
            " response = NULL, updated_at = ? WHERE name = ?",
            (int(status), code, self._now(), name),
        )
        self._conn.commit()

    def _record_response(self, name: str, data) -> None:
        self._conn.execute(
            "UPDATE operations SET response = ?, http_status = NULL,"
            " error_code = NULL, updated_at = ? WHERE name = ?",
            (json.dumps(data, ensure_ascii=False), self._now(), name),
        )
        self._conn.commit()

    def archive_and_rekey(self, name: str, reason: str) -> str:
        """把原请求移进历史记录，给同名操作换一个新 key。

        适用场景：后台缓存了某个失败回执（原 key 重发只会拿回同一个
        失败），或被驳回的候选使旧请求作废。每种 reason 最多归档 2 次，
        超过抛 ValueError("archive_limit_reached")。
        """

        row = self._get(name)
        if row is None:
            raise ValueError("operation_not_found")
        counter = self._conn.execute(
            "SELECT count FROM archive_counts WHERE reason = ?", (reason,)
        ).fetchone()
        used = counter["count"] if counter else 0
        if used >= MAX_ARCHIVE_PER_REASON:
            raise ValueError("archive_limit_reached")
        # 1) 原操作移进历史
        self._conn.execute(
            "INSERT INTO operation_history"
            " (name, key, signature, path, http_status, error_code,"
            "  response, reason, archived_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["name"], row["key"], row["signature"], row["path"],
                row["http_status"], row["error_code"], row["response"],
                reason, self._now(),
            ),
        )
        self._conn.execute("DELETE FROM operations WHERE name = ?", (name,))
        # 2) 计数 +1（不存在则插入）
        self._conn.execute(
            "INSERT INTO archive_counts (reason, count) VALUES (?, 1)"
            " ON CONFLICT(reason) DO UPDATE SET count = count + 1",
            (reason,),
        )
        # 3) 同名、同签名、新 key 重新登记（输入没变，只是换 key 重来）
        self._insert(name, str(uuid.uuid4()), row["signature"], row["path"])
        return self.key_of(name)

    def discard_operation(self, name: str, reason: str) -> None:
        """把一个操作整条丢弃（归档进历史、不再保留同名占位）。

        和 archive_and_rekey 的区别：archive_and_rekey 保留原签名、换新
        键"原样重发"；discard_operation 连签名一起丢掉，供导入
        确认刷新用——409 IMPORT_V2_SELECTION_STALE / 400 GIT_COMMAND_
        FAILED 之后要带**新的确认快照**（请求体变了）重发，原签名必须
        作废，原操作整体移入历史记录。
        每种 reason 的计数和 archive_and_rekey 共用同一张表，
        上限同为 2 次。这是在不改既有接口的前提下新增的方法。
        """

        row = self._get(name)
        if row is None:
            raise ValueError("operation_not_found")
        counter = self._conn.execute(
            "SELECT count FROM archive_counts WHERE reason = ?", (reason,)
        ).fetchone()
        used = counter["count"] if counter else 0
        if used >= MAX_ARCHIVE_PER_REASON:
            raise ValueError("archive_limit_reached")
        self._conn.execute(
            "INSERT INTO operation_history"
            " (name, key, signature, path, http_status, error_code,"
            "  response, reason, archived_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["name"], row["key"], row["signature"], row["path"],
                row["http_status"], row["error_code"], row["response"],
                reason, self._now(),
            ),
        )
        self._conn.execute("DELETE FROM operations WHERE name = ?", (name,))
        self._conn.execute(
            "INSERT INTO archive_counts (reason, count) VALUES (?, 1)"
            " ON CONFLICT(reason) DO UPDATE SET count = count + 1",
            (reason,),
        )
        self._conn.commit()

    def archive_count(self, reason: str) -> int:
        """某种原因已经归档过多少次（测试 / 巡检用）。"""

        counter = self._conn.execute(
            "SELECT count FROM archive_counts WHERE reason = ?", (reason,)
        ).fetchone()
        return int(counter["count"]) if counter else 0

    def history_of(self, name: str) -> list[dict]:
        """这个操作名的历史归档记录（按时间正序）。"""

        rows = self._conn.execute(
            "SELECT * FROM operation_history WHERE name = ? ORDER BY id",
            (name,),
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._conn.close()
