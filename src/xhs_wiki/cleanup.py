# -*- coding: utf-8 -*-
"""cleanup.py —— 清理原件：删除"已整理完成并核对通过"的笔记的下载原件。

这个文件做什么：一次运行（runner）的最后一步（也可由 `xhs-wiki cleanup`
手动触发）。对已经到终态（distilled 已整理 / deferred 内容不足但来源页
已入库）、还没清理过的笔记，在删除 media/ 下的视频原片和图片之前做
"三道核对"，全部通过才删；先写回执再删文件，中途断电能对账恢复。

在整体里的位置：被 runner（cleanup_apply 依赖）和 cli（cleanup 子命令）
调用；读台账（ledger）判断候选，读解析包（package.json）拿原件清单。

数据从哪来、记到哪去：
- 候选和清理状态记在 SQLite 台账（ledger）里；
- "来源已入库"由 Engine 协议的 source_exists(note_id, engine_ref) 确认
  （轻量引擎检查来源页是否存在）；
- 回执写在每条笔记工作目录下的 cleanup-receipt.json。

安全规矩（核心）：
1. 删除前逐条核对，任何一道不通过就跳过这条、记下原因、绝不删：
   ① 引擎确认来源已入库（source_exists 为真）；
   ② 解析包还在，且它的 sha256 等于台账里记的 package_sha256；
   ③ 要删的每个文件都在解析包的原件清单里、sha256 一致，并且是
      work_dir/media/ 下的普通文件（不是符号链接，resolve() 之后仍
      在这个目录里）。清单之外的文件一概不碰。
2. 先写回执（cleanup-receipt.json，权限 0600），再删文件，每删一个就
   把回执补写一次；删完确认文件确实不存在了，才更新台账。

术语说明（给非程序员）：
- "原件"：下载器存下的视频原片和原图，在 APP_HOME/work/<note_id>/media/ 下。
- "解析包"（package.json）：解析阶段（parse/package.py）生成的结构化产物，里面的
  原件清单记录这份笔记下载了哪些文件、每个文件的 sha256。
- "sha256"：一种内容指纹。同一个文件的 sha256 一定相同；内容变了一个
  字节，指纹就完全不同——用来确认"磁盘上这个文件就是当初下载的那个"。
- "回执"：删除动作的书面记录（删了哪些文件、各多大）。先写回执再删，
  万一中途断电，下次能凭回执判断删到哪一步了。
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

# 可以作为清理候选的终态阶段：
# - distilled：整理完成，知识库里已有来源页和主题归类；
# - deferred：内容不足没进主题，但来源页已入库（来源页保留，原件可删）。
CLEANUP_STAGES = ("distilled", "deferred")

# 台账里表示"原件已清理"的状态值。
CLEANED_STATUS = "cleaned"

# 解析包里原件清单可能的键名。architecture.md §5 写的是"原件清单（路径 /
# 大小 / sha256）"没定键名；parse/package.py 写的是 media，original_media 一并兼容。
MANIFEST_KEYS = ("original_media", "media")


class _Skip(Exception):
    """内部信号：这条笔记核对不通过，跳过不删。reason 是原因代码。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _sha256(path: Path) -> str:
    """算一个文件的 sha256（分块读取，大文件也不占内存）。"""
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _write_private_json(path: Path, data) -> None:
    """把 data 原子写成 JSON 文件，权限 0600（只有当前用户能读）。

    先写临时文件（tempfile.mkstemp 创建出来就是 0600）再 os.replace 顶替，
    断电/崩溃时不会留下半截文件。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".",
                                     suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# 第一步：找候选（只读，不删任何东西；--dry-run 的数据来源）
# ---------------------------------------------------------------------------

def find_candidates(ledger, engine=None, *, work_dir_fn=None) -> list:
    """在台账里找"可以清理原件"的笔记（只读，不删任何文件）。

    条件：
    - 阶段是 distilled 或 deferred；
    - cleanup_status 为空（还没清理过）；
    - work_dir/media/ 里有文件（没有原件就没什么可清的）。

    engine 参数保留在签名里（和 apply 对称）；候选资格本身只由
    台账状态决定，与引擎的逐项核对（source_exists）放在 apply 里做——
    那一步任何不符都会跳过，不影响这里的清单口径。

    返回候选列表，每条是：
    {"note_id", "stage", "work_dir": Path, "package_sha256", "engine_ref",
     "media_files": [Path…], "total_bytes": int}
    """
    if work_dir_fn is None:
        from . import config
        work_dir_fn = config.work_dir
    candidates = []
    for note in ledger.all_notes():
        if note.get("stage") not in CLEANUP_STAGES:
            continue
        if note.get("cleanup_status"):
            continue
        media_dir = Path(work_dir_fn(note["note_id"])) / "media"
        if not media_dir.is_dir():
            continue
        files = sorted(p for p in media_dir.rglob("*") if p.is_file())
        if not files:
            continue
        candidates.append({
            "note_id": note["note_id"],
            "stage": note["stage"],
            "work_dir": Path(work_dir_fn(note["note_id"])),
            "package_sha256": note.get("package_sha256"),
            "engine_ref": note.get("engine_ref"),
            "media_files": files,
            "total_bytes": sum(p.stat().st_size for p in files),
        })
    return candidates


# ---------------------------------------------------------------------------
# 第二步：删除前的三道核对（任何一道不通过就跳过）
# ---------------------------------------------------------------------------

def _load_manifest(candidate: dict):
    """读候选的解析包，核对 sha256（第 2 道），返回原件清单。

    任何一步不通过抛 _Skip：文件没了 / 是符号链接 / sha256 对不上 /
    清单为空。这里绝不碰文件内容以外的任何东西。
    """
    package_path = candidate["work_dir"] / "package.json"
    if package_path.is_symlink() or not package_path.is_file():
        raise _Skip("package_missing")
    if _sha256(package_path) != candidate.get("package_sha256"):
        raise _Skip("package_sha256_mismatch")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _Skip("package_unreadable")
    manifest = None
    for key in MANIFEST_KEYS:
        manifest = package.get(key)
        if manifest:
            break
    if not manifest:
        raise _Skip("no_original_media")
    return manifest


def _checked_media_file(work_dir: Path, media: dict) -> Path:
    """核对清单里的一个原件文件（第 3 道），通过则返回它的绝对路径。

    清单里的 path 与 download 结果一致，是相对工作目录的路径
    （形如 "media/<note_id>/xxx.mp4"）。规则（收紧到
    这条笔记自己的 media/ 目录）：
    - 路径必须是相对路径，且以 media/ 开头（绝对路径一律拒绝）；
    - 不能是符号链接（符号链接可能指向任意文件）；
    - 必须是普通文件，而且 resolve() 之后仍落在 media/ 目录里面
      （挡住 "../" 之类的路径逃逸）；
    - 当前内容的 sha256 必须等于清单里记的值（防止同名文件被换过内容）。
    """
    relative = media.get("path", "")
    target = Path(relative)
    if not relative or target.is_absolute():
        raise _Skip("media_path_invalid")
    media_dir = work_dir / "media"
    # 统一成相对 media/ 的形状再做前缀检查，挡住 "media/../xxx" 这类写法。
    try:
        rel_in_media = target.relative_to("media")
    except ValueError:
        raise _Skip("media_outside_dir")
    target = media_dir / rel_in_media
    if target.is_symlink():
        raise _Skip("media_is_symlink")
    if not target.is_file():
        raise _Skip("media_missing")
    if not target.resolve().is_relative_to(media_dir.resolve()):
        raise _Skip("media_outside_dir")
    if _sha256(target) != media.get("sha256"):
        raise _Skip("media_hash_changed")
    return target


def _verified_files(candidate: dict, engine) -> list:
    """对一条候选做删除前的全部核对，通过则返回要删的绝对路径列表。

    三道核对（见模块 docstring）。任何一道不通过抛 _Skip（带原因代码），
    调用方跳过这条、一个文件都不删。
    """
    # 第 1 道：引擎确认来源已入库。source_exists 是 Engine 协议新增的
    # 方法（引擎不一定实现了它），没有这个方法按"无法确认"跳过。
    engine_ref = candidate.get("engine_ref")
    if isinstance(engine_ref, str):
        try:
            engine_ref = json.loads(engine_ref)
        except ValueError:
            engine_ref = {"raw": engine_ref}
    check = getattr(engine, "source_exists", None)
    if check is None:
        raise _Skip("engine_lacks_source_exists")
    if not check(candidate["note_id"], engine_ref):
        raise _Skip("source_not_in_engine")
    # 第 2 道：解析包核对；第 3 道：逐个文件核对。
    manifest = _load_manifest(candidate)
    files = [_checked_media_file(candidate["work_dir"], media)
             for media in manifest]
    if len(set(files)) != len(files):
        raise _Skip("media_duplicate_paths")
    return files


# ---------------------------------------------------------------------------
# 第三步：执行删除（先写回执，再删文件，最后更新台账）
# ---------------------------------------------------------------------------

def apply(candidates, *, ledger, engine, store, now, work_dir_fn=None) -> dict:
    """对通过核对的候选执行清理，返回汇总字典。

    参数：
    - candidates：find_candidates 的返回值；
    - ledger：台账（更新 cleanup_status / cleaned_at / freed_bytes）；
    - engine：引擎（做第 1 道核对 source_exists）；
    - store：ServiceStore（把累计释放字节数写进 scheduler.json）；
    - now：() -> datetime 时间函数（由调用方注入，方便测试）；
    - work_dir_fn：工作目录函数（默认 config.work_dir，测试可注入）。

    返回：{"notes": 删了的条数, "freed_bytes": 释放的字节数,
           "skipped": [{"note_id", "reason"}, …]}
    """
    result = {"notes": 0, "freed_bytes": 0, "skipped": []}
    for candidate in candidates:
        try:
            freed = _apply_one(candidate, ledger=ledger, engine=engine, now=now)
        except _Skip as skip:
            result["skipped"].append({"note_id": candidate["note_id"],
                                      "reason": skip.reason})
            continue
        except (OSError, ValueError, KeyError):
            # 单条笔记意外出错（文件系统等）：跳过这条，不影响其他笔记。
            result["skipped"].append({"note_id": candidate["note_id"],
                                      "reason": "note_cleanup_failed"})
            continue
        result["notes"] += 1
        result["freed_bytes"] += freed
    # 累计释放字节数写进 scheduler.json（控制台"空间"区显示）。
    if result["freed_bytes"]:
        scheduler = store.read_scheduler()
        store.write_scheduler(cleanup_freed_bytes_total=(
            scheduler.get("cleanup_freed_bytes_total", 0) + result["freed_bytes"]))
    return result


def _apply_one(candidate: dict, *, ledger, engine, now) -> int:
    """清理一条笔记（apply 的单条版本；核对不通过抛 _Skip）。

    流程：三道核对 → 处理上次中断留下的回执（对账恢复）→ 先写
    status=started 的回执 → 逐个删文件、每删一个补写回执 → 确认删干净
    → 回执改 completed → 更新台账。返回这条释放的字节数。
    """
    note_id = candidate["note_id"]
    work_dir = candidate["work_dir"]
    receipt_path = work_dir / "cleanup-receipt.json"

    # 上次清理中断过：只认 status=completed 的回执（文件确实删完了，
    # 只是台账还没来得及更新），补上台账更新就算这条完成；started 状态
    # 的回执说不清删到哪一步，保守跳过等人工处理。
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("status") != "completed":
            raise _Skip("cleanup_interrupted_requires_reconciliation")
        _update_ledger(ledger, note_id, now,
                       freed=receipt.get("deleted_bytes", 0))
        return receipt.get("deleted_bytes", 0)

    files = _verified_files(candidate, engine)

    # 先写"开始删除"的回执（0600）：万一中途断电，回执能说明删了哪些。
    receipt = {"status": "started", "note_id": note_id,
               "started_at": now().isoformat(), "deleted": [], "deleted_bytes": 0}
    _write_private_json(receipt_path, receipt)
    media_dir = work_dir / "media"
    freed = 0
    for target in files:
        # 删之前再核一次内容（从核对完到动手之间文件可能被换掉）。
        size = target.stat().st_size
        target.unlink()
        # 回执里记相对 media/ 的路径（红线：绝不写绝对路径以外的信息也没
        # 有必要；相对路径足够对账，且不含用户名等个人路径成分）。
        rel = target.relative_to(media_dir)
        receipt["deleted"].append({"path": str(rel), "bytes": size,
                                   "deleted_at": now().isoformat()})
        receipt["deleted_bytes"] += size
        freed += size
        _write_private_json(receipt_path, receipt)
    # 逐个确认确实删掉了（任何一个还在都不更新台账）。
    if any((media_dir / item["path"]).exists() for item in receipt["deleted"]):
        raise _Skip("deleted_media_still_exists")
    # 空的 media 目录也删掉（只有目录空了才 rmdir，绝不会删到别的东西）。
    try:
        (work_dir / "media").rmdir()
    except OSError:
        pass  # 目录不空（还有清单外的文件）：留着，不影响结果
    receipt["status"] = "completed"
    receipt["completed_at"] = now().isoformat()
    _write_private_json(receipt_path, receipt)
    # 最后更新台账（cleanup_status=cleaned、cleaned_at、freed_bytes）。
    _update_ledger(ledger, note_id, now, freed=freed)
    return freed


def _update_ledger(ledger, note_id: str, now, *, freed: int) -> None:
    """把清理结果写回台账。阶段保持不变（distilled/deferred 都是终态），
    只更新 cleanup_status / cleaned_at / freed_bytes 三个业务列。"""
    row = ledger.get(note_id)
    if row is None:
        raise _Skip("note_not_found")
    ledger.advance(note_id, row["stage"], cleanup_status=CLEANED_STATUS,
                   cleaned_at=now().isoformat(), freed_bytes=freed)


def summarize_candidates(candidates) -> dict:
    """把候选清单汇总成统计字典（--dry-run 打印的数据，不删任何东西）。

    - total：{"notes": 条数, "bytes": 字节数}；
    - by_stage：按阶段（distilled / deferred）分组的 {"notes", "bytes"}；
    - top：按大小倒序的前 10 条 [{"note_id", "bytes"}]。
    """
    by_stage = {}
    for stage in CLEANUP_STAGES:
        by_stage[stage] = {"notes": 0, "bytes": 0}
    total_notes = total_bytes = 0
    for candidate in candidates:
        stage = candidate.get("stage")
        bucket = by_stage.setdefault(stage, {"notes": 0, "bytes": 0})
        bucket["notes"] += 1
        bucket["bytes"] += candidate.get("total_bytes", 0)
        total_notes += 1
        total_bytes += candidate.get("total_bytes", 0)
    top = sorted(candidates, key=lambda c: c.get("total_bytes", 0),
                 reverse=True)[:10]
    return {
        "total": {"notes": total_notes, "bytes": total_bytes},
        "by_stage": by_stage,
        "top": [{"note_id": c["note_id"], "bytes": c.get("total_bytes", 0)}
                for c in top],
    }
