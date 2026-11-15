from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts


class HandoverInvariantError(Exception):
    """完成态校验不满足：整单失败，本趟归属变更与拒收标记全部回滚。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_shifts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())


def list_batches(owner_shift_id: int | None = None) -> list[dict]:
    with connect() as conn:
        if owner_shift_id is None:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                ORDER BY b.id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                WHERE b.owner_shift_id = ?
                ORDER BY b.id
                """,
                (owner_shift_id,),
            ).fetchall()
        return rows_to_dicts(rows)


def list_handovers() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT h.*,
                   fs.name AS from_shift_name,
                   ts.name AS to_shift_name,
                   (SELECT COUNT(*) FROM handover_rejections r
                     WHERE r.handover_id = h.id) AS rejected_count
            FROM handovers h
            JOIN shifts fs ON fs.id = h.from_shift_id
            JOIN shifts ts ON ts.id = h.to_shift_id
            ORDER BY h.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def _count_manifest_owned_by(conn, handover_id: int, shift_id: int) -> int:
    return conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM handover_items hi
        JOIN batches b ON b.id = hi.batch_id
        WHERE hi.handover_id = ? AND b.owner_shift_id = ?
        """,
        (handover_id, shift_id),
    ).fetchone()["c"]


def _bucket_counts(conn, handover_id: int, to_shift_id: int) -> dict:
    """分桶计数：清单数/过户数/拒收数全部从清单、批次归属、拒收记录同源 SQL 派生。"""
    manifest = conn.execute(
        "SELECT COUNT(*) AS c FROM handover_items WHERE handover_id = ?",
        (handover_id,),
    ).fetchone()["c"]
    rejected = conn.execute(
        "SELECT COUNT(*) AS c FROM handover_rejections WHERE handover_id = ?",
        (handover_id,),
    ).fetchone()["c"]
    transferred = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM handover_items hi
        JOIN batches b ON b.id = hi.batch_id
        WHERE hi.handover_id = ?
          AND b.owner_shift_id = ?
          AND hi.batch_id NOT IN (
              SELECT batch_id FROM handover_rejections WHERE handover_id = ?
          )
        """,
        (handover_id, to_shift_id, handover_id),
    ).fetchone()["c"]
    return {"清单数": manifest, "过户数": transferred, "拒收数": rejected}


def _manifest_keys(conn, handover_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT b.biz_key
        FROM handover_items hi
        JOIN batches b ON b.id = hi.batch_id
        WHERE hi.handover_id = ?
        ORDER BY b.biz_key
        """,
        (handover_id,),
    ).fetchall()
    return [r["biz_key"] for r in rows]


def _manifest_keys_owned_by(conn, handover_id: int, shift_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT b.biz_key
        FROM handover_items hi
        JOIN batches b ON b.id = hi.batch_id
        WHERE hi.handover_id = ? AND b.owner_shift_id = ?
        ORDER BY b.biz_key
        """,
        (handover_id, shift_id),
    ).fetchall()
    return [r["biz_key"] for r in rows]


