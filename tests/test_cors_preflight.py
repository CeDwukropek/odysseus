"""Regression test for the CORS-preflight auth bypass.

AuthMiddleware is the outermost middleware, so it used to 401 the credential-less
OPTIONS preflight before CORSMiddleware could answer it -- which blocks every
cross-origin browser/WebView client before the real request is ever sent. The
fix lets a genuine preflight through; `is_cors_preflight` is the pure predicate
it uses. Guard it so the bypass can't silently regress.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.middleware import is_cors_preflight


def test_genuine_preflight_is_detected():
    assert is_cors_preflight("OPTIONS", {"access-control-request-method": "POST"}) is True


def test_bare_options_is_not_a_preflight():
    # OPTIONS without Access-Control-Request-Method must NOT bypass auth.
    assert is_cors_preflight("OPTIONS", {}) is False


def test_real_methods_are_never_preflight():
    headers = {"access-control-request-method": "POST"}
    for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        assert is_cors_preflight(method, headers) is False


def _middleware_order():
    import ast
    tree = ast.parse(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"),
                          encoding="utf-8").read())
    order = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_middleware" and node.args:
            order.append((node.lineno, getattr(node.args[0], "id", "")))
    return [name for _, name in sorted(order)]


def test_cors_is_registered_last_so_it_wraps_auth():
    # Starlette runs the last registered middleware first; CORS must wrap AuthMiddleware.
    order = _middleware_order()
    assert order[-1] == "CORSMiddleware"
    assert order.index("AuthMiddleware") < order.index("CORSMiddleware")


def test_auth_rejection_keeps_cors_headers_for_allowed_origin():
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def reject(request, call_next):
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    app = Starlette(routes=[Route("/api/x", lambda request: PlainTextResponse("ok"))])
    app.add_middleware(BaseHTTPMiddleware, dispatch=reject)
    app.add_middleware(CORSMiddleware, allow_origins=["https://client.example"], allow_credentials=True)
    response = TestClient(app).get("/api/x", headers={"Origin": "https://client.example"})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "https://client.example"
    other = TestClient(app).get("/api/x", headers={"Origin": "https://other.example"})
    assert "access-control-allow-origin" not in other.headers
