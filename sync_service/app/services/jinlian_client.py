"""锦联 ERP 接口封装：3 个接口 + brotli 解压兜底 + 动态版本号 + 登录。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import brotli
import httpx

from app import config

logger = logging.getLogger(__name__)


class JinlianClientError(Exception):
    pass


# ===== 模块级缓存：最新版本号（进程内只拉一次）=====
_cached_version: str | None = None


async def fetch_latest_version() -> str:
    """从阿里云 OSS 拉取锦联国际最新版本号（YAML 中的 version 字段）。

    进程内缓存：首次调用拉取，后续直接返回缓存值。
    拉取失败返回兜底版本 ERP_DEFAULT_VERSION。
    """
    global _cached_version
    if _cached_version is not None:
        return _cached_version

    try:
        async with httpx.AsyncClient(
            timeout=10.0, verify=config.HTTP_VERIFY_SSL
        ) as client:
            # 加 noCache 参数防缓存
            url = f"{config.ERP_VERSION_URL}?noCache=1"
            resp = await client.get(url)
            text = resp.text
            # 简单解析 YAML 里的 version 行（避免依赖 PyYAML 的额外开销）
            for line in text.splitlines():
                if line.startswith("version:"):
                    ver = line.split(":", 1)[1].strip().strip("'\"")
                    if ver:
                        _cached_version = ver
                        logger.info("获取锦联最新版本号: %s", ver)
                        return ver
            logger.warning("版本文件中未找到 version 字段，使用兜底 %s", config.ERP_DEFAULT_VERSION)
    except Exception as e:  # noqa: BLE001
        logger.warning("获取锦联版本号失败，使用兜底 %s: %s", config.ERP_DEFAULT_VERSION, e)

    _cached_version = config.ERP_DEFAULT_VERSION
    return _cached_version


class JinlianClient:
    """锦联 ERP 接口客户端。

    3 个方法：
    - get_waybills(customer_code, start, end): 业务员运单管理（运单列表）
    - get_waybill_summary(waybill_numbers): 物流轨迹管理（汇总）
    - get_track_events(waybill_id): 轨迹事件明细（含 brotli 解压）

    token 优先级：显式传入 > .env 静态 token > 调用登录接口动态获取
    """

    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
        version: str | None = None,
    ) -> None:
        self.base_url = (base_url or config.ERP_BASE_URL).rstrip("/")
        self.token = token or config.ERP_TOKEN
        self._version = version  # None 表示待异步初始化
        # 如果没传 token，需要在 init 时同步登录一次（简化处理）
        if not self.token:
            self._login_sync()
        if not self.token:
            raise JinlianClientError(
                "ERP token 未配置且登录失败：请在 .env 中设置 EXTERNAL_ERP_TOKEN 或检查账号密码"
            )

    def _login_sync(self) -> None:
        """同步调用登录接口获取 token（仅在无静态 token 时使用）。"""
        if not (config.ERP_USERNAME and config.ERP_PASSWORD_MD5 and config.ERP_MAC_ADDR):
            logger.warning("登录所需账号/密码/mac 不全，跳过自动登录")
            return
        try:
            import asyncio as _aio
            # 在同步上下文里跑异步登录
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            sess = loop.run_until_complete(
                self._login_async(
                    config.ERP_USERNAME,
                    config.ERP_PASSWORD_MD5,
                    config.ERP_MAC_ADDR,
                )
            )
            if sess:
                self.token = sess
                logger.info("自动登录成功，获取到 token(sess)")
        except Exception as e:  # noqa: BLE001
            logger.warning("自动登录失败: %s", e)

    async def _login_async(
        self, username: str, password_md5: str, mac_addr: str
    ) -> str | None:
        """异步登录，返回 sess（token）。"""
        url = f"{self.base_url}/account/login"
        body = {
            "userName": username,
            "password": password_md5,
            "macAddr": mac_addr,
        }
        # 登录时用兜底版本号（此时 _version 可能还没初始化）
        headers = self._headers_raw(
            token="",
            version=config.ERP_DEFAULT_VERSION,
            json_body=True,
        )
        async with httpx.AsyncClient(
            timeout=15.0, verify=config.HTTP_VERIFY_SSL
        ) as client:
            resp = await client.post(url, json=body, headers=headers)
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("登录失败: code=%s msg=%s", data.get("code"), data.get("message"))
            return None
        sess = (data.get("data") or {}).get("sess")
        return sess

    def _headers_raw(
        self, *, token: str, version: str, json_body: bool = True
    ) -> dict[str, str]:
        """构造请求头（不依赖实例状态，便于登录前调用）。"""
        h = {
            "authorization": token,
            "sess": token,
            "erpversion": version,
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) JLGJ/{version} Chrome/150.0.7871.250 "
                "Electron/43.7.0 Safari/537.36"
            ),
            "accept": "application/json, text/plain, */*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "zh-CN",
            "cache-control": "no-cache",
        }
        if json_body:
            h["content-type"] = "application/json"
        return h

    def _headers(self, *, json_body: bool = True) -> dict[str, str]:
        version = self._version or config.ERP_DEFAULT_VERSION
        return self._headers_raw(
            token=self.token, version=version, json_body=json_body
        )

    async def ensure_version(self) -> None:
        """异步初始化版本号（启动时调用一次）。"""
        if self._version is None:
            self._version = await fetch_latest_version()
            logger.info("JinlianClient 初始化版本号: %s", self._version)

    async def _send_request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送 HTTP 请求并解析 JSON（不校验 code）。

        内部辅助方法：仅负责发请求 + 解压兜底 + JSON 解析。
        """
        async with httpx.AsyncClient(
            headers=self._headers(json_body=json_body is not None),
            timeout=30.0,
            verify=config.HTTP_VERIFY_SSL,
        ) as client:
            resp = await client.request(method, url, json=json_body, params=params)

        # httpx 已自动解压 gzip/deflate/br（前提是装了 brotli 库）。
        # 但某些情况下 content-encoding 仍标记为 br 而字节已是解压后的，
        # 此时 resp.json() 能直接成功；若失败再尝试手动 brotli 兜底。
        try:
            return resp.json()
        except Exception:
            raw = resp.content
            # 兜底：尝试手动 brotli 解压（仅在 httpx 未自动解压时有效）
            try:
                raw = brotli.decompress(raw)
            except Exception as e:  # noqa: BLE001
                logger.debug("brotli 兜底解压失败（可能已解压）: %s", e)
            text = raw.decode("utf-8", errors="replace")
            import json as _json
            try:
                return _json.loads(text)
            except Exception as e:  # noqa: BLE001
                raise JinlianClientError(
                    f"响应解析 JSON 失败: {e}; 原文前200字符: {text[:200]}"
                ) from e

    @staticmethod
    def _is_auth_expired(message: str | None) -> bool:
        """判断 ERP message 是否表示登录/token 过期（触发自动续期）。"""
        if not message:
            return False
        msg = message.strip()
        # 命中任一关键词即视为 token 失效，需要重新登录
        keywords = ("登录过期", "登录已过期", "登录失效", "未登录", "请重新登录", "token")
        return any(k in msg for k in keywords)

    async def _relogin_once(self) -> bool:
        """重新调用登录接口刷新 token。返回是否成功。

        依赖 .env 中配置的 ERP_USERNAME / ERP_PASSWORD_MD5 / ERP_MAC_ADDR。
        成功后直接更新 self.token，后续请求自动用新 token。
        """
        if not (config.ERP_USERNAME and config.ERP_PASSWORD_MD5 and config.ERP_MAC_ADDR):
            logger.warning("账号/密码/mac 不全，无法自动重新登录")
            return False
        try:
            new_token = await self._login_async(
                config.ERP_USERNAME, config.ERP_PASSWORD_MD5, config.ERP_MAC_ADDR
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("自动重新登录抛异常: %s", e)
            return False
        if not new_token:
            logger.warning("自动重新登录失败：登录接口未返回 sess")
            return False
        self.token = new_token
        logger.info("ERP token 已自动续期（重新登录成功）")
        return True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = await self._send_request(
            method, url, json_body=json_body, params=params
        )

        if data.get("code") != 0:
            msg = data.get("message") or ""
            # 登录/token 过期 → 自动重新登录并重试一次原请求
            if self._is_auth_expired(msg):
                logger.warning("ERP 提示登录过期（message=%s），尝试自动续期...", msg)
                if await self._relogin_once():
                    logger.info("续期成功，重试原请求: %s %s", method, path)
                    data = await self._send_request(
                        method, url, json_body=json_body, params=params
                    )
                    if data.get("code") != 0:
                        raise JinlianClientError(
                            f"ERP 续期后重试仍返回非零 code: {data.get('code')}; "
                            f"message={data.get('message')}"
                        )
                    return data
                # 续期失败 → 抛出更明确的错误，方便排查
                raise JinlianClientError(
                    f"ERP token 过期且自动重新登录失败；原错误 message={msg}"
                )
            raise JinlianClientError(
                f"ERP 返回非零 code: {data.get('code')}; message={data.get('message')}"
            )
        return data

    # ===== 接口1：业务员运单管理 =====
    async def get_waybills(
        self,
        customer_code: str,
        start_date: str,
        end_date: str,
        *,
        page: int = 1,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """返回 {count, rows:[...], pagination}。rows 中含 id, serviceOrderNumber, fbaNo 等。"""
        limit = limit or config.SYNC_DEFAULT_PAGE_SIZE
        body = {
            "numberNo": None,
            "numberType": "ORDER_NUMBER",
            "dateType": "CREATED_TIME",
            "warehouseStatus": [],
            "holdStatus": [],
            "operationStatus": [],
            "transferStatus": [],
            "customerId": [],
            "extraServices": [],
            "productSold": [],
            "categoryId": [],
            "channelServiceId": [],
            "postalCode": None,
            "packageType": [],
            "signInUserId": [],
            "signOutUserId": [],
            "salesmanId": [],
            "frontLineCustomerServiceId": [],
            "backLineCustomerServiceId": [],
            "printOrderStaffId": [],
            "deliveryMethodId": [],
            "destinationCountry": [],
            "billOfLadingNumber": None,
            "warehouseOrderNumber": None,
            "valueAddedTaxNumber": None,
            "trayNumber": None,
            "customerCode": [customer_code],
            "receivingPointId": None,
            "organizationId": None,
            "startDate": start_date,
            "endDate": end_date,
            "page": page,
            "limit": limit,
        }
        data = await self._request("POST", "/waybillManagement/business", json_body=body)
        return data.get("data", {}) or {}

    async def get_all_waybills(
        self,
        customer_code: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """自动翻页获取所有运单。"""
        all_rows: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self.get_waybills(
                customer_code, start_date, end_date, page=page
            )
            rows = data.get("rows", []) or []
            all_rows.extend(rows)
            pagination = data.get("pagination") or {}
            total_pages = pagination.get("totalPages", 1)
            if page >= total_pages or not rows:
                break
            page += 1
        return all_rows

    # ===== 接口2：物流轨迹管理（汇总） =====
    async def get_waybill_summary(
        self, waybill_numbers: list[str]
    ) -> list[dict[str, Any]]:
        """按运单号查汇总（latestTrajectoryContent / loadingTime / signTime / containerShip）。"""
        if not waybill_numbers:
            return []
        body = {
            "numberNo": waybill_numbers,
            "numberType": "ORDER_NUMBER",
            "dateType": "CREATED_TIME",
            "allocationNo": None,
            "billOfLadingNumber": None,
            "customerId": [],
            "orderStatus": [],
            "productSold": [],
            "channelServiceId": [],
            "extraServices": [],
            "destinationCountry": [],
            "productGroupId": [],
            "categoryId": [],
            "salesmanId": [],
            "frontLineCustomerServiceId": [],
            "backLineCustomerServiceId": [],
            "serviceCode": [],
            "latestTrajectoryStatus": [],
            "receivingPointId": [],
            "isAndQuery": True,
            "page": 1,
            "limit": config.SYNC_DEFAULT_PAGE_SIZE,
        }
        data = await self._request("POST", "/waybillManagement", json_body=body)
        return (data.get("data", {}) or {}).get("rows", []) or []

    # ===== 接口3：轨迹事件明细 =====
    async def get_track_events(
        self, waybill_id: int
    ) -> dict[str, Any]:
        """返回 {orderStatus, id, trajectory: [{time, location, content, code, isLatest, ...}]}。"""
        path = f"/waybillManagement/track/{waybill_id}"
        params = {"perspective": "isCustomer"}
        data = await self._request("GET", path, params=params)
        return data.get("data", {}) or {}

    async def get_track_events_batch(
        self, waybill_ids: list[int], concurrency: int = 5
    ) -> dict[int, dict[str, Any]]:
        """并发拉取多个运单的轨迹事件，返回 {waybill_id: trajectory_data}。"""
        sem = asyncio.Semaphore(concurrency)

        async def _one(wid: int) -> tuple[int, dict[str, Any]]:
            async with sem:
                try:
                    data = await self.get_track_events(wid)
                    return wid, data
                except Exception as e:  # noqa: BLE001
                    logger.warning("获取轨迹失败 waybill_id=%s: %s", wid, e)
                    return wid, {"trajectory": [], "_error": str(e)}

        results = await asyncio.gather(*[_one(w) for w in waybill_ids])
        return dict(results)
