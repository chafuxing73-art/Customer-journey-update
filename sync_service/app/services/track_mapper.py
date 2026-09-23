"""轨迹事件 CODE → 节点时间映射引擎。

CODE 定义（来自 SOP）：
- 起飞/开船/发车: DA, BM02, DDP01
- 落地/到港/到站: AA, CY04
- 清关: BM03, K2, RD
- 提柜/交付: JL25, CY06, X4, FN
- 签收: CC, CY11（CY11 要求 content 含 "ISA"）
- 查验/甩柜: CY→查验，CY05→甩柜，文本填入 content

特殊规则：有提柜但无清关 → 清关时间 = 提柜时间（按日）。
日期统一保留到日（UTC 当日 00:00:00 的毫秒时间戳）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app import config

logger = logging.getLogger(__name__)


# ===== CODE 集合 =====
CODES_DEPART = {"DA", "BM02", "DDP01"}
CODES_ARRIVE = {"AA", "CY04"}
CODES_CLEARANCE = {"BM03", "K2", "RD"}
CODES_DELIVERY = {"JL25", "CY06", "X4", "FN"}
CODES_SIGN = {"CC", "CY11"}


def _to_utc_day_ms(time_str: str | None) -> int | None:
    """ISO8601 时间字符串 → UTC 当日 00:00:00 的毫秒时间戳。

    '2026-09-15T05:46:17.000Z' → 1742272000000（2026-09-15 UTC 0点 ms）
    失败返回 None。
    """
    if not time_str:
        return None
    s = time_str.strip()
    try:
        # 兼容带/不带毫秒、带/不带Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day = dt.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return int(day.timestamp() * 1000)
    except Exception as e:  # noqa: BLE001
        logger.debug("时间解析失败 %r: %s", time_str, e)
        return None


def _norm_code(code: str | None) -> str:
    """归一化 CODE（去空格、大写）。"""
    if not code:
        return ""
    return code.strip().upper()


def map_track_to_nodes(
    trajectory: list[dict[str, Any]],
    *,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从轨迹事件数组提取节点时间，应用特殊规则。

    返回 dict，键为企微字段 field_id：
        F_DEPART, F_ARRIVE, F_CLEARANCE, F_DELIVERY, F_SIGN,
        F_INSPECTION, F_LATEST_TRACK, F_VESSEL_VOYAGE
    值：date_time 字段为 ms 时间戳（int）或 None；text 字段为字符串。
    """
    result: dict[str, Any] = {
        config.F_DEPART: None,
        config.F_ARRIVE: None,
        config.F_CLEARANCE: None,
        config.F_DELIVERY: None,
        config.F_SIGN: None,
        config.F_INSPECTION: "",
        config.F_LATEST_TRACK: "",
        config.F_VESSEL_VOYAGE: "",
    }

    inspection_texts: list[str] = []
    latest_content: str = ""

    for ev in trajectory or []:
        code = _norm_code(ev.get("code"))
        content = (ev.get("content") or "").strip()
        time_str = ev.get("time")
        ms = _to_utc_day_ms(time_str)

        # 最近轨迹：取 isLatest=true 的 content
        if ev.get("isLatest") and content:
            latest_content = content

        # 节点映射（取最早的一个，因为同一 CODE 可能出现多次）
        if code in CODES_DEPART and result[config.F_DEPART] is None and ms is not None:
            result[config.F_DEPART] = ms
        elif code in CODES_ARRIVE and result[config.F_ARRIVE] is None and ms is not None:
            result[config.F_ARRIVE] = ms
        elif code in CODES_CLEARANCE and result[config.F_CLEARANCE] is None and ms is not None:
            result[config.F_CLEARANCE] = ms
        elif code in CODES_DELIVERY and result[config.F_DELIVERY] is None and ms is not None:
            result[config.F_DELIVERY] = ms
        elif code in CODES_SIGN and result[config.F_SIGN] is None and ms is not None:
            # CY11 要求 content 含 "ISA"
            if code == "CY11" and "ISA" not in content.upper():
                continue
            result[config.F_SIGN] = ms

        # 查验/甩柜
        if code == "CY" and content:
            inspection_texts.append(f"查验: {content}")
        elif code == "CY05" and content:
            inspection_texts.append(f"甩柜: {content}")

    # 特殊规则：有提柜但无清关 → 清关时间 = 提柜时间
    if (
        result[config.F_DELIVERY] is not None
        and result[config.F_CLEARANCE] is None
    ):
        result[config.F_CLEARANCE] = result[config.F_DELIVERY]
        logger.info(
            "应用特殊规则：有提柜无清关 → 清关时间=提柜时间=%s",
            result[config.F_DELIVERY],
        )

    # 拼装查验/甩柜文本
    if inspection_texts:
        result[config.F_INSPECTION] = "; ".join(inspection_texts)

    # 优先用 summary 的 latestTrajectoryContent / 船名航次
    if summary:
        s_latest = (summary.get("latestTrajectoryContent") or "").strip()
        if s_latest:
            latest_content = s_latest
        result[config.F_VESSEL_VOYAGE] = _build_vessel_voyage(summary)

    result[config.F_LATEST_TRACK] = latest_content
    return result


