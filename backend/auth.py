"""认证与角色权限（单管理员 + 匿名只读）。

- 密码 PBKDF2-HMAC-SHA256 加盐哈希，绝不存明文；校验用 hmac.compare_digest 常量时间比较。
- token 存 httponly cookie。
- require_admin：写/执行端点的守卫，非管理员 403。
- current_user：含匿名（返回 None），用于 GET 只读放行。
"""
import hashlib
import hmac
import os
import secrets

import aiosqlite
from fastapi import Request, HTTPException

from database import get_db_path

COOKIE_NAME = "akivili_token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 天
COOKIE_SECURE = os.environ.get("AKIVILI_COOKIE_SECURE", "").lower() in ("1", "true", "yes")

# 播种管理员：从环境变量读，缺省用占位值。
# 部署时务必设置 AKIVILI_ADMIN_USER / AKIVILI_ADMIN_PASSWORD（尤其密码），切勿沿用默认占位。
SEED_ADMIN_USERNAME = os.environ.get("AKIVILI_ADMIN_USER", "admin")
SEED_ADMIN_PASSWORD = os.environ.get("AKIVILI_ADMIN_PASSWORD", "changeme")


def hash_password(password: str, salt: str = "") -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return hashed, salt


def verify_password(password: str, hashed: str, salt: str) -> bool:
    check, _ = hash_password(password, salt)
    return hmac.compare_digest(check, hashed)  # 常量时间，防时序攻击


async def seed_admin() -> None:
    """首次启动播种管理员；已存在则跳过。"""
    async with aiosqlite.connect(get_db_path()) as db:
        cur = await db.execute("SELECT id FROM users WHERE username=?", (SEED_ADMIN_USERNAME,))
        if await cur.fetchone():
            return
        hashed, salt = hash_password(SEED_ADMIN_PASSWORD)
        await db.execute(
            "INSERT INTO users (username, password_hash, password_salt, role) VALUES (?,?,?, 'admin')",
            (SEED_ADMIN_USERNAME, hashed, salt))
        await db.commit()


async def _user_from_token(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT id, username, role FROM users WHERE token=?", (token,))).fetchone()
    return {"id": row["id"], "username": row["username"], "role": row["role"]} if row else None


async def current_user(request: Request) -> dict | None:
    """当前用户，匿名返回 None（用于只读接口）。"""
    return await _user_from_token(request)


async def require_admin(request: Request) -> dict:
    """写/执行端点守卫：非管理员 403。"""
    u = await _user_from_token(request)
    if not u:
        raise HTTPException(401, "未登录")
    if u["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return u
