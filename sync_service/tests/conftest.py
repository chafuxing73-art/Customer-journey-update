"""pytest 全局配置：测试启动前注入必要的环境变量，避免 config 模块导入失败。"""
from __future__ import annotations

import os

# 在 app.config 被导入前设置必需项（config 启动时会校验）
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-ci-only-please-change")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "Test123456")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