def _build_vessel_voyage(summary: dict[str, Any]) -> str:
    """从 summary.containerShip 提取船名航次，拼成 'vessel/voyage'。"""
    ships = summary.get("containerShip") or []
    if not ships:
        return ""
    first = ships[0] if isinstance(ships, list) else {}
    vv = first.get("vesselVoyage") or {}
    vessel = (vv.get("vessel") or "").strip()
    voyage = (vv.get("voyage") or "").strip()
    if vessel and voyage:
        return f"{vessel}/{voyage}"
    return vessel or voyage or ""


def build_smartsheet_record(
    waybill: dict[str, Any],
    summary: dict[str, Any] | None,
    track_data: dict[str, Any] | None,
    *,
    rpa_error: str = "",
) -> dict[str, Any]:
    """把运单 + 汇总 + 轨迹数据组装成企微 webhook 的单条记录 values。

    参数：
    - waybill: 接口1业务员运单管理返回的单行
    - summary: 接口2物流轨迹管理的单行（可为空）
    - track_data: 接口3轨迹事件明细 data（含 trajectory/orderStatus）
    - rpa_error: 同步过程中的错误信息（填入 F_RPA_ERROR）
    """
    # 处理 recipient.fbaWarehouseCode
    recipient = waybill.get("recipient") or {}
    fba_warehouse = (recipient.get("fbaWarehouseCode") or "").strip() if recipient else ""
    if not fba_warehouse:
        # 汇总接口的 recipient 可能也带
        if summary:
            s_recipient = summary.get("recipient") or {}
            fba_warehouse = (s_recipient.get("fbaWarehouseCode") or "").strip()

    nodes = map_track_to_nodes(
        (track_data or {}).get("trajectory", []),
        summary=summary,
    )

    # date_time 字段统一转字符串（毫秒时间戳）；为空则不传该键
    def _dt_str(ms: int | None) -> str | None:
        return str(ms) if ms is not None else None

    values: dict[str, Any] = {
        config.F_WAREHOUSE_TIME: _dt_str(_to_utc_day_ms(waybill.get("createdAt"))),
        config.F_WAYBILL_NO: waybill.get("serviceOrderNumber") or "",
        config.F_FBA_NO: waybill.get("fbaNo") or "",
        config.F_TRANSFER_NO: "",  # 用户决策：留空
        config.F_COUNTRY: waybill.get("destinationCountryName") or "",
        config.F_QUANTITY: waybill.get("quantity") or 0,
        config.F_PRODUCT: waybill.get("productSoldNameCN") or "",
        config.F_FBA_WAREHOUSE: fba_warehouse,
        config.F_LATEST_TRACK: nodes[config.F_LATEST_TRACK],
        config.F_DEPART: _dt_str(nodes[config.F_DEPART]),
        config.F_ARRIVE: _dt_str(nodes[config.F_ARRIVE]),
        config.F_CLEARANCE: _dt_str(nodes[config.F_CLEARANCE]),
        config.F_DELIVERY: _dt_str(nodes[config.F_DELIVERY]),
        config.F_SIGN: _dt_str(nodes[config.F_SIGN]),
        config.F_ABNORMAL: waybill.get("latestRemark") or "",
        config.F_INSPECTION: nodes[config.F_INSPECTION],
        config.F_VESSEL_VOYAGE: nodes[config.F_VESSEL_VOYAGE],
        config.F_POD: "",  # 用户决策：留空
        config.F_RPA_ERROR: rpa_error,
    }
    # 过滤值为 None 的 date_time 字段（企微 date_time 不接受空值）
    values = {k: v for k, v in values.items() if v is not None}
    return values