def _rejected_keys(conn, handover_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT b.biz_key
        FROM handover_rejections r
        JOIN batches b ON b.id = r.batch_id
        WHERE r.handover_id = ?
        ORDER BY b.biz_key
        """,
        (handover_id,),
    ).fetchall()
    return [r["biz_key"] for r in rows]


def get_handover(handover_id: int) -> dict:
    with connect() as conn:
        h = conn.execute(
            """
            SELECT h.*,
                   fs.name AS from_shift_name,
                   ts.name AS to_shift_name
            FROM handovers h
            JOIN shifts fs ON fs.id = h.from_shift_id
            JOIN shifts ts ON ts.id = h.to_shift_id
            WHERE h.id = ?
            """,
            (handover_id,),
        ).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        items = conn.execute(
            """
            SELECT b.*
            FROM handover_items hi
            JOIN batches b ON b.id = hi.batch_id
            WHERE hi.handover_id = ?
            ORDER BY b.id
            """,
            (handover_id,),
        ).fetchall()
        rejections = conn.execute(
            """
            SELECT r.batch_id, r.reason, r.created_at, b.biz_key, b.title
            FROM handover_rejections r
            JOIN batches b ON b.id = r.batch_id
            WHERE r.handover_id = ?
            ORDER BY r.batch_id
            """,
            (handover_id,),
        ).fetchall()
        out = dict(h)
        out["items"] = rows_to_dicts(items)
        out["rejections"] = rows_to_dicts(rejections)
        out["buckets"] = _bucket_counts(conn, handover_id, h["to_shift_id"])
        return out


def list_rejections(handover_id: int) -> list[dict]:
    """拒收记录与交班单隔离可查：独立拒收表按交班单 id 关联。"""
    with connect() as conn:
        h = conn.execute(
            "SELECT id FROM handovers WHERE id = ?", (handover_id,)
        ).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        rows = conn.execute(
            """
            SELECT r.handover_id, r.batch_id, r.reason, r.created_at, b.biz_key, b.title
            FROM handover_rejections r
            JOIN batches b ON b.id = r.batch_id
            WHERE r.handover_id = ?
            ORDER BY r.batch_id
            """,
            (handover_id,),
        ).fetchall()
        return rows_to_dicts(rows)


def set_rejections(handover_id: int, rejections: list[dict]) -> dict:
    """进行中交班单的拒收集合整体落库（同事务替换，杜绝半截拒收）。"""
    with connect() as conn:
        h = conn.execute(
            "SELECT * FROM handovers WHERE id = ?", (handover_id,)
        ).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        if h["status"] != "进行中":
            raise HTTPException(400, "交班单已结案，拒收标记不可再改")
        manifest = {
            r["batch_id"]
            for r in conn.execute(
                "SELECT batch_id FROM handover_items WHERE handover_id = ?",
                (handover_id,),
            ).fetchall()
        }
        seen: set[int] = set()
        cleaned: list[tuple[int, str]] = []
        for item in rejections:
            bid = int(item["batch_id"])
            reason = str(item.get("reason") or "").strip()
            if bid not in manifest:
                raise HTTPException(400, f"批次#{bid} 不在交班单#{handover_id}清单内，不能拒收")
            if bid in seen:
                raise HTTPException(400, f"批次#{bid} 重复拒收")
            if not reason:
                raise HTTPException(400, f"批次#{bid} 拒收必须填写原因")
            seen.add(bid)
            cleaned.append((bid, reason))
        conn.execute(
            "DELETE FROM handover_rejections WHERE handover_id = ?", (handover_id,)
        )
        for bid, reason in cleaned:
            conn.execute(
                """
                INSERT INTO handover_rejections(handover_id, batch_id, reason, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (handover_id, bid, reason, _now()),
            )
        conn.commit()
    return get_handover(handover_id)


def dashboard() -> dict:
    with connect() as conn:
        shifts = rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())
        inflight = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE status = '在途'"
        ).fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM handovers WHERE status = '进行中'"
        ).fetchone()["c"]
        last = conn.execute(
            "SELECT * FROM handovers ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "班次数": len(shifts),
            "在途批次数": inflight,
            "进行中交班": active,
            "最近交班": dict(last) if last else None,
            "班次": shifts,
        }


def start_handover(from_shift_id: int, to_shift_id: int) -> dict:
    if from_shift_id == to_shift_id:
        raise HTTPException(400, "交班双方不能是同一班次")
    with connect() as conn:
        fs = conn.execute("SELECT * FROM shifts WHERE id = ?", (from_shift_id,)).fetchone()
        ts = conn.execute("SELECT * FROM shifts WHERE id = ?", (to_shift_id,)).fetchone()
        if not fs or not ts:
            raise HTTPException(404, "班次不存在")
        active = conn.execute(
            "SELECT id FROM handovers WHERE status = '进行中' LIMIT 1"
        ).fetchone()
        if active:
            raise HTTPException(409, f"已有进行中的交班单#{active['id']}，禁止并行交班")
        batches = conn.execute(
            "SELECT id FROM batches WHERE owner_shift_id = ? AND status = '在途' ORDER BY id",
            (from_shift_id,),
        ).fetchall()
        if not batches:
            raise HTTPException(400, "交班方没有在途批次可交接")
        cur = conn.execute(
            """
            INSERT INTO handovers(from_shift_id, to_shift_id, status, batch_count, started_at)
            VALUES (?, ?, '进行中', ?, ?)
            """,
            (from_shift_id, to_shift_id, len(batches), _now()),
        )
        hid = cur.lastrowid
        for b in batches:
            conn.execute(
                "INSERT INTO handover_items(handover_id, batch_id) VALUES (?, ?)",
                (hid, b["id"]),
            )
        conn.execute("UPDATE shifts SET status = '交班中' WHERE id = ?", (from_shift_id,))
        conn.execute("UPDATE shifts SET status = '接班中' WHERE id = ?", (to_shift_id,))
        conn.commit()
    return get_handover(hid)


