"""应用权限域 SQLite 存储（app.db）。

职责：
- 建表（users / customers / user_customer_assignments / sync_history / audit_log / sync_locks）
- 用户 CRUD、客户 CRUD、客户分配、同步历史、审计日志
- 首次启动 bootstrap admin、从 customers.yaml 迁移种子数据

与 qiwai_client.record_map.db 分离：前者是同步映射域（高频读写、与权限无关），
本文件是权限/运营域。两个 DB 独立，避免锁竞争。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from app import config

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "app.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ===== 表结构初始化 =====
def init_db() -> None:
    """建所有表（幂等）。"""
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT    NOT NULL UNIQUE,
                password_hash   TEXT    NOT NULL,
                role            TEXT    NOT NULL DEFAULT 'user',
                enabled         INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT    NOT NULL,
                last_login_at   TEXT,
                pwd_changed_at  TEXT    NOT NULL,
                reset_token     TEXT,
                reset_expires_at TEXT
            );

            CREATE TABLE IF NOT EXISTS customers (
                code            TEXT    PRIMARY KEY,
                name            TEXT,
                webhook_url     TEXT    NOT NULL,
                sync_mode       TEXT    NOT NULL DEFAULT 'incremental',
                schedule        TEXT,
                enabled         INTEGER NOT NULL DEFAULT 1,
                owner_user_id   INTEGER NOT NULL REFERENCES users(id),
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_customer_assignments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                customer_code   TEXT    NOT NULL REFERENCES customers(code),
                assigned_by     INTEGER REFERENCES users(id),
                assigned_at     TEXT    NOT NULL,
                UNIQUE(user_id, customer_code)
            );

            CREATE TABLE IF NOT EXISTS sync_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_code   TEXT    NOT NULL,
                triggered_by    TEXT,
                trigger_source  TEXT    NOT NULL,
                total           INTEGER,
                new             INTEGER,
                updated         INTEGER,
                skipped         INTEGER,
                failed          INTEGER,
                errors          TEXT,
                started_at      TEXT    NOT NULL,
                finished_at     TEXT,
                success         INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_sync_history_code_time
                ON sync_history(customer_code, started_at DESC);

            CREATE TABLE IF NOT EXISTS audit_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                actor           TEXT    NOT NULL,
                actor_role      TEXT,
                action          TEXT    NOT NULL,
                target          TEXT,
                detail          TEXT,
                created_at      TEXT    NOT NULL,
                ip              TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_log_time
                ON audit_log(created_at DESC);

            CREATE TABLE IF NOT EXISTS sync_locks (
                customer_code   TEXT PRIMARY KEY,
                locked_by       TEXT,
                locked_at       TEXT,
                expires_at      TEXT
            );
            """
        )


# ===== 用户 =====
def get_user_by_username(username: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, username, role, enabled, created_at, last_login_at "
            "FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def create_user(username: str, password_hash: str, role: str = "user") -> dict[str, Any]:
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, enabled, created_at, pwd_changed_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (username, password_hash, role, now, now),
        )
        uid = cur.lastrowid
        conn.commit()
    u = get_user_by_id(uid)  # type: ignore[arg-type]
    assert u is not None
    return u


def update_user_enabled(user_id: int, enabled: bool) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET enabled=? WHERE id=?", (1 if enabled else 0, user_id)
        )
        conn.commit()


def update_user_password(user_id: int, password_hash: str) -> None:
    """改密：同时更新 pwd_changed_at，使旧 token 失效。"""
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, pwd_changed_at=? WHERE id=?",
            (password_hash, now, user_id),
        )
        conn.commit()


def update_last_login(user_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET last_login_at=? WHERE id=?", (_now(), user_id)
        )
        conn.commit()


# ===== 客户 =====
def _row_to_customer_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled", 1))
    return d


def get_customer(code: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE code=?", (code,)
        ).fetchone()
        return _row_to_customer_dict(row) if row else None


def list_customers() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM customers ORDER BY created_at"
        ).fetchall()
        return [_row_to_customer_dict(r) for r in rows]


