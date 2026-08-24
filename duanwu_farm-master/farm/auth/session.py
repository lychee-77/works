# farm/auth/session.py — 账号密码登录与 token 缓存
from __future__ import annotations

import json
import os
import threading
import time

import requests

from config import (
    ACCOUNT_PASSWORD,
    ACCOUNT_USERNAME,
    API_HOST,
    HEADERS_BASE,
    REGISTER_MARK_FILE,
    SIGN_DIR,
)

_SESSION_FILE = os.path.join(SIGN_DIR, "session.json")
_LOGIN_LOCK = threading.RLock()


def _session_dir() -> None:
    if SIGN_DIR:
        os.makedirs(SIGN_DIR, exist_ok=True)


def _load_session() -> dict:
    if not os.path.exists(_SESSION_FILE):
        return {}
    try:
        with open(_SESSION_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_session(token: str, user: dict | None = None) -> None:
    _session_dir()
    payload = {
        "token": token,
        "user": user or {},
        "saved_at": int(time.time()),
    }
    with open(_SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _clear_signing_registration() -> None:
    if os.path.exists(REGISTER_MARK_FILE):
        os.remove(REGISTER_MARK_FILE)


def login_account(username: str, password: str, *, captcha: str = "") -> dict:
    """
    POST /api/auth/login
    返回 {token, user, message}
    """
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        raise RuntimeError("用户名或密码为空")

    url = f"{API_HOST.rstrip('/')}/api/auth/login"
    body = {"username": username, "password": password}
    if captcha:
        body["_captcha"] = captcha

    resp = requests.post(url, json=body, headers=dict(HEADERS_BASE), timeout=20)
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"登录响应解析失败 status={resp.status_code}: {resp.text[:200]}") from e

    if resp.status_code != 200 or not data.get("success"):
        msg = data.get("message") or f"登录失败 status={resp.status_code}"
        raise RuntimeError(msg)

    inner = data.get("data") or {}
    token = inner.get("token")
    if not token:
        raise RuntimeError("登录成功但响应缺少 token")

    user = inner.get("user") or {}
    return {"token": str(token), "user": user, "message": data.get("message") or "ok"}


def ensure_access_token(*, force: bool = False, quiet: bool = False) -> str:
    """读取缓存 token；必要时用 .env 账号密码登录。"""
    with _LOGIN_LOCK:
        if not force:
            cached = _load_session()
            token = (cached.get("token") or "").strip()
            if token:
                return token

        username = (ACCOUNT_USERNAME or "").strip()
        password = ACCOUNT_PASSWORD or ""
        if not username or not password:
            raise RuntimeError("请在 .env 配置 ACCOUNT_USERNAME 与 ACCOUNT_PASSWORD")

        if not quiet:
            print(f"正在登录账号 {username}…")
        result = login_account(username, password)
        token = result["token"]
        _save_session(token, result.get("user"))
        _clear_signing_registration()
        if not quiet:
            print("登录成功，已缓存 token")
        return token


def invalidate_access_token() -> None:
    """清除 token 缓存（下次会重新登录）。"""
    with _LOGIN_LOCK:
        if os.path.exists(_SESSION_FILE):
            os.remove(_SESSION_FILE)
        _clear_signing_registration()
