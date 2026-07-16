"""이벤트 소스 (§4.2). HA WS 구독과 DEV 모의 소스 공용 인터페이스."""
from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Awaitable, Callable, Optional, Protocol

import aiohttp

log = logging.getLogger("automation_maker.engine")

OnEvent = Callable[[str, Optional[dict], Optional[dict]], None]
OnResync = Callable[[list], None]
OnConnect = Callable[[], None]        # 연결/구독 성공 통지 (§4.2, 상태 dot 용)
OnDisconnect = Callable[[], None]     # 연결 종료 통지


class EventSource(Protocol):
    async def start(self, on_event: OnEvent, on_resync: OnResync,
                    on_connect: OnConnect | None = None,
                    on_disconnect: OnDisconnect | None = None) -> None: ...
    async def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# DEV: MockHAClient 연동
# ---------------------------------------------------------------------------
class MockEventSource:
    """MockHAClient.on_state_changed 훅에 연결되는 모의 소스."""

    def __init__(self, mock_client):
        self._client = mock_client
        self._on_event: OnEvent | None = None
        self._on_disconnect: OnDisconnect | None = None

    async def start(self, on_event: OnEvent, on_resync: OnResync,
                    on_connect: OnConnect | None = None,
                    on_disconnect: OnDisconnect | None = None) -> None:
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        states = await self._client.get_states()
        on_resync(states)
        if on_connect is not None:
            on_connect()  # 모의 소스는 상시 연결
        self._client.on_state_changed = self._handle

    def _handle(self, entity_id, old_state, new_state) -> None:
        if self._on_event is not None:
            self._on_event(entity_id, old_state, new_state)

    async def stop(self) -> None:
        if getattr(self._client, "on_state_changed", None) is self._handle:
            self._client.on_state_changed = None
        self._on_event = None
        if self._on_disconnect is not None:
            self._on_disconnect()
            self._on_disconnect = None

    def inject(self, entity_id, state, attributes=None) -> None:
        """상태 주입 → set_state가 on_state_changed 훅을 발생시킨다."""
        self._client.set_state(entity_id, state, attributes)


# ---------------------------------------------------------------------------
# 실기기: supervisor WebSocket
# ---------------------------------------------------------------------------
class HAEventSource:
    def __init__(self, base_url: str = "http://supervisor/core", token: str | None = None,
                 session: aiohttp.ClientSession | None = None):
        self._base = base_url.rstrip("/")
        self._token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
        self._session = session
        self._own_session = session is None
        self._on_event: OnEvent | None = None
        self._on_resync: OnResync | None = None
        self._on_connect: OnConnect | None = None
        self._on_disconnect: OnDisconnect | None = None
        self._task: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._stopped = False
        self._msg_id = 1
        self._pong = True

    async def start(self, on_event: OnEvent, on_resync: OnResync,
                    on_connect: OnConnect | None = None,
                    on_disconnect: OnDisconnect | None = None) -> None:
        self._on_event = on_event
        self._on_resync = on_resync
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._stopped = False
        if self._session is None:
            self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run())  # 강참조 보관

    async def stop(self) -> None:
        self._stopped = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _next_id(self) -> int:
        i = self._msg_id
        self._msg_id += 1
        return i

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopped:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("HA WS 연결 오류: %s", e)
            if self._stopped:
                break
            jitter = 1 + random.uniform(-0.25, 0.25)
            await asyncio.sleep(backoff * jitter)
            backoff = min(backoff * 2, 60.0)

    async def _connect_once(self) -> None:
        ws_url = self._base.replace("https://", "wss://").replace("http://", "ws://") + "/websocket"
        self._msg_id = 1
        async with self._session.ws_connect(
            ws_url, heartbeat=30, receive_timeout=None
        ) as ws:
            self._ws = ws
            # 1) 인증
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                raise RuntimeError("auth_required 미수신")
            await ws.send_json({"type": "auth", "access_token": self._token})
            authed = await ws.receive_json()
            if authed.get("type") != "auth_ok":
                raise RuntimeError("인증 실패")
            # 2) 구독 → 3) get_states (이 순서 필수)
            sub_id = self._next_id()
            await ws.send_json({"id": sub_id, "type": "subscribe_events",
                                "event_type": "state_changed"})
            states_id = self._next_id()
            await ws.send_json({"id": states_id, "type": "get_states"})

            # auth_ok + 구독 성공 → 연결됨 통지 (fix 8)
            if self._on_connect is not None:
                self._on_connect()

            self._pong = True
            pinger = asyncio.create_task(self._ping_loop(ws))
            buffered: list[dict] = []
            resynced = False
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    data = msg.json()
                    mtype = data.get("type")
                    if mtype == "pong":
                        self._pong = True
                        continue
                    if mtype == "result" and data.get("id") == states_id:
                        if self._on_resync is not None:
                            self._on_resync(data.get("result") or [])
                        resynced = True
                        for ev in buffered:  # 구독~스냅샷 사이 실제 변경분 재생
                            self._dispatch(ev)
                        buffered.clear()
                        continue
                    if mtype == "event":
                        ev = data.get("event", {})
                        if ev.get("event_type") != "state_changed":
                            continue
                        if not resynced:
                            buffered.append(ev)
                        else:
                            self._dispatch(ev)
            finally:
                pinger.cancel()
                self._ws = None
                # 연결 종료 통지 (fix 8) — 재연결 성공 시 다시 on_connect
                if self._on_disconnect is not None:
                    self._on_disconnect()

    def _dispatch(self, event: dict) -> None:
        d = event.get("data", {})
        if self._on_event is not None:
            self._on_event(d.get("entity_id"), d.get("old_state"), d.get("new_state"))

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # ws 하트비트(30s)와 이중화된 앱레벨 ping(25s 간격, 10s 타임아웃)
        try:
            while not ws.closed:
                await asyncio.sleep(25)
                self._pong = False
                await ws.send_json({"id": self._next_id(), "type": "ping"})
                await asyncio.sleep(10)
                if not self._pong and not ws.closed:
                    log.warning("HA WS pong 타임아웃 → 재연결")
                    await ws.close()
                    return
        except (asyncio.CancelledError, Exception):
            return
