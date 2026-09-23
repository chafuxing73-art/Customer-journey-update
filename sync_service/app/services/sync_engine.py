"""同步编排：拉运单 → 拉轨迹 → 映射 → 写企微。

策略：
1. 调接口1（业务员运单管理）拿 customerCode 下的所有运单
2. 并发调接口3（轨迹事件明细）拿每条运单的轨迹
3. 批量调接口2（物流轨迹管理）拿汇总（最近轨迹/船名航次）
4. 用 track_mapper 组装记录
5. 区分 add/update：本地有 record_id → update；没有 → add 并保存 record_id
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app import config
from app.services import db, jinlian_client, qiwai_client, track_mapper

logger = logging.getLogger(__name__)


# ===== 同客户并发同步防护（进程内集合 + DB 兜底）=====
_in_progress: set[str] = set()


class SyncInProgressError(Exception):
    """同一客户已有同步正在进行中。"""

    def __init__(self, customer_code: str) -> None:
        self.customer_code = customer_code
        super().__init__(f"客户 {customer_code} 正在同步中")


class SyncResult:
    def __init__(self, customer_code: str) -> None:
        self.customer_code = customer_code
        self.total = 0
        self.new = 0
        self.updated = 0
        self.skipped = 0  # 已签收 + 已有企微记录 → 跳过更新
        self.failed = 0
        self.errors: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer_code": self.customer_code,
            "total": self.total,
            "new": self.new,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
        }


class SyncEngine:
    def __init__(
        self,
        jinlian: jinlian_client.JinlianClient | None = None,
        qiwai: qiwai_client.QiwaiClient | None = None,
    ) -> None:
        self.jinlian = jinlian or jinlian_client.JinlianClient()
        self.qiwai = qiwai or qiwai_client.QiwaiClient()

    def _build_date_range(
        self, customer_cfg: dict[str, Any] | None = None
    ) -> tuple[str, str]:
        """生成 startDate / endDate（北京时间格式 yyyy/MM/dd HH:mm:ss）。

        查询客户所有运单：起始日期设为 2000/01/01，确保覆盖全部历史运单
        （包括早期运单的入仓时间/签收时间等节点），不受 SYNC_LOOKBACK_DAYS 限制。
        """
        cn_tz = timezone(timedelta(hours=8))
        now_utc = datetime.now(timezone.utc)
        # 起始日期设为很早，覆盖全部历史运单
        start_cn = datetime(2000, 1, 1, tzinfo=cn_tz)
        end_cn = now_utc.astimezone(cn_tz).replace(hour=23, minute=59, second=59)
        return (
            start_cn.strftime("%Y/%m/%d %H:%M:%S"),
            end_cn.strftime("%Y/%m/%d %H:%M:%S"),
        )

    @staticmethod
    def _is_signed(summary: dict[str, Any] | None, track_data: dict[str, Any] | None) -> bool:
        """判断运单是否已签收。

        满足任一即为已签收：
        1. 汇总 latestTrajectoryStatus 在已签收状态集合中
        2. 轨迹明细中含签收节点 CODE（CC / CY11）
        """
        # 依据1：汇总 latestTrajectoryStatus
        if summary:
            status = (summary.get("latestTrajectoryStatus") or "").strip().upper()
            if status and status in config.SIGNED_LATEST_STATUS:
                return True

        # 依据2：轨迹明细里有签收节点 CODE
        trajectory = (track_data or {}).get("trajectory") or []
        for ev in trajectory:
            code = (ev.get("code") or "").strip().upper()
            if code in config.SIGNED_TRACK_CODES:
                return True
            ev_status = (ev.get("status") or "").strip().upper()
            if ev_status and ev_status in config.SIGNED_TRACK_STATUS:
                return True

        return False

    async def sync_customer(
        self,
        customer_code: str,
        triggered_by: str = "system",
        trigger_source: str = "scheduled",
    ) -> SyncResult:
        result = SyncResult(customer_code)
        started_at = datetime.now(timezone.utc).isoformat()
        # 并发锁（进程内集合 + DB 兜底，防同客户并发同步）
        if customer_code in _in_progress:
            raise SyncInProgressError(customer_code)
        _in_progress.add(customer_code)
        if not db.acquire_db_lock(customer_code, locked_by=triggered_by):
            _in_progress.discard(customer_code)
            raise SyncInProgressError(customer_code)
        try:
            await self._sync_customer_impl(result, customer_code)
        finally:
            _in_progress.discard(customer_code)
            db.release_db_lock(customer_code)
            finished_at = datetime.now(timezone.utc).isoformat()
            db.insert_sync_history(
                customer_code=customer_code,
                triggered_by=triggered_by,
                trigger_source=trigger_source,
                started_at=started_at,
                finished_at=finished_at,
                success=(result.failed == 0),
                total=result.total,
                new=result.new,
                updated=result.updated,
                skipped=result.skipped,
                failed=result.failed,
                errors=result.errors,
            )
        return result

    async def _sync_customer_impl(
        self, result: SyncResult, customer_code: str
    ) -> SyncResult:
        """同步主体（已被 sync_customer 的锁/历史包装）。"""
        customer_cfg = config.get_customer(customer_code)
        if not customer_cfg:
            result.errors.append(f"未找到客户配置: {customer_code}")
            return result
        if not customer_cfg.get("enabled", True):
            result.errors.append(f"客户已禁用: {customer_code}")
            return result

        # 确保版本号已初始化（首次同步时拉取一次）
        await self.jinlian.ensure_version()

        webhook_url = customer_cfg["webhook_url"]
        start_date, end_date = self._build_date_range(customer_cfg)
        logger.info(
            "开始同步客户 %s, 时间范围 %s ~ %s",
            customer_code, start_date, end_date,
        )

        # ===== 1. 拉运单列表 =====
        try:
            waybills = await self.jinlian.get_all_waybills(
                customer_code, start_date, end_date
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("拉取运单失败")
            result.errors.append(f"拉取运单失败: {e}")
            return result

        # 1.1 按运单号去重（锦联翻页边界可能返回重复）
        seen_nos: set[str] = set()
        deduped: list[dict[str, Any]] = []
        dup_count = 0
        for wb in waybills:
            no = wb.get("serviceOrderNumber")
            if no and no not in seen_nos:
                seen_nos.add(no)
                deduped.append(wb)
            elif no:
                dup_count += 1
        if dup_count:
            logger.warning("锦联 API 返回 %d 条重复运单，已去重", dup_count)
            result.errors.append(f"锦联 API 返回 {dup_count} 条重复运单，已去重")
        waybills = deduped

        result.total = len(waybills)
        logger.info("客户 %s 拉到 %d 条运单（去重后）", customer_code, len(waybills))
        if not waybills:
            return result

        # ===== 2. 拉汇总（提前到轨迹明细之前，用于判断签收状态，避免已签收运单仍拉轨迹）=====
        waybill_numbers = [
            w.get("serviceOrderNumber") for w in waybills if w.get("serviceOrderNumber")
        ]
        summary_map: dict[str, dict[str, Any]] = {}
        try:
            summaries = await self.jinlian.get_waybill_summary(waybill_numbers)
            summary_map = {
                s.get("serviceOrderNumber"): s
                for s in summaries
                if s.get("serviceOrderNumber")
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("拉取汇总失败（忽略，签收判断将仅依赖轨迹明细）: %s", e)

        # ===== 2.1 预过滤：已签收 + 本地有 record_id → 跳过（不拉轨迹明细、不更新企微）=====
        active_waybills: list[dict[str, Any]] = []
        for wb in waybills:
            wb_no = wb.get("serviceOrderNumber") or ""
            summary = summary_map.get(wb_no)
            # 先靠汇总判断签收（不拉轨迹明细即可判断，性能最高）
            if summary:
                status = (summary.get("latestTrajectoryStatus") or "").strip().upper()
                is_signed = bool(status and status in config.SIGNED_LATEST_STATUS)
                if is_signed and qiwai_client.get_record_id(customer_code, wb_no):
                    result.skipped += 1
                    logger.debug("跳过已签收运单 %s（状态=%s，已有企微记录）", wb_no, status)
                    continue
            active_waybills.append(wb)

        if result.skipped:
            logger.info(
                "已签收运单预过滤跳过 %d 条，剩余 %d 条需要处理",
                result.skipped, len(active_waybills),
            )

        # ===== 3. 对剩余运单批量调轨迹事件明细 =====
        waybill_ids = [w["id"] for w in active_waybills if w.get("id")]
        track_map = await self.jinlian.get_track_events_batch(waybill_ids)

        # ===== 4. 组装记录，区分 add/update =====
        add_payloads: list[dict[str, Any]] = []
        add_waybill_nos: list[str] = []  # 与 add_payloads 对齐，用于回填 record_id
        update_payloads: list[dict[str, Any]] = []

        # 批次内已处理运单号（防止同一批次内重复加入 add —— 此时 save_record_id 还没执行）
        batch_seen: set[str] = set()

        for wb in active_waybills:
            wb_no = wb.get("serviceOrderNumber") or ""
            if not wb_no:
                result.failed += 1
                result.errors.append(f"运单缺少 serviceOrderNumber, id={wb.get('id')}")
                continue

            # 批次内去重（保险：锦联 API 已去重，但防止边界情况）
            if wb_no in batch_seen:
                logger.warning("批次内重复运单号，跳过: %s", wb_no)
                continue
            batch_seen.add(wb_no)

            wb_id = wb.get("id")
            track_data = track_map.get(wb_id, {})
            summary = summary_map.get(wb_no)

            # 二次校验：轨迹明细也能判断签收（汇总可能漏判）
            existing_record_id = qiwai_client.get_record_id(customer_code, wb_no)
            if existing_record_id and self._is_signed(summary, track_data):
                result.skipped += 1
                logger.debug(
                    "跳过已签收运单 %s（轨迹确认已签收，已有企微记录）", wb_no,
                )
                continue

            rpa_err = track_data.get("_error", "") if track_data else ""
            try:
                values = track_mapper.build_smartsheet_record(
                    wb, summary, track_data, rpa_error=rpa_err
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("组装记录失败 %s", wb_no)
                result.failed += 1
                result.errors.append(f"组装记录失败 {wb_no}: {e}")
                continue

            if existing_record_id:
                update_payloads.append({
                    "record_id": existing_record_id,
                    "values": values,
                })
            else:
                add_payloads.append(values)
                add_waybill_nos.append(wb_no)

        # ===== 5. 调 webhook 写入 =====
        # 5.1 add
        if add_payloads:
            try:
                record_ids = await self.qiwai.add_records(webhook_url, add_payloads)

                # 校验返回数量
                if len(record_ids) != len(add_payloads):
                    result.errors.append(
                        f"add 返回 record_id 数量不符: 请求 {len(add_payloads)} 条, "
                        f"返回 {len(record_ids)} 条; 后面的运单号映射可能丢失!"
                    )
                    logger.warning(
                        "add 返回数量不符: 请求 %d, 返回 %d; 后半段映射将缺失",
                        len(add_payloads), len(record_ids),
                    )

                # 逐条保存映射（校验 record_id 非空）
                saved_count = 0
                missing_ids: list[str] = []
                for wb_no, rid in zip(add_waybill_nos, record_ids):
                    if rid:
                        qiwai_client.save_record_id(customer_code, wb_no, rid)
                        saved_count += 1
                    else:
                        missing_ids.append(wb_no)
                        logger.warning("运单 %s add 返回空 record_id，未保存映射", wb_no)

                result.new = saved_count
                if missing_ids:
                    result.errors.append(
                        f"以下 {len(missing_ids)} 个运单 add 返回空 record_id，未保存映射: "
                        + ", ".join(missing_ids[:10])
                        + ("..." if len(missing_ids) > 10 else "")
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception("add_records 失败")
                result.failed += len(add_payloads)
                result.errors.append(f"add_records 失败: {e}")

        # 5.2 update
        if update_payloads:
            try:
                count = await self.qiwai.update_records(webhook_url, update_payloads)
                result.updated = count
                if count != len(update_payloads):
                    result.errors.append(
                        f"update 部分失败: 期望 {len(update_payloads)} 条，实际 {count} 条"
                    )
            except Exception as e:  # noqa: BLE001
                err_msg = str(e)
                logger.warning("update_records 批量失败，尝试转为 add 重试: %s", err_msg)

                # record_id 失效（record not exists）→ 删除本地映射，转 add 重试
                if "record not exists" in err_msg or "2022003" in err_msg:
                    retry_add_payloads: list[dict[str, Any]] = []
                    retry_add_nos: list[str] = []
                    for upd in update_payloads:
                        # 从 update_payload 反查运单号
                        wb_no = next(
                            (k for k, v in upd.get("values", {}).items()
                             if k == config.F_WAYBILL_NO),
                            "",
                        )
                        # values 里的运单号是 list 结构，取 text
                        wb_val = upd.get("values", {}).get(config.F_WAYBILL_NO)
                        if isinstance(wb_val, list) and wb_val:
                            wb_no = wb_val[0].get("text", "") if isinstance(wb_val[0], dict) else str(wb_val[0])
                        elif isinstance(wb_val, str):
                            wb_no = wb_val

                        if wb_no:
                            qiwai_client.delete_record_id(customer_code, wb_no)
                            retry_add_payloads.append(upd["values"])
                            retry_add_nos.append(wb_no)

                    if retry_add_payloads:
                        try:
                            record_ids = await self.qiwai.add_records(webhook_url, retry_add_payloads)
                            saved = 0
                            for wb_no, rid in zip(retry_add_nos, record_ids):
                                if rid:
                                    qiwai_client.save_record_id(customer_code, wb_no, rid)
                                    saved += 1
                            result.new += saved
                            result.updated = 0
                            logger.info(
                                "update 转 add 重试成功: %d 条重新新增", saved
                            )
                            if len(record_ids) != len(retry_add_payloads):
                                result.errors.append(
                                    f"转 add 重试返回数量不符: 请求 {len(retry_add_payloads)}, "
                                    f"返回 {len(record_ids)}"
                                )
                        except Exception as e2:  # noqa: BLE001
                            logger.exception("转 add 重试也失败")
                            result.failed += len(update_payloads)
                            result.errors.append(f"update 转 add 重试失败: {e2}")
                    else:
                        result.failed += len(update_payloads)
                        result.errors.append(f"update 失败且无法解析运单号: {e}")
                else:
                    logger.exception("update_records 失败")
                    result.failed += len(update_payloads)
                    result.errors.append(f"update_records 失败: {e}")

        logger.info(
            "同步完成 客户 %s: total=%d new=%d updated=%d skipped=%d failed=%d",
            customer_code, result.total, result.new, result.updated, result.skipped, result.failed,
        )
        return result


# 单例
_engine: SyncEngine | None = None


def get_engine() -> SyncEngine:
    global _engine
    if _engine is None:
        _engine = SyncEngine()
    return _engine
