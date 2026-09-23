"""配置加载：.env + customers.yaml + 字段映射常量。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # sync_service/
ENV_PATH = BASE_DIR / ".env"
CUSTOMERS_PATH = BASE_DIR / "customers.yaml"


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)


def _load_customers() -> dict[str, dict[str, Any]]:
    if not CUSTOMERS_PATH.exists():
        return {}
    with CUSTOMERS_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("customers", {}) or {}


_load_env()

# ===== ERP 配置 =====
ERP_BASE_URL = os.getenv("EXTERNAL_ERP_BASE_URL", "https://prod.jlfba.com")
ERP_TOKEN = os.getenv("EXTERNAL_ERP_TOKEN", "")
ERP_MAC_ADDR = os.getenv("EXTERNAL_ERP_MAC_ADDR", "")
ERP_USERNAME = os.getenv("EXTERNAL_ERP_USERNAME", "")
ERP_PASSWORD_MD5 = os.getenv("EXTERNAL_ERP_PASSWORD_MD5", "")
# 锦联客户端版本号获取地址（启动时拉取，用于 erpversion header 和 user-agent）
ERP_VERSION_URL = os.getenv(
    "ERP_VERSION_URL",
    "https://jl-test-buket.oss-cn-beijing.aliyuncs.com/version/latest.yml",
)
ERP_DEFAULT_VERSION = "1.1.5"  # 拉取失败时的兜底版本

# ===== 企微 API（预留） =====
QWAI_CORP_ID = os.getenv("QWAI_CORP_ID", "")
QWAI_AGENT_ID = os.getenv("QWAI_AGENT_ID", "")
QWAI_SECRET = os.getenv("QWAI_SECRET", "")

# ===== 用户与鉴权 =====
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "7"))
# 启动时强制校验：JWT_SECRET 缺失或仍为占位符则拒绝启动
if not JWT_SECRET or JWT_SECRET == "please-change-this-to-a-random-32-byte-string":
    raise RuntimeError(
        "JWT_SECRET 未配置或仍为占位符，请在 .env 中设置一个随机密钥"
    )
if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_USERNAME / ADMIN_PASSWORD 未配置，无法 bootstrap 管理员")

# ===== 同步默认参数 =====
SYNC_DEFAULT_PAGE_SIZE = int(os.getenv("SYNC_DEFAULT_PAGE_SIZE", "500"))
SYNC_LOOKBACK_DAYS = int(os.getenv("SYNC_LOOKBACK_DAYS", "30"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# 是否校验 HTTPS 证书；本地开发可设为 false（生产建议 true）
HTTP_VERIFY_SSL = os.getenv("HTTP_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

# ===== 已签收状态过滤 =====
# 汇总接口 latestTrajectoryStatus 中表示"已签收/已完成"的值（大写匹配）
# 在 sync_engine 中，如果汇总返回的状态在以下列表中，且本地已有 record_id，则跳过更新
SIGNED_LATEST_STATUS = {
    "SIGNED",
    "SIGNED_AND_DELIVERED",
    "DELIVERED",
    "DELIVERED_TO_SIGN",
    "COMPLETED",
    "FINISHED",
    "RECEIVED",
}
# 轨迹明细中签收节点的 CODE（与 track_mapper.py CODES_SIGN 保持一致）
SIGNED_TRACK_CODES = {"CC", "CY11"}
# 轨迹节点 status 字段中表示签收的值
SIGNED_TRACK_STATUS = {"SIGNED", "DELIVERED", "COMPLETED", "FINISHED", "RECEIVED"}

# ===== 客户配置 =====
CUSTOMERS: dict[str, dict[str, Any]] = _load_customers()

# 兜底 webhook：yaml 里没配的客户也能同步到这张表（测试/临时多客户场景）
DEFAULT_WEBHOOK_URL = (
    "https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook?key="
    "6syVifHSIQ9eRrNKzCZb0Mz5VjQqocLdmPo8TOmcLyCMF4KNYadptoFnu7q6veFg9AfQZSN15WvVA5q86EeRp3DqiUXW8YyLfJcu09AgNhj3"
)


def get_customer(customer_code: str) -> dict[str, Any] | None:
    """按客户代码返回 DB 中的客户配置。

    客户必须在 DB 中存在（带 owner_user_id）才能被同步；未配置返回 None。
    enabled=false 返回 None（禁止同步）。调用方同步时还需通过 auth 权限校验。
    """
    # 懒加载避免与 db.py 的循环导入
    from app.services import db
    cust = db.get_customer(customer_code)
    if not cust:
        return None
    if not cust.get("enabled", True):
        return None
    return cust


def list_customers() -> list[dict[str, Any]]:
    """返回 DB 中所有客户配置（带 code 字段）。"""
    from app.services import db
    return db.list_customers()


def reload_customers() -> None:
    """DB 为运行时唯一数据源，CRUD 直接写库，无需 reload。保留为 no-op 兼容旧调用。"""
    return None


# ===== 企微智能表格字段 schema（field_id → 标题）=====
# 用于构造 webhook 的 add_records 请求体
SMARTSHEET_FIELDS: dict[str, dict[str, str]] = {
    "fabcde": {"title": "入仓时间", "type": "date_time"},
    "fJgh00": {"title": "运单号（不变）", "type": "text"},
    "fw6Isp": {"title": "FBA号（不变）", "type": "text"},
    "fjEoFy": {"title": "转单号（不变）", "type": "text"},
    "fGVpNq": {"title": "国家（不变）", "type": "text"},
    "fUsMKf": {"title": "件数（不变）", "type": "number"},
    "fn4id8": {"title": "销售产品（不变）", "type": "text"},
    "ftQ4BF": {"title": "fba仓库代码（不变）", "type": "text"},
    "fFEeVu": {"title": "最近轨迹（变化）", "type": "text"},
    "fuPniO": {"title": "航班起飞/开船/发车（确定后不变）", "type": "date_time"},
    "fcx09Y": {"title": "航班落地/到港/到站（确定后不变）", "type": "date_time"},
    "fp2Nnm": {"title": "清关时间（确定后不变）", "type": "date_time"},
    "fL8UUV": {"title": "提柜/交付（确定后不变）", "type": "date_time"},
    "fwFZbt": {"title": "签收日期（确定后不变）", "type": "date_time"},
    "fMHIlA": {"title": "异常备注", "type": "text"},
    "ffYpJ0": {"title": "查验/甩柜（确认后不变）", "type": "text"},
    "fEM5Y4": {"title": "船名航次", "type": "text"},
    "fnfy28": {"title": "POD", "type": "text"},
    "fOfJjR": {"title": "RPA报错", "type": "text"},
}

# field_id 常量（便于代码引用，避免硬编码字符串拼错）
F_WAREHOUSE_TIME = "fabcde"
F_WAYBILL_NO = "fJgh00"
F_FBA_NO = "fw6Isp"
F_TRANSFER_NO = "fjEoFy"
F_COUNTRY = "fGVpNq"
F_QUANTITY = "fUsMKf"
F_PRODUCT = "fn4id8"
F_FBA_WAREHOUSE = "ftQ4BF"
F_LATEST_TRACK = "fFEeVu"
F_DEPART = "fuPniO"
F_ARRIVE = "fcx09Y"
F_CLEARANCE = "fp2Nnm"
F_DELIVERY = "fL8UUV"
F_SIGN = "fwFZbt"
F_ABNORMAL = "fMHIlA"
F_INSPECTION = "ffYpJ0"
F_VESSEL_VOYAGE = "fEM5Y4"
F_POD = "fnfy28"
F_RPA_ERROR = "fOfJjR"
