#!/usr/bin/env python3
"""一键部署脚本：在服务器上运行，自动创建所有项目文件并启动 Docker。"""
import base64, os, sys

FILES = {}

# ===== 配置文件 =====
FILES["docker-compose.yml"] = b"""
services:
  sync-service:
    build: .
    container_name: sync-service
    restart: always
    ports:
      - "8000:8000"
    volumes:
      - ./.env:/app/.env:ro
      - ./customers.yaml:/app/customers.yaml:ro
      - ./data:/app/data
    environment:
      - TZ=Asia/Shanghai
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
"""

FILES["Dockerfile"] = b"""
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN mkdir -p data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

FILES["requirements.txt"] = b"""
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
httpx>=0.27.0
apscheduler>=3.10.4
pyyaml>=6.0.1
python-dotenv>=1.0.1
brotli>=1.1.0
python-multipart>=0.0.9
pyjwt>=2.8.0
bcrypt>=4.0.0
"""

FILES[".env"] = b"""
EXTERNAL_ERP_BASE_URL=https://prod.jlfba.com
EXTERNAL_ERP_TOKEN=64c0784bacc6c7d3
EXTERNAL_ERP_MAC_ADDR=03000200-0400-0500-0006-000700080009
EXTERNAL_ERP_USERNAME=11038
EXTERNAL_ERP_PASSWORD_MD5=e10adc3949ba59abbe56e057f20f883e
QWAI_CORP_ID=wwd5993be58eb1ecb3
QWAI_AGENT_ID=1000088
QWAI_SECRET=F7_7yVrKRkPikxHcl1WOs3dma66FxrBYPgLh28lpqRg
SYNC_DEFAULT_PAGE_SIZE=500
SYNC_LOOKBACK_DAYS=30
LOG_LEVEL=INFO
ADMIN_USERNAME=admin
ADMIN_PASSWORD=ChangeMe!2026
JWT_SECRET=vMPk2ViGj60dWJzhG1ff2LGlLnqmB3PBP5Dyb0hltpQ
JWT_EXPIRE_DAYS=7
"""

FILES["customers.yaml"] = b"""
customers:
  '8888275373':
    name: \u6d4b\u8bd5\u5ba2\u62378888275373
    webhook_url: https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook?key=6syVifHSIQ9eRrNKzCZb0Mz5VjQqocLdmPo8TOmcLyCMF4KNYadptoFnu7q6veFg9AfQZSN15WvVA5q86EeRp3DqiUXW8YyLfJcu09AgNhj3
    sync_mode: incremental
    schedule: 0 9,18 * * *
    enabled: true
  RTBTEST:
    name: \u5ba2\u6237-RTBTEST
    webhook_url: https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook?key=6syVifHSIQ9eRrNKzCZb0Mz5VjQqocLdmPo8TOmcLyCMF4KNYadptoFnu7q6veFg9AfQZSN15WvVA5q86EeRp3DqiUXW8YyLfJcu09AgNhj3
    sync_mode: incremental
    schedule: 0 9,18 * * *
    enabled: true
"""

# ===== Python 源码（base64 编码，避免特殊字符问题）=====
# app/__init__.py 和 app/routers/__init__.py 和 app/services/__init__.py 是空文件

def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")

FILES["app/__init__.py"] = b""
FILES["app/routers/__init__.py"] = b""
FILES["app/services/__init__.py"] = b""

# 我会把所有 Python 文件内容作为 base64 字符串存储
_python_files = {}

_python_files["app/config.py"] = _b64('''\
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
ERP_VERSION_URL = os.getenv(
    "ERP_VERSION_URL",
    "https://jl-test-buket.oss-cn-beijing.aliyuncs.com/version/latest.yml",
)
ERP_DEFAULT_VERSION = "1.1.5"

# ===== 企微 API（预留） =====
QWAI_CORP_ID = os.getenv("QWAI_CORP_ID", "")
QWAI_AGENT_ID = os.getenv("QWAI_AGENT_ID", "")
QWAI_SECRET = os.getenv("QWAI_SECRET", "")

# ===== 用户与鉴权 =====
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "7"))
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
HTTP_VERIFY_SSL = os.getenv("HTTP_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

# ===== 已签收状态过滤 =====
SIGNED_LATEST_STATUS = {
    "SIGNED",
    "SIGNED_AND_DELIVERED",
    "DELIVERED",
    "DELIVERED_TO_SIGN",
    "COMPLETED",
    "FINISHED",
    "RECEIVED",
}
SIGNED_TRACK_CODES = {"CC", "CY11"}
SIGNED_TRACK_STATUS = {"SIGNED", "DELIVERED", "COMPLETED", "FINISHED", "RECEIVED"}

# ===== 客户配置 =====
CUSTOMERS: dict[str, dict[str, Any]] = _load_customers()

DEFAULT_WEBHOOK_URL = (
    "https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook?key="
    "6syVifHSIQ9eRrNKzCZb0Mz5VjQqocLdmPo8TOmcLyCMF4KNYadptoFnu7q6veFg9AfQZSN15WvVA5q86EeRp3DqiUXW8YyLfJcu09AgNhj3"
)


def get_customer(customer_code: str) -> dict[str, Any] | None:
    from app.services import db
    cust = db.get_customer(customer_code)
    if not cust:
        return None
    if not cust.get("enabled", True):
        return None
    return cust


def list_customers() -> list[dict[str, Any]]:
    from app.services import db
    return db.list_customers()


def reload_customers() -> None:
    return None


# ===== 企微智能表格字段 schema =====
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
''')

# 其余 Python 文件也用同样方式...
# 由于文件较多，我把它们全部放在下面的 dict 中

_python_files["app/main.py"] = _b64(open(__file__.replace("deploy.py", "sync_service/app/main.py"), "r", encoding="utf-8").read()) if os.path.exists(__file__.replace("deploy.py", "sync_service/app/main.py")) else ""

# 这种方式不行，因为脚本在服务器上运行时没有本地文件。

print("ERROR: This approach won't work on the server.")
print("Please use the alternative method.")
sys.exit(1)
