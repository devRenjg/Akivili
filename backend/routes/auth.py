"""认证接口：登录 / 登出 / 当前用户。"""
import secrets

from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel

from sqlalchemy import select, update as sa_update

from auth import (COOKIE_NAME, COOKIE_MAX_AGE, COOKIE_SECURE, verify_password)
from models import User, get_session_factory, now_expr

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(req: LoginRequest, response: Response):
    username = req.username.strip()
    async with get_session_factory()() as session:
        user = (await session.execute(
            select(User).where(User.username == username))).scalar_one_or_none()
        if not user or not verify_password(req.password, user.password_hash, user.password_salt):
            raise HTTPException(401, "用户名或密码错误")
        token = secrets.token_urlsafe(32)
        # last_seen 用 now_expr()（SQLite→CURRENT_TIMESTAMP，与旧 datetime('now') 同 UTC）
        await session.execute(sa_update(User).where(User.id == user.id).values(
            token=token, last_seen=now_expr()))
        await session.commit()
        uid, uname, urole = user.id, user.username, user.role
    response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return {"user": {"id": uid, "username": uname, "role": urole}}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        async with get_session_factory()() as session:
            await session.execute(sa_update(User).where(User.token == token).values(token=None))
            await session.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return {"user": None}
    async with get_session_factory()() as session:
        row = (await session.execute(
            select(User.id, User.username, User.role).where(User.token == token))).first()
        return {"user": {"id": row.id, "username": row.username, "role": row.role} if row else None}
