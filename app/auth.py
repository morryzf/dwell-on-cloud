"""登录。一个人用，所以做到"够"就停：签名 cookie，不建会话表。

两条进门的路：
- 网页：用户名密码登录，拿一个签名 cookie，之后浏览器自己带着。
- AI 侧：请求头带 X-Dwell-Token，绕过网页登录。心跳和 MCP 走这条。
"""

import hmac
import os
import secrets
from hashlib import sha256

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "dwell_session"
MAX_AGE = 60 * 60 * 24 * 30  # 一个月不用重新登录

# 密钥没配就临时生成一个。这样本地能直接跑，但重启会掉登录状态——
# 生产环境必须配 DWELL_SECRET，不然每次部署你都得重新登录。
SECRET = os.environ.get("DWELL_SECRET") or secrets.token_hex(32)
USER = os.environ.get("DWELL_USER", "")
PASSWORD = os.environ.get("DWELL_PASSWORD", "")
API_TOKEN = os.environ.get("DWELL_API_TOKEN", "")

_signer = URLSafeTimedSerializer(SECRET, salt="dwell-login")


def credentials_configured() -> bool:
    return bool(USER and PASSWORD)


def check_login(user: str, password: str) -> bool:
    """compare_digest 而不是 ==：避免按字符逐位比较泄漏信息。"""
    if not credentials_configured():
        return False
    ok_user = hmac.compare_digest(user.encode(), USER.encode())
    ok_pass = hmac.compare_digest(password.encode(), PASSWORD.encode())
    return ok_user and ok_pass


def make_token(user: str) -> str:
    return _signer.dumps({"u": user})


def verify_cookie(raw: str) -> bool:
    try:
        data = _signer.loads(raw, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return data.get("u") == USER


def check_api_token(token: str) -> bool:
    if not API_TOKEN:
        return False
    return hmac.compare_digest(token.encode(), API_TOKEN.encode())


def is_authed(request: Request) -> bool:
    token = request.headers.get("X-Dwell-Token", "")
    if token and check_api_token(token):
        return True
    raw = request.cookies.get(COOKIE_NAME, "")
    return bool(raw) and verify_cookie(raw)


async def require_auth(request: Request):
    """挂在需要登录的路由上。没登录就 401，前端自己弹登录框。"""
    if not is_authed(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="要先登录",
        )
