"""auth 模块密码哈希单元测试。"""
from __future__ import annotations

from app.services.auth import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    hashed = hash_password("MySecret123!")
    assert hashed != "MySecret123!"
    assert verify_password("MySecret123!", hashed) is True


def test_verify_wrong_password():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("wrong-password", hashed) is False


def test_verify_invalid_hash():
    # 传入非 bcrypt 哈希不应抛异常，返回 False
    assert verify_password("anything", "not-a-valid-hash") is False


def test_hash_is_salted():
    # 相同密码两次哈希结果不同（随机盐）
    h1 = hash_password("same-password")
    h2 = hash_password("same-password")
    assert h1 != h2
