"""认证接口：登录 / 登出 / 当前用户。"""
import secrets

import aiosqlite
from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel

from auth import (COOKIE_NAME, COOKIE_MAX_AGE, COOKIE_SECURE, verify_password)
from database import get_db_path

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(req: LoginRequest, response: Response):
    username = req.username.strip()
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM users WHERE username=?", (username,))).fetchone()
        if not row or not verify_password(req.password, row["password_hash"], row["password_salt"]):
            raise HTTPException(401, "用户名或密码错误")
        token = secrets.token_urlsafe(32)
        await db.execute("UPDATE users SET token=?, last_seen=datetime('now') WHERE id=?",
                         (token, row["id"]))
        await db.commit()
    response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return {"user": {"id": row["id"], "username": row["username"], "role": row["role"]}}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        async with aiosqlite.connect(get_db_path()) as db:
            await db.execute("UPDATE users SET token=NULL WHERE token=?", (token,))
            await db.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return {"user": None}
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT id, username, role FROM users WHERE token=?", (token,))).fetchone()
        return {"user": dict(row) if row else None}