def list_customers_for_user(user_id: int) -> list[dict[str, Any]]:
    """返回该用户拥有或被分配的客户（去重）。"""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT c.* FROM customers c
            LEFT JOIN user_customer_assignments a ON a.customer_code = c.code
            WHERE c.owner_user_id = ? OR a.user_id = ?
            ORDER BY c.created_at
            """,
            (user_id, user_id),
        ).fetchall()
        return [_row_to_customer_dict(r) for r in rows]


def create_customer(
    code: str,
    webhook_url: str,
    owner_user_id: int,
    name: str | None = None,
    sync_mode: str = "incremental",
    schedule: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO customers "
            "(code, name, webhook_url, sync_mode, schedule, enabled, owner_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                code,
                name or f"客户-{code}",
                webhook_url,
                sync_mode,
                schedule,
                1 if enabled else 0,
                owner_user_id,
                now,
                now,
            ),
        )
        conn.commit()
    c = get_customer(code)
    assert c is not None
    return c


def update_customer(code: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"name", "webhook_url", "sync_mode", "schedule", "enabled"}
    fields = [f for f in allowed if f in updates]
    if not fields:
        return get_customer(code)
    now = _now()
    set_clause = ", ".join(
        f"{f}={'1' if f == 'enabled' and updates[f] else '0' if f == 'enabled' else '?'}"
        for f in fields
    )
    # enabled 单独处理为 0/1；其余用参数
    params: list[Any] = []
    set_parts: list[str] = []
    for f in fields:
        if f == "enabled":
            set_parts.append(f"enabled={1 if updates[f] else 0}")
        else:
            set_parts.append(f"{f}=?")
            params.append(updates[f])
    set_clause = ", ".join(set_parts)
    params.append(now)
    params.append(code)
    with _conn() as conn:
        conn.execute(
            f"UPDATE customers SET {set_clause}, updated_at=? WHERE code=?", params
        )
        conn.commit()
    return get_customer(code)


def delete_customer(code: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM user_customer_assignments WHERE customer_code=?", (code,)
        )
        conn.execute("DELETE FROM customers WHERE code=?", (code,))
        conn.commit()


# ===== 客户分配 =====
def assign_customer(
    user_id: int, customer_code: str, assigned_by: int
) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_customer_assignments "
            "(user_id, customer_code, assigned_by, assigned_at) VALUES (?, ?, ?, ?)",
            (user_id, customer_code, assigned_by, now),
        )
        conn.commit()


def unassign_customer(user_id: int, customer_code: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM user_customer_assignments WHERE user_id=? AND customer_code=?",
            (user_id, customer_code),
        )
        conn.commit()


def list_assignments_for_customer(customer_code: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT a.*, u.username AS assigned_username "
            "FROM user_customer_assignments a "
            "JOIN users u ON u.id = a.user_id "
            "WHERE a.customer_code=? ORDER BY a.assigned_at",
            (customer_code,),
        ).fetchall()
        return [dict(r) for r in rows]


def is_customer_accessible(customer_code: str, user_id: int, role: str) -> bool:
    """判断用户是否能访问某客户（owner 或被分配，admin 直通）。"""
    if role == "admin":
        return True
    with _conn() as conn:
        # owned?
        row = conn.execute(
            "SELECT 1 FROM customers WHERE code=? AND owner_user_id=?",
            (customer_code, user_id),
        ).fetchone()
        if row:
            return True
        # assigned?
        row = conn.execute(
            "SELECT 1 FROM user_customer_assignments WHERE user_id=? AND customer_code=?",
            (user_id, customer_code),
        ).fetchone()
        return row is not None


# ===== 同步历史 =====
def insert_sync_history(
    customer_code: str,
    triggered_by: str,
    trigger_source: str,
    started_at: str,
    finished_at: str,
    success: bool,
    total: int = 0,
    new: int = 0,
    updated: int = 0,
    skipped: int = 0,
    failed: int = 0,
    errors: list[str] | None = None,
) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO sync_history "
            "(customer_code, triggered_by, trigger_source, total, new, updated, skipped, failed, "
            "errors, started_at, finished_at, success) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                customer_code, triggered_by, trigger_source,
                total, new, updated, skipped, failed,
                json.dumps(errors or [], ensure_ascii=False),
                started_at, finished_at, 1 if success else 0,
            ),
        )
        conn.commit()
        return cur.lastrowid or 0


def list_sync_history(
    customer_code: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    with _conn() as conn:
        if customer_code:
            rows = conn.execute(
                "SELECT * FROM sync_history WHERE customer_code=? "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (customer_code, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sync_history ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["errors"] = json.loads(d.get("errors") or "[]")
            except Exception:  # noqa: BLE001
                d["errors"] = []
            d["success"] = bool(d.get("success"))
            out.append(d)
        return out


def list_sync_history_for_user(
    user_id: int, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """仅返回该用户可访问客户的同步历史。"""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT h.* FROM sync_history h
            WHERE h.customer_code IN (
                SELECT code FROM customers WHERE owner_user_id = ?
                UNION
                SELECT customer_code FROM user_customer_assignments WHERE user_id = ?
            )
            ORDER BY h.started_at DESC LIMIT ? OFFSET ?
            """,
            (user_id, user_id, limit, offset),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["errors"] = json.loads(d.get("errors") or "[]")
            except Exception:  # noqa: BLE001
                d["errors"] = []
            d["success"] = bool(d.get("success"))
            out.append(d)
        return out


