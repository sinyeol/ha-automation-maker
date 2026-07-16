"""공용 픽스처 및 import 경로 조정.

tests/ 는 automation_maker/ 아래에 있고, 백엔드는 `from backend import ...` 로
불러온다. 따라서 automation_maker 디렉터리(= 이 파일의 상위의 상위)를 sys.path 에
넣어 import 루트로 삼는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# automation_maker/ 를 import 루트로
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ---------------------------------------------------------------------------
# 모델 헬퍼 (여러 테스트에서 공용)
# ---------------------------------------------------------------------------
def dur(hours: int = 0, minutes: int = 0, seconds: int = 0) -> dict:
    """UI 모델의 Duration 객체(§4)."""
    return {"hours": hours, "minutes": minutes, "seconds": seconds}


# 검증을 통과하는 최소 트리거/액션 (개별 노드 골든 테스트의 채움용)
FILLER_TRIGGER = {"type": "time", "at": "07:00"}
FILLER_ACTION = {"type": "service", "action": "light.turn_on"}


def make_model(*, triggers=None, conditions=None, condition_mode="and",
               actions=None, alias="테스트 자동화", **extra) -> dict:
    """§4 AutomationModel. 지정하지 않은 부분은 검증을 통과하는 기본값으로 채운다."""
    m = {
        "alias": alias,
        "description": "",
        "mode": extra.pop("mode", "single"),
        "triggers": triggers if triggers is not None else [dict(FILLER_TRIGGER)],
        "condition_mode": condition_mode,
        "conditions": conditions if conditions is not None else [],
        "actions": actions if actions is not None else [dict(FILLER_ACTION)],
    }
    m.update(extra)
    return m


@pytest.fixture
def model_factory():
    return make_model


# ---------------------------------------------------------------------------
# DEV_MODE aiohttp 앱 팩토리 (MockHAClient 기반)
# ---------------------------------------------------------------------------
@pytest.fixture
def make_app(monkeypatch):
    """create_app 이 DEV_MODE 환경변수를 호출 시점에 읽으므로, 앱 생성 직전에 설정한다."""
    def _make(dev: bool = True):
        if dev:
            monkeypatch.setenv("DEV_MODE", "1")
        else:
            monkeypatch.delenv("DEV_MODE", raising=False)
        from backend.app import create_app
        from backend.mock_data import MockHAClient
        return create_app(MockHAClient())
    return _make


@pytest.fixture
def valid_model_payload():
    """POST/PUT api/automations 에 넣을 유효한 {"model": ...} 본문."""
    return {
        "model": make_model(
            alias="거실 저녁등",
            triggers=[{"type": "state", "entity_id": "light.living_room_main", "to": "on"}],
            actions=[{"type": "service", "action": "light.turn_on",
                      "target": {"entity_id": ["light.kitchen"]}}],
        )
    }


# ===========================================================================
# v2 픽스처 (SPEC-V2). 자체 규칙 엔진 + 한국어 파서용.
#
# 통합(app.py/mock_data.py) 이 아직 배선되지 않아도 v2 테스트가 폐루프로 돌 수 있도록,
# §4.2/§8.2 훅(set_state·on_state_changed·call_service 상태 반영)을 구현한 테스트용
# MockHAClient 확장과, register_v2_routes 로 배선한 aiohttp 앱 팩토리를 제공한다.
# 통합 완료 후에는 실제 create_app 배선으로 교체 가능하다.
# ===========================================================================
import copy as _copy
from datetime import datetime as _dt, timezone as _tz


# v2 기본 설정 (persons/modes/near_home). 파서·gazetteer 가 공유한다.
DEFAULT_V2_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user"},
    "modes": {},
    "near_home": {"zone_state": "home"},
    "aliases": [],
}


def _v2_reflect(client, domain, service, eid, data):
    """§8.2 표준 서비스 → mock 상태 반영. scene 은 no-op."""
    if service in ("turn_on", "turn_off") and domain in (
            "light", "switch", "fan", "media_player", "input_boolean"):
        attrs = {}
        if domain == "light" and service == "turn_on" and data.get("brightness_pct"):
            attrs["brightness"] = round(data["brightness_pct"] * 255 / 100)
        if domain == "fan" and service == "turn_on" and data.get("percentage") is not None:
            attrs["percentage"] = data["percentage"]
        client.set_state(eid, "on" if service == "turn_on" else "off", attrs or None)
    elif domain == "climate" and service in ("turn_on", "turn_off"):
        client.set_state(eid, "heat" if service == "turn_on" else "off")
    elif domain == "cover":
        client.set_state(eid, "open" if service == "open_cover" else "closed")
    elif domain == "lock":
        client.set_state(eid, "locked" if service == "lock" else "unlocked")


def make_v2_client():
    """§4.2/§8.2 훅을 구현한 테스트용 MockHAClient 확장 클래스 인스턴스."""
    from backend.mock_data import MockHAClient

    class V2TestHAClient(MockHAClient):
        def __init__(self):
            super().__init__()
            self.on_state_changed = None
            self.service_calls = []

        def set_state(self, entity_id, state, attributes=None):
            prev = self._states.get(entity_id)
            old = _copy.deepcopy(prev) if prev else None
            attrs = dict((prev or {}).get("attributes") or {})
            if attributes:
                attrs.update(attributes)
            now = _dt.now(_tz.utc).isoformat()
            new = {"entity_id": entity_id, "state": state, "attributes": attrs,
                   "last_changed": now, "last_updated": now}
            self._states[entity_id] = new
            cb = self.on_state_changed
            if cb is not None:
                cb(entity_id, old, _copy.deepcopy(new))

        async def call_service(self, domain, service, data):
            self.service_calls.append((domain, service, dict(data or {})))
            await super().call_service(domain, service, data)  # automation 도메인 처리
            targets = data.get("entity_id") if isinstance(data, dict) else None
            if isinstance(targets, str):
                targets = [targets]
            for eid in (targets or []):
                _v2_reflect(self, domain, service, eid, data or {})

    return V2TestHAClient()


@pytest.fixture
def v2_data_dir(tmp_path, monkeypatch):
    """엔진/스토어가 쓰는 DATA_DIR 을 임시 디렉터리로 격리한다."""
    d = tmp_path / "devdata"
    d.mkdir()
    monkeypatch.setenv("DATA_DIR", str(d))
    return d


# ===========================================================================
# v3 엔진 헬퍼 (SPEC-V3). mode_state 배선 + 서브룰 발화 검증용.
#
# now_fn/loop 주입 없이 실루프 + 짧은 실시간 타이머로 결정적으로 검증한다.
# test_modes.py / test_multiclause.py 가 공유한다.
# ===========================================================================
class V3Source:
    """on_event/on_resync 를 직접 구동하는 모의 이벤트 소스(상시 연결)."""

    def __init__(self, initial=None):
        self.initial = initial or []
        self._on_event = None

    async def start(self, on_event, on_resync, on_connect=None, on_disconnect=None):
        self._on_event = on_event
        on_resync(self.initial)
        if on_connect is not None:
            on_connect()

    async def stop(self):
        self._on_event = None

    def inject(self, entity_id, new_state, old_state="off", attributes=None):
        old = {"entity_id": entity_id, "state": old_state, "attributes": {}}
        new = {"entity_id": entity_id, "state": new_state,
               "attributes": attributes or {}}
        self._on_event(entity_id, old, new)


class V3RecordingHA:
    """call_service 호출을 (domain, service, data) 튜플로 기록한다."""

    def __init__(self):
        self.calls = []

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, dict(data or {})))


def v3_seed(*entity_states):
    return [{"entity_id": eid, "state": st, "attributes": {}}
            for eid, st in entity_states]


V3_INVENTORY = {
    "areas": [{"area_id": "living_room", "name": "거실"}],
    "entities": [
        {"entity_id": "binary_sensor.living_room_motion", "domain": "binary_sensor",
         "name": "거실 모션", "area_id": "living_room", "device_class": "motion",
         "state": "off"},
        {"entity_id": "light.living_room_main", "domain": "light",
         "name": "거실 메인등", "area_id": "living_room", "state": "off"},
    ],
    "zones": [],
}


@pytest.fixture
def make_v3_engine(v2_data_dir):
    """mode_state 를 배선한 RuleEngine + 구동 소스 + 기록 HA 를 만드는 팩토리.

    반환: async _make(settings=None, seed=()) -> (engine, rule_store, runlog, ha, source, mode_state)
    """
    import asyncio as _asyncio
    from backend.engine.engine import RuleEngine
    from backend.engine.modes import ModeState
    from backend.engine.rule_store import RuleStore
    from backend.engine.runlog import RunLog
    from backend.engine.state_cache import StateCache
    from backend.engine.storage import JsonStore
    from backend.engine.variables import GlobalVars

    async def _make(settings=None, seed=(), inventory=None):
        loop = _asyncio.get_running_loop()
        cfg = _copy.deepcopy(settings) if settings is not None \
            else _copy.deepcopy(DEFAULT_V2_SETTINGS)
        rs = RuleStore(JsonStore(v2_data_dir / "rules.json", [], loop=loop))
        rl = RunLog(JsonStore(v2_data_dir / "runlog.json", [], loop=loop))
        cache = StateCache()
        gvars = GlobalVars(cfg)
        ms = ModeState(cfg, JsonStore(v2_data_dir / "modes_state.json", {}, loop=loop))
        ha = V3RecordingHA()
        inv = inventory if inventory is not None else V3_INVENTORY
        engine = RuleEngine(rs, cache, gvars, ha, lambda: inv, rl,
                            loop=loop, mode_state=ms)
        src = V3Source(list(seed))
        await engine.start(src)
        return engine, rs, rl, ha, src, ms
    return _make


@pytest.fixture
def make_v2_app(v2_data_dir, monkeypatch):
    """register_v2_routes 로 배선된 DEV 앱을 만드는 팩토리.

    반환된 app 의 app["ha"] 로 V2TestHAClient 에 접근해 mock 상태를 검증할 수 있다.
    """
    def _make(settings=None):
        from aiohttp import web
        from backend.api_v2 import register_v2_routes
        from backend.app import extract_zones
        from backend.ha_client import merge_inventory
        from backend.engine.storage import JsonStore
        from backend.engine.state_cache import StateCache
        from backend.engine.variables import GlobalVars
        from backend.engine.rule_store import RuleStore
        from backend.engine.runlog import RunLog
        from backend.engine.engine import RuleEngine
        from backend.engine.event_source import MockEventSource
        from backend.engine.modes import ModeState
        from backend.nl.gazetteer import Gazetteer

        monkeypatch.setenv("DEV_MODE", "1")
        ha = make_v2_client()
        cfg = _copy.deepcopy(settings) if settings is not None \
            else _copy.deepcopy(DEFAULT_V2_SETTINGS)

        app = web.Application()
        app["ha"] = ha
        app["dev_mode"] = True
        app["mode"] = "dev"
        app["inventory"] = {"areas": [], "entities": [], "zones": []}

        settings_store = JsonStore(v2_data_dir / "settings.json", cfg)
        app["settings_store"] = settings_store
        global_vars = GlobalVars(settings_store.data)
        app["global_vars"] = global_vars

        def _gazetteer_fn():
            # 시작된 앱의 상태 변경 경고를 피하려고 app[...] 대신 반환값만 사용한다.
            return Gazetteer.build(app["inventory"], settings_store.data)
        app["gazetteer_fn"] = _gazetteer_fn

        rule_store = RuleStore(JsonStore(v2_data_dir / "rules.json", []))
        runlog = RunLog(JsonStore(v2_data_dir / "runlog.json", []))
        cache = StateCache()
        app["rule_store"] = rule_store
        app["runlog"] = runlog
        # SPEC-V3 §1.2: ModeState 를 엔진에 배선한다. settings.modes 정의를 공유
        # 참조하므로 설정의 모드가 그대로 반영된다(생성자 인자 추가 흡수).
        mode_state = ModeState(settings_store.data,
                               JsonStore(v2_data_dir / "modes_state.json", {}))
        app["mode_state"] = mode_state
        engine = RuleEngine(rule_store, cache, global_vars, ha,
                            lambda: app["inventory"], runlog, mode_state=mode_state)
        app["engine"] = engine
        event_source = MockEventSource(ha)
        app["event_source"] = event_source

        register_v2_routes(app)

        async def _on_startup(app):
            await ha.start()
            reg = await ha.fetch_registries()
            states = await ha.get_states()
            inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
            app["inventory"] = {"areas": inv["areas"], "entities": inv["entities"],
                                "zones": extract_zones(states)}
            _gazetteer_fn()
            await engine.start(event_source)

        async def _on_cleanup(app):
            await engine.stop()
            await ha.close()

        app.on_startup.append(_on_startup)
        app.on_cleanup.append(_on_cleanup)
        return app
    return _make
