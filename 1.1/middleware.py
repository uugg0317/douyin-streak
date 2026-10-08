"""HTTP 中间件：本机请求检查、禁缓存、限速与安全响应头。"""
from __future__ import annotations
from urllib.parse import urlsplit
from fastapi.responses import JSONResponse
from ip_guard import _check_rate, _client_ip
from local_access import LOOPBACK_HOSTS

async def local_request_guard(request, call_next):
    """限制本机 Host，并拒绝外部网页发起的跨站修改请求。"""
    if request.url.hostname not in LOOPBACK_HOSTS:
        return JSONResponse(status_code=403, content={"detail": "仅支持本机访问"})
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse(status_code=403, content={"detail": "拒绝跨站请求"})
        origin = request.headers.get("origin", "")
        if origin:
            try:
                source = urlsplit(origin)
                same_port = (source.port or (443 if source.scheme == "https" else 80)) == (request.url.port or (443 if request.url.scheme == "https" else 80))
                trusted = source.scheme in {"http", "https"} and source.hostname in LOOPBACK_HOSTS and same_port
            except ValueError:
                trusted = False
            if not trusted:
                return JSONResponse(status_code=403, content={"detail": "拒绝跨站请求"})
    return await call_next(request)

async def cache_policy(request, call_next):
    """HTML/API 禁缓存；静态资源允许浏览器复用并重新验证。"""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    elif path == "/api/avatar":
        response.headers["Cache-Control"] = "private, max-age=86400"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/static/vendor/"):
        response.headers["Cache-Control"] = "public, max-age=604800, must-revalidate"
    elif path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400, must-revalidate"
    return response

async def rate_limit(request, call_next):
    if not _check_rate(_client_ip(request), request.url.path):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试"})
    return await call_next(request)

async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; connect-src 'self'")
    return response

def register_middleware(app) -> None:
    app.middleware("http")(cache_policy)
    app.middleware("http")(rate_limit)
    app.middleware("http")(security_headers)
    app.middleware("http")(local_request_guard)
