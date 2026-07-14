"""ingress_guard 미들웨어 접근 제어 테스트.

실제 aiohttp 테스트 클라이언트는 remote 가 항상 127.0.0.1(허용 IP)이라 위조로 403 을
재현하기 어렵다. 따라서 미들웨어를 순수 함수처럼 직접 호출해 허용/차단/개발모드 우회를
검증하고, 허용 경로는 비-DEV 앱의 실제 요청으로도 통과함을 확인한다.
"""
from __future__ import annotations

import pytest

from backend.app import ALLOWED_REMOTES, ingress_guard

_SENTINEL = object()


class _FakeRequest:
    def __init__(self, remote, dev_mode):
        self.remote = remote
        self.app = {"dev_mode": dev_mode}


async def _pass_handler(request):
    return _SENTINEL


async def _boom_handler(request):
    raise AssertionError("차단돼야 하는 요청에서 핸들러가 호출됨")


# ---------------------------------------------------------------------------
# 미들웨어 단위 테스트
# ---------------------------------------------------------------------------
def test_allowed_remotes_constant():
    assert ALLOWED_REMOTES == {"172.30.32.2", "127.0.0.1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["172.30.32.2", "127.0.0.1"])
async def test_allowed_ip_passes(ip):
    req = _FakeRequest(ip, dev_mode=False)
    result = await ingress_guard(req, _pass_handler)
    assert result is _SENTINEL


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["10.0.0.9", "192.168.1.50", "203.0.113.7", None])
async def test_forbidden_ip_blocked(ip):
    req = _FakeRequest(ip, dev_mode=False)
    resp = await ingress_guard(req, _boom_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_dev_mode_bypasses_ip_check():
    # DEV_MODE 에서는 외부 IP 라도 통과
    req = _FakeRequest("203.0.113.7", dev_mode=True)
    result = await ingress_guard(req, _pass_handler)
    assert result is _SENTINEL


# ---------------------------------------------------------------------------
# 비-DEV 실제 앱: 허용 IP(테스트 클라이언트=127.0.0.1) 통과 확인
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_non_dev_app_allows_loopback(aiohttp_client, make_app):
    client = await aiohttp_client(make_app(dev=False))
    resp = await client.get("/api/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["mode"] == "ha"