# ===== 审计日志 =====
def insert_audit(
    actor: str,
    actor_role: str | None,
    action: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor, actor_role, action, target, detail, created_at, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                actor, actor_role, action, target,
                json.dumps(detail or {}, ensure_ascii=False),
                _now(), ip,
            ),
        )
        conn.commit()


def list_audit_log(
    action: str | None = None, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    with _conn() as conn:
        if action:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE action=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (action, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail") or "{}")
            except Exception:  # noqa: BLE001
                d["detail"] = {}
            out.append(d)
        return out


# ===== 同步并发锁（DB 兜底） =====
def acquire_db_lock(customer_code: str, locked_by: str, ttl_minutes: int = 10) -> bool:
    """尝试在 sync_locks 表登记。成功返回 True；若已有未过期锁返回 False。"""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl_minutes)
    with _conn() as conn:
        # 清理过期锁
        conn.execute(
            "DELETE FROM sync_locks WHERE expires_at < ?", (now.isoformat(),)
        )
        try:
            conn.execute(
                "INSERT INTO sync_locks (customer_code, locked_by, locked_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (customer_code, locked_by, now.isoformat(), expires.isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def release_db_lock(customer_code: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM sync_locks WHERE customer_code=?", (customer_code,)
        )
        conn.commit()


# ===== 启动初始化：bootstrap admin + 迁移种子 =====
def bootstrap_admin() -> int | None:
    """users 表空时，用 .env 创建管理员。返回 admin user_id。"""
    from app.services import auth

    with _conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if cnt > 0:
        return None

    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not username or not password:
        logger.error("首次启动需要 .env 配置 ADMIN_USERNAME 和 ADMIN_PASSWORD")
        raise RuntimeError("ADMIN_USERNAME/ADMIN_PASSWORD 未配置，无法 bootstrap 管理员")

    pw_hash = auth.hash_password(password)
    u = create_user(username, pw_hash, role="admin")
    insert_audit(
        actor=username, actor_role="admin", action="bootstrap_admin",
        target=str(u["id"]), detail={"note": "首次启动自动创建管理员"},
    )
    logger.info("已创建管理员账号 %s (id=%s)", username, u["id"])
    return u["id"]


def migrate_customers_from_yaml(admin_user_id: int | None = None) -> int:
    """customers 表空且 customers.yaml 存在时，把种子导入，owner=bootstrap admin。

    返回迁移条数。若 admin_user_id 为 None，则尝试找 role=admin 的第一个用户。
    """
    with _conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"]
    if cnt > 0:
        return 0
    if not config.CUSTOMERS_PATH.exists():
        return 0

    if admin_user_id is None:
        with _conn() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()
        if not row:
            logger.warning("迁移客户种子时未找到管理员，跳过")
            return 0
        admin_user_id = row["id"]

    try:
        with config.CUSTOMERS_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 customers.yaml 种子失败: %s", e)
        return 0

    customers = data.get("customers", {}) or {}
    n = 0
    for code, cfg in customers.items():
        if get_customer(code):
            continue
        create_customer(
            code=code,
            webhook_url=cfg.get("webhook_url") or config.DEFAULT_WEBHOOK_URL,
            owner_user_id=admin_user_id,
            name=cfg.get("name"),
            sync_mode=cfg.get("sync_mode", "incremental"),
            schedule=cfg.get("schedule"),
            enabled=cfg.get("enabled", True),
        )
        n += 1
    if n:
        insert_audit(
            actor="system", actor_role="system", action="migrate_yaml",
            target="customers", detail={"count": n, "owner_admin_id": admin_user_id},
        )
        logger.info("从 customers.yaml 迁移 %d 个客户到 DB（owner=admin）", n)
    return n


def init_all() -> None:
    """启动时统一调用：建表 + bootstrap admin + 迁移种子。"""
    init_db()
    admin_id = bootstrap_admin()
    migrate_customers_from_yaml(admin_id)
