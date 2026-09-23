"""企微智能表格 webhook 客户端。

企微 webhook 仅支持 add_records / update_records 两种操作（不支持查询）。
- add_records 成功时响应体里会返回每条记录的 record_id
- update_records 需要预先知道 record_id（由本地映射表查找）

「运单号只出现一次 更新」的实现：
- 本地 SQLite 存 (customer_code, waybill_no) → record_id 映射
- 同步时：本地有 record_id → update；没有 → add，并保存返回的 record_id
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from app import config

logger = logging.getLogger(__name__)


class QiwaiClientError(Exception):
    pass


# ===== 本地 record_id 映射存储（SQLite）=====
DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "record_map.db"


def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS waybill_record_map (
                customer_code TEXT NOT NULL,
                waybill_no    TEXT NOT NULL,
                record_id     TEXT NOT NULL,
                last_sync_at   TEXT NOT NULL,
                PRIMARY KEY (customer_code, waybill_no)
            )
            """
        )


def get_record_id(customer_code: str, waybill_no: str) -> str | None:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT record_id FROM waybill_record_map WHERE customer_code=? AND waybill_no=?",
            (customer_code, waybill_no),
        )
        row = cur.fetchone()
        return row[0] if row else None


def save_record_id(customer_code: str, waybill_no: str, record_id: str) -> None:
    _ensure_db()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO waybill_record_map
                (customer_code, waybill_no, record_id, last_sync_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(customer_code, waybill_no)
            DO UPDATE SET record_id=excluded.record_id, last_sync_at=excluded.last_sync_at
            """,
            (customer_code, waybill_no, record_id, now),
        )
        conn.commit()


def get_existing_waybills(customer_code: str) -> dict[str, str]:
    """返回 {waybill_no: record_id}。"""
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT waybill_no, record_id FROM waybill_record_map WHERE customer_code=?",
            (customer_code,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def delete_record_id(customer_code: str, waybill_no: str) -> None:
    """删除本地映射（record_id 失效时调用，便于下次同步重新 add）。"""
    _ensure_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM waybill_record_map WHERE customer_code=? AND waybill_no=?",
            (customer_code, waybill_no),
        )
        conn.commit()


# ===== webhook HTTP 调用 =====
class QiwaiClient:
    """企微智能表格 webhook 客户端。"""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def _post(self, webhook_url: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=config.HTTP_VERIFY_SSL) as client:
            resp = await client.post(webhook_url, json=body)
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise QiwaiClientError(
                f"webhook 响应非 JSON: {e}; 状态={resp.status_code}; 原文={resp.text[:300]}"
            ) from e
        if data.get("errcode") != 0:
            raise QiwaiClientError(
                f"webhook 调用失败: errcode={data.get('errcode')}; "
                f"errmsg={data.get('errmsg')}"
            )
        return data

    async def add_records(
        self, webhook_url: str, records: list[dict[str, Any]]
    ) -> list[str]:
        """新增记录，返回 record_id 列表（顺序与 records 对齐）。

        企微 webhook 单次 add 上限 500 条，超过自动分批。
        """
        if not records:
            return []
        BATCH = 500
        all_ids: list[str] = []
        for i in range(0, len(records), BATCH):
            batch = records[i:i + BATCH]
            body = {"add_records": [{"values": v} for v in batch]}
            data = await self._post(webhook_url, body)
            added = data.get("add_records") or []
            all_ids.extend(r.get("record_id", "") for r in added)
        return all_ids

    async def update_records(
        self,
        webhook_url: str,
        updates: list[dict[str, Any]],
    ) -> int:
        """更新记录。updates = [{"record_id": "...", "values": {...}}, ...]。返回更新条数。

        企微 webhook 单次 update 上限 500 条，超过自动分批。
        """
        if not updates:
            return 0
        BATCH = 500
        total = 0
        for i in range(0, len(updates), BATCH):
            batch = updates[i:i + BATCH]
            body = {"update_records": batch}
            data = await self._post(webhook_url, body)
            updated = data.get("update_records") or []
            total += len(updated)
        return total

    async def webhook_probe(self, webhook_url: str) -> dict[str, Any]:
        """探测 webhook 可用性（零脏数据）。

        企微 webhook 不支持 query/delete，发 add 会产生脏记录。
        这里发空 body 的 POST：企微会返回 errcode != 0 但不写记录，
        根据 errmsg 区分 key 无效（invalid key）vs key 有效但参数错误。
        """
        import httpx
        reachable = False
        valid_key = False
        errcode: int | None = None
        errmsg = ""
        try:
            async with httpx.AsyncClient(
                timeout=15.0, verify=config.HTTP_VERIFY_SSL
            ) as client:
                resp = await client.post(webhook_url, json={})
            reachable = True
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                errmsg = f"响应非 JSON: {resp.text[:200]}"
                return {
                    "reachable": reachable, "valid_key": False,
                    "errcode": None, "errmsg": errmsg,
                }
            errcode = data.get("errcode")
            errmsg = data.get("errmsg", "")
            # errcode==0 极少（空 body 一般参数错误）；key 无效时 errcode 通常非 0 且 errmsg 含 invalid
            msg_lower = (errmsg or "").lower()
            if errcode == 0:
                valid_key = True
            elif "invalid" in msg_lower or "key" in msg_lower:
                valid_key = False
            else:
                # 参数错误（如缺 records）说明 key 有效、URL 可达
                valid_key = True
        except Exception as e:  # noqa: BLE001
            errmsg = f"请求失败: {e}"
        return {
            "reachable": reachable,
            "valid_key": valid_key,
            "errcode": errcode,
            "errmsg": errmsg,
        }
