"""v2 REST API (§7). 자체 규칙 엔진 + 한국어 자연어 규칙용 라우트.

앱 배선(integration 담당)이 아래 키를 채운다고 가정한다:
  app["engine"]         RuleEngine
  app["rule_store"]     RuleStore
  app["settings_store"] JsonStore(settings.json) — .data 가 설정 dict
  app["gazetteer_fn"]   () -> Gazetteer (현재 인벤토리·설정으로 재빌드/반환)
  app["global_vars"]    GlobalVars (settings dict 를 공유 참조 → 인플레이스 병합으로 갱신)
  app["dev_mode"]       bool
  app["runlog"]         RunLog (없으면 engine._runlog 로 폴백)
  app["event_source"]   MockEventSource (DEV, 없으면 engine._event_source 로 폴백)

오류 봉투는 v1 app.py 의 _error 와 동일 형식이다(자체 재구현 — app.py 미수정).
사용자 노출 문자열은 한국어, 식별자는 영어.
"""
from __future__ import annotations

import json
import logging
import os
import re

from aiohttp import web

from backend.engine.rule_model import validate_rule_model
from backend.nl.llm_assist import llm_parse
from backend.nl.parser import parse as nl_parse

log = logging.getLogger("automation_maker.api_v2")

_LLM_MERGE_SCORE = 0.7          # LLM 이 채운 슬롯의 후보 스코어(§7)
_LLM_TRIGGER_CONF = 0.6         # 이 미만이면 LLM 보조 시도

_MAX_SENTENCE = 500             # parse/rules 문장 최대 길이(자)
_MAX_RULES = 200               # 저장 가능한 규칙(루틴) 최대 개수


