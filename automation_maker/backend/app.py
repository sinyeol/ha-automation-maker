"""aiohttp 앱: ingress IP 가드, 오류 핸들링, 정적 서빙, REST 엔드포인트."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

import aiohttp
from aiohttp import web

from backend.api_v2 import register_v2_routes
from backend.automation_builder import (
    KNOWN_SERVICES, ValidationError, build_automation, summarize, to_yaml, validate_model,
)
from backend.engine.engine import RuleEngine
from backend.engine.event_source import HAEventSource, MockEventSource
from backend.engine.modes import ModeState
from backend.engine.rule_store import RuleStore
from backend.engine.runlog import RunLog
from backend.engine.state_cache import StateCache
from backend.engine.storage import JsonStore, data_dir
from backend.engine.sun import SunProvider
from backend.engine.variables import GlobalVars
from backend.ha_client import HAClient, merge_inventory
from backend.nl.gazetteer import Gazetteer

VERSION = "3.2.0"
FRONTEND = Path(__file__).parent.parent / "frontend"
ALLOWED_REMOTES = {"172.30.32.2", "127.0.0.1"}

log = logging.getLogger("automation_maker")


# ---------------------------------------------------------------------------
# 응답 헬퍼
# ---------------------------------------------------------------------------
def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(
        data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _error(code: str, message: str, status: int, errors: list | None = None) -> web.Response:
    payload: dict = {"error": {"code": code, "message": message}}
    if errors is not None:
        payload["error"]["errors"] = errors
    return _json(payload, status=status)


def _raise_bad_request(message: str = "요청 본문을 해석할 수 없습니다.") -> None:
    """검증 미들웨어와 동일한 JSON 형태로 400을 즉시 반환한다(핸들러에서 raise)."""
    raise web.HTTPBadRequest(
        text=json.dumps({"error": {"code": "bad_request", "message": message}},
                        ensure_ascii=False),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# 자동화 목록 추출 헬퍼 (states의 automation.* 필터링)
# ---------------------------------------------------------------------------
def extract_automations(states: list[dict]) -> list[dict]:
    out = []
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith("automation."):
            continue
        attrs = s.get("attributes", {}) or {}
        aid = attrs.get("id")
        out.append({
            "entity_id": eid,
            "automation_id": aid,
            "alias": attrs.get("friendly_name") or eid,
            "state": s.get("state"),
            "last_triggered": attrs.get("last_triggered"),
            "editable": aid is not None,
        })
    return out


async def _resolve_entity_id(ha, automation_id: str) -> str | None:
    for s in await ha.get_states():
        eid = s.get("entity_id", "")
        if eid.startswith("automation.") and s.get("attributes", {}).get("id") == automation_id:
            return eid
    return None


def extract_zones(states: list[dict]) -> list[dict]:
    # zone.*은 일반 엔티티 인벤토리(merge_inventory)에서 제외되므로 별도 목록으로 내려준다.
    out = []
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith("zone."):
            continue
        attrs = s.get("attributes", {}) or {}
        out.append({"entity_id": eid, "name": attrs.get("friendly_name") or eid})
    out.sort(key=lambda z: z["name"])
    return out


# ---------------------------------------------------------------------------
# v2 엔진 배선 헬퍼 (§8)
# ---------------------------------------------------------------------------
def _default_settings() -> dict:
    """settings.json 기본값 (§5). API 키는 환경에서만 읽으므로 여기 두지 않는다.

    persons/modes 기본값은 DEV mock 인벤토리(person.wife·scene.sleep_mode)에 맞춰
    두어 DEV_MODE에서 예시 문장이 즉시 동작하게 한다(§11). 실기기 사용자는 설정에서 수정.
    """
    return {
        "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                     "evening": "17:00", "night": "21:00"},
        "persons": {"나": "person.user", "와이프": "person.wife"},
        # SPEC-V3 §1.1·§7: 신형 모드 정의(initial/on_action/off_action). 슬립 모드는
        # 켜질 때 scene.sleep_mode 를 실행하고, 꺼질 때 side-effect 없음.
        "modes": {"슬립 모드": {"initial": "off",
                              "on_action": {"action": "scene.turn_on",
                                            "target": {"entity_id": ["scene.sleep_mode"]}},
                              "off_action": None}},
        "near_home": {"zone_state": "home", "note": "사람 엔티티가 이 상태면 '집 근처'"},
        # APP-PORT-PLAN §2.1: sun 트리거·sun_window 조건의 일출/일몰 계산 좌표(기본 서울).
        # UI 미설정이어도 기본값으로 동작한다. 사용자는 설정에서 위도/경도를 바꿀 수 있다.
        "location": {"latitude": 37.5665, "longitude": 126.9780},
        "aliases": [],
        "confirm_actions": ["lock", "valve"],
        # SPEC-V3 §4.1: LLM 해석 백엔드 선택(off|api|cli). 키/토큰은 환경에서만 읽는다.
        "llm": {"backend": "off"},
        # Phase 3C: CLI 학습 기능 on/off(사용자 선택 옵션, 기본 off). 실제 정규화는
        # 기존 llm.backend(cli/api)를 재사용한다(별도 백엔드 옵션 없음).
        "learn": {"enabled": False},
    }


def _merge_settings_defaults(settings: dict) -> None:
    """로드된 설정에 빠진 최상위 기본 키를 채운다(인플레이스)."""
    for key, val in _default_settings().items():
        settings.setdefault(key, val)


# ---------------------------------------------------------------------------
# Phase 3B — L2 템플릿 매처 배선 헬퍼
# ---------------------------------------------------------------------------
def _load_pattern_library() -> list:
    """빌드시 생성되는 패턴 라이브러리(nl/pattern_library.yaml, read-only). 애드온 이미지에 동봉.

    로더(backend.nl.pattern_match.load_pattern_library) 부재·손상 시 빈 목록(매처 비활성).
    """
    try:
        from backend.nl.pattern_match import load_pattern_library
    except Exception:
        return []
    try:
        lib = load_pattern_library()
    except Exception:
        log.exception("패턴 라이브러리 로드 실패 — 매처 비활성")
        return []
    return lib if isinstance(lib, list) else []


def _build_template_matcher(pattern_library: list, gazetteer, inventory: dict):
    """L2 매처(backend.nl.pattern_match.TemplateMatcher). 모듈/라이브러리 부재 시 None.

    미배포·초기화 실패를 조용히 흡수한다(매처 비활성 = 규칙만 사용, 회귀 0).
    """
    if not pattern_library:
        return None
    try:
        from backend.nl.pattern_match import TemplateMatcher
    except Exception:
        return None
    try:
        return TemplateMatcher(pattern_library, gazetteer, inventory)
    except Exception:
        log.exception("템플릿 매처 초기화 실패 — 매처 비활성")
        return None


def _build_learned_store(ddir):
    """로컬 학습 저장소(backend.nl.learn.LearnedStore, JsonStore 확장/YAML). 부재 시 None."""
    try:
        from backend.nl.learn import LearnedStore
    except Exception:
        return None
    try:
        return LearnedStore(ddir / "learned_patterns.yaml")
    except Exception:
        log.exception("학습 저장소 초기화 실패 — 학습 비활성")
        return None


async def _build_inventory(ha) -> dict:
    """bootstrap 형태 인벤토리 {areas, entities, zones}. gazetteer·엔진 스코프 공용."""
    reg = await ha.fetch_registries()
    states = await ha.get_states()
    inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
    inv["zones"] = extract_zones(states)
    return inv


# ---------------------------------------------------------------------------
# 미들웨어
# ---------------------------------------------------------------------------
@web.middleware
async def ingress_guard(request: web.Request, handler):
    if not request.app["dev_mode"]:
        if request.remote not in ALLOWED_REMOTES:
            return _error("forbidden", "허용되지 않은 접근입니다.", 403)
    return await handler(request)


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except ValidationError as e:
        return _error("invalid_model", "입력한 자동화 모델이 올바르지 않습니다.", 400, errors=e.errors)
    except json.JSONDecodeError:
        return _error("bad_request", "요청 본문을 해석할 수 없습니다.", 400)
    except aiohttp.ClientResponseError as e:
        # HA(Supervisor) 프록시가 4xx/5xx로 응답한 경우
        if e.status == 400:
            return _error("ha_rejected", f"HA가 구성을 거부했습니다: {e.message}", 400)
        if e.status == 404:
            return _error("not_found", "자동화를 찾을 수 없습니다.", 404)
        return _error("ha_upstream", "HA 응답 오류가 발생했습니다.", 502)
    except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
        return _error("ha_timeout", "HA 응답이 지연되고 있습니다.", 504)
    except (aiohttp.ClientConnectorError, aiohttp.ClientConnectionError):
        return _error("ha_unreachable", "HA에 연결할 수 없습니다.", 502)
    except Exception:
        log.exception("처리되지 않은 서버 오류")
        return _error("server_error", "서버 내부 오류가 발생했습니다.", 500)


@web.middleware
async def static_cache(request: web.Request, handler):
    resp = await handler(request)
    if request.path == "/" or request.path.startswith("/css/") or request.path.startswith("/js/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------------------
# 핸들러
# ---------------------------------------------------------------------------
async def handle_index(request: web.Request) -> web.Response:
    return web.FileResponse(FRONTEND / "index.html")


async def handle_health(request: web.Request) -> web.Response:
    return _json({"ok": True, "mode": request.app["mode"], "version": VERSION})


async def handle_bootstrap(request: web.Request) -> web.Response:
    ha = request.app["ha"]
    reg = await ha.fetch_registries()
    states = await ha.get_states()
    inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
    return _json({
        "mode": request.app["mode"],
        "areas": inv["areas"],
        "entities": inv["entities"],
        "zones": extract_zones(states),
        "services": KNOWN_SERVICES,
        "automations": extract_automations(states),
    })


async def handle_list(request: web.Request) -> web.Response:
    ha = request.app["ha"]
    return _json({"automations": extract_automations(await ha.get_states())})


async def handle_get(request: web.Request) -> web.Response:
    ha = request.app["ha"]
    aid = request.match_info["id"]
    config = await ha.get_automation_config(aid)
    if config is None:
        return _error("not_found", "자동화를 찾을 수 없습니다.", 404)
    return _json({"id": aid, "config": config})


async def _read_model(request: web.Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    model = body.get("model")
    if not isinstance(model, dict):
        model = {}
    return model


async def handle_create(request: web.Request) -> web.Response:
    ha = request.app["ha"]
    model = await _read_model(request)
    errors = validate_model(model)
    if errors:
        return _error("invalid_model", "입력한 자동화 모델이 올바르지 않습니다.", 400, errors=errors)
    config = build_automation(model)

    aid = None
    for _ in range(3):
        cand = uuid4().hex
        if await ha.get_automation_config(cand) is None:
            aid = cand
            break
    if aid is None:
        aid = uuid4().hex

    await ha.upsert_automation(aid, config)
    return _json({"id": aid, "yaml": to_yaml(config)})


async def handle_update(request: web.Request) -> web.Response:
    ha = request.app["ha"]
    aid = request.match_info["id"]
    if await ha.get_automation_config(aid) is None:
        return _error("not_found", "수정할 자동화를 찾을 수 없습니다.", 404)
    model = await _read_model(request)
    errors = validate_model(model)
    if errors:
        return _error("invalid_model", "입력한 자동화 모델이 올바르지 않습니다.", 400, errors=errors)
    config = build_automation(model)
    await ha.upsert_automation(aid, config)
    return _json({"id": aid, "yaml": to_yaml(config)})


async def handle_delete(request: web.Request) -> web.Response:
    ha = request.app["ha"]
    await ha.delete_automation(request.match_info["id"])
    return _json({"ok": True})


async def handle_toggle(request: web.Request) -> web.Response:
    ha = request.app["ha"]
    aid = request.match_info["id"]
    body = await request.json()
    on = bool((body or {}).get("on"))
    eid = await _resolve_entity_id(ha, aid)
    if eid is None:
        return _error("not_found", "자동화를 찾을 수 없습니다.", 404)
    await ha.call_service("automation", "turn_on" if on else "turn_off", {"entity_id": eid})
    return _json({"ok": True})


async def handle_run(request: web.Request) -> web.Response:
    ha = request.app["ha"]
    aid = request.match_info["id"]
    eid = await _resolve_entity_id(ha, aid)
    if eid is None:
        return _error("not_found", "자동화를 찾을 수 없습니다.", 404)
    await ha.call_service("automation", "trigger", {"entity_id": eid})
    return _json({"ok": True})


async def handle_preview(request: web.Request) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    model = body.get("model")
    if not isinstance(model, dict):
        model = {}
    entity_names = body.get("entity_names")
    if not isinstance(entity_names, dict):
        entity_names = {}
    errors = validate_model(model)
    if errors:
        return _json({"yaml": "", "summary": summarize(model, entity_names), "errors": errors})
    config = build_automation(model)
    return _json({"yaml": to_yaml(config), "summary": summarize(model, entity_names), "errors": []})


# ---------------------------------------------------------------------------
# 앱 구성
# ---------------------------------------------------------------------------
def create_app(ha: "HAClient") -> web.Application:
    dev_mode = os.environ.get("DEV_MODE") == "1"
    # 실기기(SUPERVISOR_TOKEN 존재)가 아니면 /data 대신 ./devdata 를 쓴다
    # (SPEC §0 DEV 기본값 + 테스트/개발 격리, .gitignore 대상).
    if not os.environ.get("DATA_DIR") and not os.environ.get("SUPERVISOR_TOKEN"):
        os.environ["DATA_DIR"] = os.path.abspath("devdata")

    app = web.Application(middlewares=[ingress_guard, error_middleware, static_cache])
    app["ha"] = ha
    app["dev_mode"] = dev_mode
    app["mode"] = "dev" if dev_mode else "ha"

    # v1 라우트
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/bootstrap", handle_bootstrap)
    app.router.add_get("/api/automations", handle_list)
    app.router.add_get("/api/automations/{id}", handle_get)
    app.router.add_post("/api/automations", handle_create)
    app.router.add_put("/api/automations/{id}", handle_update)
    app.router.add_delete("/api/automations/{id}", handle_delete)
    app.router.add_post("/api/automations/{id}/toggle", handle_toggle)
    app.router.add_post("/api/automations/{id}/run", handle_run)
    app.router.add_post("/api/preview", handle_preview)

    # v2 라우트 (핸들러는 요청 시점에 app[...] 배선을 읽으므로 여기서 등록만)
    register_v2_routes(app)

    app.router.add_get("/", handle_index)
    if (FRONTEND / "css").is_dir():
        app.router.add_static("/css/", FRONTEND / "css")
    if (FRONTEND / "js").is_dir():
        app.router.add_static("/js/", FRONTEND / "js")

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


# ---------------------------------------------------------------------------
# 수명주기: v2 엔진 구성요소 생성·배선·기동/정리
# ---------------------------------------------------------------------------
async def _on_startup(app: web.Application) -> None:
    await app["ha"].start()
    ha = app["ha"]
    ddir = data_dir()

    # 설정
    settings_store = JsonStore(ddir / "settings.json", _default_settings())
    if isinstance(settings_store.data, dict):
        _merge_settings_defaults(settings_store.data)
    else:
        settings_store.data = _default_settings()
    app["settings_store"] = settings_store

    # 인벤토리(기동 시 1회) — 실패해도 엔진은 기동한다(빈 인벤토리).
    try:
        inventory = await _build_inventory(ha)
    except Exception:
        log.exception("인벤토리 초기화 실패 — 빈 인벤토리로 시작")
        inventory = {"areas": [], "entities": [], "zones": []}
    app["_inventory"] = inventory

    # gazetteer 는 매 호출마다 현재 인벤토리+설정으로 재빌드(설정 변경 즉시 반영)
    def gazetteer_fn():
        return Gazetteer.build(app["_inventory"], settings_store.data)
    app["gazetteer_fn"] = gazetteer_fn

    # 전역 변수는 settings dict 참조를 공유(인플레이스 병합으로 갱신)
    app["global_vars"] = GlobalVars(settings_store.data)

    # Phase 3B: L2 템플릿 매처(규칙이 not ok 일 때만 흡수). 라이브러리/모듈 부재 시 None.
    app["pattern_library"] = _load_pattern_library()
    app["template_matcher"] = _build_template_matcher(
        app["pattern_library"], gazetteer_fn(), inventory)

    # Phase 3C: 로컬 학습 저장소(/data/learned_patterns.yaml). 학습된 (원문→정규형→model)을
    # CLI 없이 로컬 재사용한다(§3). LearnedStore 는 JsonStore 확장이라 그대로 flush 대상.
    learned_store = _build_learned_store(ddir)
    app["learned_store"] = learned_store
    # 재기동 시 엔티티 재검증(§3)은 인벤토리가 실제로 로드됐을 때만 수행한다. HA 부팅 순서상
    # core API 가 아직 안 떠서 _build_inventory 가 실패하면 inventory 는 빈 값이 되는데, 그
    # 상태로 revalidate 를 돌리면 모든 학습 항목이 '엔티티 소멸'로 오판돼 디스크에서 영구
    # 삭제된다(일시적 장애 → 데이터 소실). 빈 인벤토리면 건너뛴다(revalidate 내부에도 가드 존재).
    if learned_store is not None and inventory.get("entities"):
        try:
            removed = learned_store.revalidate(inventory)
            if removed:
                log.info("학습 항목 %d개 제거(엔티티 소멸)", len(removed))
        except Exception:
            log.exception("학습 항목 재검증 실패")

    # §4.5 CLI 증류 수용체: 학습된 (원문→정규형→구체 model)을 매처 런타임 템플릿으로 편입해,
    # 이후 같은/비슷(스트림 LCS)한 문장을 CLI 없이 로컬에서 struct_replace 로 흡수한다.
    if app["template_matcher"] is not None and learned_store is not None:
        try:
            app["template_matcher"].add_runtime_templates(learned_store.all())
        except Exception:
            log.exception("학습 런타임 템플릿 초기화 실패")

    rules_json = JsonStore(ddir / "rules.json", [])
    rule_store = RuleStore(rules_json)
    app["rule_store"] = rule_store

    runlog_json = JsonStore(ddir / "runlog.json", [])
    runlog = RunLog(runlog_json)
    app["runlog"] = runlog

    # 상태 모드 변수(SPEC-V3 §1.2·§7): 정의는 settings.modes, 런타임 상태는
    # modes_state.json. ModeState 생성이 settings.modes 를 v3 형식으로 제자리
    # 마이그레이션(v2 {action,target,data} → on_action/off_action/initial)한다.
    modes_json = JsonStore(ddir / "modes_state.json", {})
    mode_state = ModeState(settings_store.data, modes_json)
    app["mode_state"] = mode_state

    # modes_json 도 종료 flush 대상에 포함(engine.stop → mode_state.save 예약분 확정).
    app["_json_stores"] = [settings_store, rules_json, runlog_json, modes_json]
    if learned_store is not None:
        app["_json_stores"].append(learned_store)

    state_cache = StateCache()

    def inventory_fn():
        return app["_inventory"]

    # APP-PORT-PLAN §2.1: SunProvider 는 settings_store.data(공유 참조)를 읽으므로 설정의
    # location 변경이 자동 반영된다(위치 변경 시 api_v2 가 invalidate() 로 캐시만 비운다).
    sun_provider = SunProvider(settings_store.data)
    app["sun_provider"] = sun_provider
    engine = RuleEngine(rule_store, state_cache, app["global_vars"], ha,
                        inventory_fn, runlog, mode_state=mode_state,
                        sun_provider=sun_provider)
    app["engine"] = engine

    event_source = MockEventSource(ha) if app["dev_mode"] else HAEventSource()
    app["event_source"] = event_source

    await engine.start(event_source)
    log.info("v2 엔진 시작: rules=%d", len(rule_store.all()))


async def _on_cleanup(app: web.Application) -> None:
    engine = app.get("engine")
    if engine is not None:
        try:
            await engine.stop()
        except Exception:
            log.exception("엔진 종료 중 오류")
    for js in app.get("_json_stores", []):
        try:
            await js.flush()
        except Exception:
            log.exception("저장 flush 중 오류")
    await app["ha"].close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dev_mode = os.environ.get("DEV_MODE") == "1"
    if dev_mode:
        from backend.mock_data import MockHAClient
        ha = MockHAClient()
        mode = "dev"
    else:
        ha = HAClient()
        mode = "ha"
    port = int(os.environ.get("PORT", "8099"))
    log.info("HA Automation Maker 시작: mode=%s port=%s", mode, port)
    web.run_app(create_app(ha), host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