def _insert_report(conn, handover_id: int, h, consistent: int, extra: dict | None) -> None:
    """对账报告落库：分桶数字与批次归属、拒收记录同源派生（SQL），禁止内存计数。"""
    buckets = _bucket_counts(conn, handover_id, h["to_shift_id"])
    detail = {
        "清单键": _manifest_keys(conn, handover_id),
        "过户键": _manifest_keys_owned_by(conn, handover_id, h["to_shift_id"]),
        "拒收键": _rejected_keys(conn, handover_id),
        "校验": "过户成功键 ∪ 拒收键 = 发起清单键，且三者两两不交",
    }
    if extra:
        detail.update(extra)
    conn.execute(
        """
        INSERT INTO reconcile_reports(
          handover_id, before_count, after_from_count, after_to_count,
          transferred_count, rejected_count, consistent, detail_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            handover_id,
            buckets["清单数"],
            _count_manifest_owned_by(conn, handover_id, h["from_shift_id"]),
            _count_manifest_owned_by(conn, handover_id, h["to_shift_id"]),
            buckets["过户数"],
            buckets["拒收数"],
            consistent,
            json.dumps(detail, ensure_ascii=False),
            _now(),
        ),
    )


def complete_handover(handover_id: int) -> dict:
    with connect() as conn:
        h = conn.execute(
            "SELECT * FROM handovers WHERE id = ?", (handover_id,)
        ).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        if h["status"] != "进行中":
            raise HTTPException(400, "只能完成进行中的交班单")
        # 同一数据库事务整单提交：归属变更、拒收落库校验、结案与对账报告同一失败闭，
        # 任一环节失败整单回滚，禁止先过户再补拒收造成半截成功。
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            manifest = [
                r["batch_id"]
                for r in conn.execute(
                    "SELECT batch_id FROM handover_items WHERE handover_id = ? ORDER BY batch_id",
                    (handover_id,),
                ).fetchall()
            ]
            rejected = [
                r["batch_id"]
                for r in conn.execute(
                    "SELECT batch_id FROM handover_rejections WHERE handover_id = ? ORDER BY batch_id",
                    (handover_id,),
                ).fetchall()
            ]
            mset, rset = set(manifest), set(rejected)
            if not rset <= mset:
                raise HandoverInvariantError("拒收键不在发起清单键内")
            placeholders = ",".join("?" for _ in manifest)
            states = conn.execute(
                f"SELECT id, owner_shift_id, status FROM batches WHERE id IN ({placeholders})",
                manifest,
            ).fetchall()
            bad = sorted(
                s["id"]
                for s in states
                if s["owner_shift_id"] != h["from_shift_id"] or s["status"] != "在途"
            )
            if len(states) != len(manifest) or bad:
                raise HandoverInvariantError(
                    f"清单批次{bad}已不在交班方在途，无法整单结案"
                )
            # 未拒收键过户到接班方；拒收键不过户，留在交班方在途
            for bid in sorted(mset - rset):
                conn.execute(
                    """
                    UPDATE batches SET owner_shift_id = ?
                    WHERE id = ? AND owner_shift_id = ? AND status = '在途'
                    """,
                    (h["to_shift_id"], bid, h["from_shift_id"]),
                )
            # 完成态校验（同源 SQL 派生）：过户成功键 ∪ 拒收键 = 发起清单键，且三者两两不交
            transferred_keys = set(_manifest_keys_owned_by(conn, handover_id, h["to_shift_id"]))
            stayed_keys = set(_manifest_keys_owned_by(conn, handover_id, h["from_shift_id"]))
            rejected_keys = set(_rejected_keys(conn, handover_id))
            manifest_keys = set(_manifest_keys(conn, handover_id))
            if transferred_keys & rejected_keys:
                raise HandoverInvariantError("拒收键出现在接班方")
            if stayed_keys != rejected_keys:
                raise HandoverInvariantError("拒收键未全部留在交班方在途")
            if transferred_keys | rejected_keys != manifest_keys:
                raise HandoverInvariantError("过户成功键∪拒收键 ≠ 发起清单键")
            buckets = _bucket_counts(conn, handover_id, h["to_shift_id"])
            if buckets["过户数"] + buckets["拒收数"] != buckets["清单数"]:
                raise HandoverInvariantError("分桶计数不满足 过户数+拒收数=清单数")
            conn.execute(
                "UPDATE handovers SET status = '已完成', finished_at = ?, batch_count = ? WHERE id = ?",
                (_now(), len(manifest), handover_id),
            )
            conn.execute("UPDATE shifts SET status = '已交班' WHERE id = ?", (h["from_shift_id"],))
            conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (h["to_shift_id"],))
            _insert_report(conn, handover_id, h, consistent=1, extra=None)
            conn.commit()
        except Exception as e:
            # 整单失败：回滚本趟所有归属变更与拒收标记，不留半截成功
            conn.rollback()
            reason = str(e) if isinstance(e, HandoverInvariantError) else f"系统异常：{e}"
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE handovers SET status = '失败', reason = ?, finished_at = ? WHERE id = ?",
                (reason, _now(), handover_id),
            )
            conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (h["from_shift_id"],))
            conn.execute("UPDATE shifts SET status = '空闲' WHERE id = ?", (h["to_shift_id"],))
            _insert_report(conn, handover_id, h, consistent=0, extra={"失败原因": reason})
            conn.commit()
        finally:
            conn.isolation_level = ""
    return get_handover(handover_id)


def list_reconcile() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            d["一致"] = bool(d.pop("consistent"))
            out.append(d)
        return out