# ---------------------------------------------------------------------------
# 응답 헬퍼 (v1 app.py 규약 재구현)
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
    raise web.HTTPBadRequest(
        text=json.dumps({"error": {"code": "bad_request", "message": message}},
                        ensure_ascii=False),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# 배선 접근 헬퍼 (contract 키 우선, 엔진 내부 필드 폴백)
# ---------------------------------------------------------------------------
def _api_key() -> str:
    """LLM API 키는 환경변수에서만 읽는다(§5). settings.json 에는 저장하지 않는다."""
    return os.environ.get("ANTHROPIC_API_KEY") or ""


def _runlog(app: web.Application):
    rl = app.get("runlog")
    if rl is not None:
        return rl
    return getattr(app.get("engine"), "_runlog", None)


def _event_source(app: web.Application):
    src = app.get("event_source")
    if src is not None:
        return src
    return getattr(app.get("engine"), "_event_source", None)


def _inventory(app: web.Application) -> dict:
    gz = app["gazetteer_fn"]()
    inv = getattr(gz, "inventory", None)
    return inv if isinstance(inv, dict) else {"areas": [], "entities": [], "zones": []}


# ---------------------------------------------------------------------------
# GET api/v2/status
# ---------------------------------------------------------------------------
async def handle_status(request: web.Request) -> web.Response:
    return _json(request.app["engine"].status())


# ---------------------------------------------------------------------------
# POST api/v2/parse
# ---------------------------------------------------------------------------
async def handle_parse(request: web.Request) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    sentence = body.get("sentence")
    if not isinstance(sentence, str):
        return _error("bad_request", "해석할 문장을 입력해 주세요.", 400)
    if len(sentence) > _MAX_SENTENCE:
        return _error("bad_request", f"문장이 너무 길어요(최대 {_MAX_SENTENCE}자).", 400)
    pins = body.get("pins")
    if not isinstance(pins, dict):
        pins = {}

    gz = request.app["gazetteer_fn"]()
    settings = request.app["settings_store"].data or {}
    result = nl_parse(sentence, gz, settings, pins)

    used_llm = False
    api_key = _api_key()
    needs_help = result.get("confidence", 0.0) < _LLM_TRIGGER_CONF or _has_unresolved(result)
    if needs_help and api_key:
        used_llm = await _try_llm_merge(sentence, result, gz, settings, api_key)

    result["used_llm"] = used_llm
    return _json(result)


def _has_unresolved(result: dict) -> bool:
    return any(c.get("status") == "unresolved" for c in result.get("chips") or [])


async def _try_llm_merge(sentence, result, gz, settings, api_key) -> bool:
    """로컬 미해결 슬롯을 LLM 결과로 채운다(§7). 실패는 조용히 무시하고 로컬 유지."""
    try:
        digest = {
            "entities": getattr(gz, "inventory", {}).get("entities", []),
            "persons": settings.get("persons") or {},
            "modes": settings.get("modes") or {},
        }
        llm = await llm_parse(sentence, digest, settings, api_key)
        if not llm or not isinstance(llm.get("model"), dict):
            return False
        filled = _merge_unresolved(result, llm)
        for w in llm.get("warnings") or []:
            if w not in result.setdefault("warnings", []):
                result["warnings"].append(w)
        if filled:
            _recompute(result, getattr(gz, "inventory", {}))
        return filled
    except Exception:
        log.exception("LLM 보조 해석 중 오류")
        return False


# --- 슬롯 경로 파싱: "triggers[0].entity_id" / "actions[1].target" 등 -------------
_SLOT_RE = re.compile(r"^([a-z_]+)\[(\d+)\](?:\.(.+))?$")


def _slot_node(model: dict, slot_key: str):
    m = _SLOT_RE.match(slot_key or "")
    if not m:
        return None, None
    arr, idx, rest = m.group(1), int(m.group(2)), m.group(3)
    lst = model.get(arr)
    if not isinstance(lst, list) or idx >= len(lst) or not isinstance(lst[idx], dict):
        return None, None
    return lst[idx], rest


def _llm_entity_for_slot(llm_model: dict, slot_key: str):
    node, rest = _slot_node(llm_model, slot_key)
    if node is None:
        return None
    if rest in (None, "", "entity_id"):
        return node.get("entity_id")
    if rest == "target":
        return (node.get("target") or {}).get("entity_id")
    return None


def _set_slot_entity(local_model: dict, slot_key: str, value) -> bool:
    node, rest = _slot_node(local_model, slot_key)
    if node is None:
        return False
    if rest in (None, "", "entity_id"):
        node["entity_id"] = value if isinstance(value, str) else (value[0] if value else None)
        return True
    if rest == "target":
        ids = value if isinstance(value, list) else [value]
        node.setdefault("target", {})["entity_id"] = ids
        return True
    return False


def _merge_unresolved(result: dict, llm: dict) -> bool:
    """로컬 unresolved 칩을 LLM 모델의 같은 슬롯 값으로 채운다. 채운 슬롯 존재 시 True."""
    llm_model = llm.get("model") or {}
    local_model = result.get("model") or {}
    id_to_label = _id_label_map(result)
    filled = False
    for chip in result.get("chips") or []:
        if chip.get("status") != "unresolved":
            continue
        val = _llm_entity_for_slot(llm_model, chip.get("slot_key"))
        if not val:
            continue
        if not _set_slot_entity(local_model, chip.get("slot_key"), val):
            continue
        chosen = val if isinstance(val, str) else (val[0] if val else None)
        if not chosen:
            continue
        chip["chosen"] = chosen
        chip["status"] = "uncertain"
        chip["candidates"] = [{
            "id": chosen,
            "label": id_to_label.get(chosen, chosen),
            "sublabel": "AI 추정",
            "score": _LLM_MERGE_SCORE,
        }]
        filled = True
    return filled


def _id_label_map(result: dict) -> dict:
    """이미 나온 후보들에서 id→label 을 모아 병합 칩 라벨에 재사용한다."""
    out: dict = {}
    for chip in result.get("chips") or []:
        for cand in chip.get("candidates") or []:
            if cand.get("id"):
                out.setdefault(cand["id"], cand.get("label") or cand["id"])
    return out


def _recompute(result: dict, inventory) -> None:
    """병합 후 ok/confidence 재산정 (parser._emit 규약과 동일)."""
    chips = result.get("chips") or []
    scores = [c["candidates"][0]["score"] for c in chips if c.get("candidates")]
    base = sum(scores) / len(scores) if scores else 0.0
    has_unresolved = any(c.get("status") == "unresolved" for c in chips)
    if has_unresolved:
        base *= 0.4
    n_uncertain = sum(1 for c in chips if c.get("status") == "uncertain")
    base *= (0.9 ** n_uncertain)
    result["confidence"] = round(min(base, 1.0), 3) if scores else 0.0
    model_errors = validate_rule_model(result.get("model") or {}, inventory)
    result["ok"] = (not has_unresolved) and (not model_errors)


# ---------------------------------------------------------------------------
# 규칙 CRUD
# ---------------------------------------------------------------------------
async def handle_rules_list(request: web.Request) -> web.Response:
    return _json({"rules": request.app["rule_store"].all()})


async def _read_rule_body(request: web.Request):
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    model = body.get("model")
    if not isinstance(model, dict):
        model = {}
    return body, model


def _sentence_error(body: dict):
    """rules 본문의 sentence 길이 상한 검사. 초과 시 오류 응답, 아니면 None."""
    sentence = body.get("sentence")
    if isinstance(sentence, str) and len(sentence) > _MAX_SENTENCE:
        return _error("bad_request", f"문장이 너무 길어요(최대 {_MAX_SENTENCE}자).", 400)
    return None


def _build_rule(body: dict, model: dict) -> dict:
    pins = body.get("pins")
    return {
        "sentence": body.get("sentence") or "",
        "name": body.get("name") or "",
        "area_id": body.get("area_id"),
        "category": body.get("category"),
        "model": model,
        "pins": pins if isinstance(pins, dict) else {},
    }


async def handle_rule_create(request: web.Request) -> web.Response:
    body, model = await _read_rule_body(request)
    bad = _sentence_error(body)
    if bad is not None:
        return bad
    if len(request.app["rule_store"].all()) >= _MAX_RULES:
        return _error("too_many_rules", f"루틴이 너무 많아요(최대 {_MAX_RULES}개).", 400)
    errors = validate_rule_model(model, _inventory(request.app))
    if errors:
        return _error("invalid_rule", "입력한 규칙이 올바르지 않습니다.", 400, errors=errors)
    rule = _build_rule(body, model)
    saved = request.app["rule_store"].upsert(rule)
    request.app["engine"].reload_rule(saved["id"])
    return _json({"rule": saved})


async def handle_rule_update(request: web.Request) -> web.Response:
    rid = request.match_info["id"]
    store = request.app["rule_store"]
    existing = store.get(rid)
    if existing is None:
        return _error("not_found", "수정할 규칙을 찾을 수 없습니다.", 404)
    body, model = await _read_rule_body(request)
    bad = _sentence_error(body)
    if bad is not None:
        return bad
    errors = validate_rule_model(model, _inventory(request.app))
    if errors:
        return _error("invalid_rule", "입력한 규칙이 올바르지 않습니다.", 400, errors=errors)
    rule = _build_rule(body, model)
    rule["id"] = rid
    # enabled 는 토글 전용 필드이므로 편집 저장 시 기존 값을 보존한다.
    rule["enabled"] = existing.get("enabled", True)
    saved = store.upsert(rule)
    request.app["engine"].reload_rule(rid)
    return _json({"rule": saved})


async def handle_rule_delete(request: web.Request) -> web.Response:
    rid = request.match_info["id"]
    request.app["rule_store"].delete(rid)
    request.app["engine"].reload_rule(rid)  # 인덱스에서 제거
    return _json({"ok": True})


async def handle_rule_toggle(request: web.Request) -> web.Response:
    rid = request.match_info["id"]
    body = await request.json()
    on = bool((body or {}).get("on"))
    rule = request.app["rule_store"].set_enabled(rid, on)
    if rule is None:
        return _error("not_found", "규칙을 찾을 수 없습니다.", 404)
    request.app["engine"].reload_rule(rid)
    return _json({"rule": rule})


async def handle_rule_run(request: web.Request) -> web.Response:
    rid = request.match_info["id"]
    rule = request.app["rule_store"].get(rid)
    if rule is None:
        return _error("not_found", "실행할 규칙을 찾을 수 없습니다.", 404)
    await request.app["engine"].fire_rule(rule, "run")
    return _json({"ok": True})


# ---------------------------------------------------------------------------
# GET api/v2/runlog
# ---------------------------------------------------------------------------
async def handle_runlog(request: web.Request) -> web.Response:
    rl = _runlog(request.app)
    entries = rl.entries() if rl is not None else []
    return _json({"entries": entries})


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
async def handle_settings_get(request: web.Request) -> web.Response:
    data = request.app["settings_store"].data
    out = dict(data) if isinstance(data, dict) else {}
    out["llm_available"] = bool(_api_key())
    return _json(out)


async def handle_settings_put(request: web.Request) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    store = request.app["settings_store"]
    current = store.data
    if not isinstance(current, dict):
        current = {}
        store.data = current
    old_segments = current.get("segments")
    # global_vars 가 이 dict 를 참조하므로 인플레이스 병합해야 vars 가 갱신된다(§7).
    for key, val in body.items():
        if key == "llm_available":
            continue  # 계산 필드는 저장하지 않는다
        current[key] = val
    store.save_soon()
    request.app["gazetteer_fn"]()  # gazetteer 재빌드(새 persons/modes/aliases 반영)
    # segments 경계가 바뀌면 엔진의 경계 타이머를 재스케줄한다(§7). reschedule_boundary 는
    # 엔진 에이전트가 병렬로 추가 중인 공개 메서드이므로 hasattr 로 방어적으로 호출한다.
    if "segments" in body and current.get("segments") != old_segments:
        engine = request.app.get("engine")
        if engine is not None and hasattr(engine, "reschedule_boundary"):
            engine.reschedule_boundary()
    out = dict(current)
    out["llm_available"] = bool(_api_key())
    return _json(out)


# ---------------------------------------------------------------------------
# POST api/v2/dev/state (DEV 전용)
# ---------------------------------------------------------------------------
async def handle_dev_state(request: web.Request) -> web.Response:
    if not request.app.get("dev_mode"):
        return _error("not_found", "개발 모드에서만 사용할 수 있습니다.", 404)
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    entity_id = body.get("entity_id")
    state = body.get("state")
    if not isinstance(entity_id, str) or not entity_id:
        return _error("bad_request", "entity_id 를 입력해 주세요.", 400)
    if not isinstance(state, str):
        return _error("bad_request", "state 를 입력해 주세요.", 400)
    attributes = body.get("attributes")
    if attributes is not None and not isinstance(attributes, dict):
        return _error("bad_request", "attributes 형식이 올바르지 않습니다.", 400)
    src = _event_source(request.app)
    if src is None or not hasattr(src, "inject"):
        return _error("dev_unavailable", "상태 주입을 사용할 수 없습니다.", 400)
    src.inject(entity_id, state, attributes)
    return _json({"ok": True})


# ---------------------------------------------------------------------------
# 라우트 등록
# ---------------------------------------------------------------------------
def register_v2_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/v2/status", handle_status)
    r.add_post("/api/v2/parse", handle_parse)
    r.add_get("/api/v2/rules", handle_rules_list)
    r.add_post("/api/v2/rules", handle_rule_create)
    r.add_put("/api/v2/rules/{id}", handle_rule_update)
    r.add_delete("/api/v2/rules/{id}", handle_rule_delete)
    r.add_post("/api/v2/rules/{id}/toggle", handle_rule_toggle)
    r.add_post("/api/v2/rules/{id}/run", handle_rule_run)
    r.add_get("/api/v2/runlog", handle_runlog)
    r.add_get("/api/v2/settings", handle_settings_get)
    r.add_put("/api/v2/settings", handle_settings_put)
    r.add_post("/api/v2/dev/state", handle_dev_state)
