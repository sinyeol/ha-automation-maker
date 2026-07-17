"""v2 REST API (§7) + SPEC-V3 확장(§5·§1.4·§3.4·§4.5). 자체 규칙 엔진 + 한국어 자연어 규칙용 라우트.

앱 배선(integration 담당)이 아래 키를 채운다고 가정한다:
  app["engine"]         RuleEngine (SPEC-V3: mode_state 를 보유 — engine.set_mode/_mode_state)
  app["rule_store"]     RuleStore
  app["settings_store"] JsonStore(settings.json) — .data 가 설정 dict
  app["gazetteer_fn"]   () -> Gazetteer (현재 인벤토리·설정으로 재빌드/반환)
  app["global_vars"]    GlobalVars (settings dict 를 공유 참조 → 인플레이스 병합으로 갱신)
  app["dev_mode"]       bool
  app["runlog"]         RunLog (없으면 engine._runlog 로 폴백)
  app["event_source"]   MockEventSource (DEV, 없으면 engine._event_source 로 폴백)
  app["mode_state"]     ModeState (선택 — 없으면 engine.mode_state/_mode_state 로 폴백)

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
from backend.nl import learn
from backend.nl.llm_assist import llm_parse
from backend.nl.manual import build_model_from_tokens, suggest_roles, tokenize
from backend.nl.parser import parse as nl_parse

log = logging.getLogger("automation_maker.api_v2")

_LLM_MERGE_SCORE = 0.7          # LLM 이 채운 슬롯의 후보 스코어(§7)
_LLM_TRIGGER_CONF = 0.6         # 이 미만이면 LLM 보조 시도

_MAX_SENTENCE = 500             # parse/rules 문장 최대 길이(자)
_MAX_RULES = 200               # 저장 가능한 규칙(루틴) 최대 개수
_MAX_ASSIGNMENTS = 500          # build 토큰 매핑 최대 개수(문장 500자 → 토큰 상한 방어)
_MAX_LEARNED = 500              # 학습 항목(learned_patterns) 최대 개수(무한 성장 방어)

_LLM_BACKENDS = ("off", "api", "cli")


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
    """LLM API 키는 환경변수에서만 읽는다(§4.1). settings.json 에는 저장하지 않는다."""
    return os.environ.get("ANTHROPIC_API_KEY") or ""


def _oauth_token() -> str:
    """구독 CLI 백엔드용 OAuth 토큰. 환경변수에서만 읽는다(§4.1)."""
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or ""


def _cli_available() -> bool:
    """구독 CLI 바이너리 설치 여부(설정 UI 준비상태 표시용)."""
    try:
        from backend.nl import cli_client
        return bool(cli_client.available())
    except Exception:
        return False


def _llm_backend(settings: dict) -> str:
    """LLM 해석 백엔드 선택(§4.5): settings.llm.backend → env LLM_BACKEND → "off"."""
    llm = settings.get("llm") if isinstance(settings, dict) else None
    if isinstance(llm, dict):
        b = llm.get("backend")
        if b in _LLM_BACKENDS:
            return b
    env = os.environ.get("LLM_BACKEND")
    if env in _LLM_BACKENDS:
        return env
    return "off"


def _llm_available(settings: dict) -> bool:
    """현재 선택된 백엔드가 사용 가능한지(§5). off 는 항상 False."""
    backend = _llm_backend(settings)
    if backend == "api":
        return bool(_api_key())
    if backend == "cli":
        return bool(_oauth_token()) or _cli_available()
    return False


def _llm_ready() -> dict:
    """백엔드별 준비상태(§8 UI: 키·토큰·CLI설치 여부)."""
    return {
        "api_key": bool(_api_key()),
        "oauth_token": bool(_oauth_token()),
        "cli": _cli_available(),
    }


# ---------------------------------------------------------------------------
# Phase 3C — 학습 설정 헬퍼 (저장소/정규화는 backend.nl.learn 이 담당)
# ---------------------------------------------------------------------------
def _learn_settings(settings: dict) -> dict:
    learn = settings.get("learn") if isinstance(settings, dict) else None
    return learn if isinstance(learn, dict) else {}


def _learn_enabled(settings: dict) -> bool:
    """학습 기능 on/off(사용자 선택 옵션, 기본 off, §3)."""
    return bool(_learn_settings(settings).get("enabled"))


def _learn_available(settings: dict) -> bool:
    """학습이 실제로 동작 가능한지: 기존 llm 백엔드가 준비돼야 정규화할 수 있다(§3)."""
    return _llm_available(settings)


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


def _mode_state(app: web.Application):
    """ModeState 접근(§1.2). app["mode_state"] → engine.mode_state → engine._mode_state."""
    ms = app.get("mode_state")
    if ms is not None:
        return ms
    engine = app.get("engine")
    if engine is None:
        return None
    return getattr(engine, "mode_state", None) or getattr(engine, "_mode_state", None)


def _mode_names(app: web.Application) -> set:
    """설정에 정의된 모드 이름 집합(검증용, §1.3)."""
    settings = app["settings_store"].data
    modes = settings.get("modes") if isinstance(settings, dict) else None
    return set(modes.keys()) if isinstance(modes, dict) else set()


def _modes_from_settings(app: web.Application) -> list:
    """mode_state 미배선 시 settings 에서 직접 모드 목록을 구성한다(폴백)."""
    settings = app["settings_store"].data
    defs = settings.get("modes") if isinstance(settings, dict) else None
    out = []
    if isinstance(defs, dict):
        for name, d in defs.items():
            d = d if isinstance(d, dict) else {}
            initial = "on" if d.get("initial") == "on" else "off"
            out.append({
                "name": name,
                "state": initial,   # 런타임 미배선 → 초기값을 상태로 노출
                "initial": initial,
                # v3 신형(on_action) 우선, v2 레거시(action) 폴백
                "has_on_action": bool(d.get("on_action") or d.get("action")),
                "has_off_action": bool(d.get("off_action")),
            })
    return out


def _inventory(app: web.Application) -> dict:
    gz = app["gazetteer_fn"]()
    inv = getattr(gz, "inventory", None)
    return inv if isinstance(inv, dict) else {"areas": [], "entities": [], "zones": []}


# ---------------------------------------------------------------------------
# GET api/v2/status
# ---------------------------------------------------------------------------
async def handle_status(request: web.Request) -> web.Response:
    st = request.app["engine"].status()
    if not isinstance(st, dict):
        st = {}
    st.setdefault("modes", {})            # SPEC-V3 §1.4 (엔진이 채우지만 방어적으로 보장)
    settings = request.app["settings_store"].data or {}
    st["llm_backend"] = _llm_backend(settings)
    st["llm_available"] = _llm_available(settings)
    st["learn_enabled"] = _learn_enabled(settings)
    st["learn_available"] = _learn_available(settings)
    return _json(st)


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

    # L2(Phase 3B): 규칙(L1)이 not ok 일 때만 로컬 학습→템플릿 매처로 흡수한다.
    # not ok 게이트가 오탐을 억제한다(실측: 매처는 규칙이 실패할 때만 채택). ok 인
    # 결과는 절대 덮어쓰지 않으므로 기존 파서 동작에 회귀가 없다.
    used_matcher = None
    if not result.get("ok"):
        if _adopt_learned(request.app, sentence, result, gz):
            used_matcher = "learned"
        elif _adopt_matcher(request.app, sentence, result, gz):
            used_matcher = "pattern"

    # L3: 로컬(L1+L2)이 부족하고 매처가 흡수하지 못했을 때만 LLM 보조(§4.5).
    backend = _llm_backend(settings)
    used_llm = False
    if used_matcher is None:
        needs_help = (result.get("confidence", 0.0) < _LLM_TRIGGER_CONF
                      or _has_unresolved(result))
        if needs_help and backend != "off":
            used_llm = await _try_llm_merge(sentence, result, gz, settings, backend)

    result["used_llm"] = used_llm
    result["llm_backend"] = backend if used_llm else None
    result["used_matcher"] = used_matcher
    result["source"] = used_matcher or ("rule" if result.get("ok") else None)
    return _json(result)


# ---------------------------------------------------------------------------
# L2 채택 헬퍼 (로컬 학습 / 템플릿 매처) — 규칙이 not ok 일 때만 호출된다.
# ---------------------------------------------------------------------------
def _apply_matched_model(result: dict, model: dict, summary: str, confidence: float,
                         **extra) -> None:
    """매처/학습 model 을 파싱 결과에 반영(공통). 미해결 칩 제거 → 저장 가능 상태."""
    result["model"] = model
    result["chips"] = []
    result["unmatched"] = []
    result["ok"] = True
    result["confidence"] = round(min(max(confidence, 0.0), 1.0), 3)
    result["summary"] = summary or ""  # 실패 파싱의 낡은 요약을 덮어쓴다
    result.update(extra)


def _adopt_learned(app: web.Application, sentence: str, result: dict, gz) -> bool:
    """로컬 학습 항목을 채택한다(learn.LearnedStore.match → model 사본).

    학습 이후 기기가 사라졌을 수 있으므로, 사용 시점에 엔티티 실존을 다시 검증한다
    (§3: 재기동 재검증에 더해 방어적 이중 검증). match 는 원문/정규형 완전일치 또는
    문자 3-gram 근접 재매칭으로 model 을 돌려주고 hits 를 올린다.
    """
    store = app.get("learned_store")
    if store is None:
        return False
    try:
        model = store.match(sentence)
    except Exception:
        log.exception("학습 저장소 매칭 중 오류")
        return False
    if not isinstance(model, dict) or not model:
        return False
    if validate_rule_model(model, _inventory(app), _mode_names(app)):
        return False
    _apply_matched_model(result, model, "", 1.0)
    return True


def _adopt_matcher(app: web.Application, sentence: str, result: dict, gz) -> bool:
    """L2 템플릿 매처 결과를 채택한다. 매처 부재/오류/검증 실패는 조용히 False."""
    matcher = app.get("template_matcher")
    if matcher is None:
        return False
    try:
        m = matcher.match(sentence)
    except Exception:
        log.exception("템플릿 매처 실행 중 오류")
        return False
    if not m or not isinstance(m.get("model"), dict):
        return False
    model = m["model"]
    if validate_rule_model(model, _inventory(app), _mode_names(app)):
        return False
    _apply_matched_model(result, model, "",
                         float(m.get("score") or _LLM_MERGE_SCORE),
                         matched_id=m.get("matched_id"))
    return True


def _has_unresolved(result: dict) -> bool:
    return any(c.get("status") == "unresolved" for c in result.get("chips") or [])


async def _try_llm_merge(sentence, result, gz, settings, backend) -> bool:
    """로컬 미해결 슬롯을 LLM 결과로 채운다(§4.5). 실패는 조용히 무시하고 로컬 유지.

    백엔드(api/cli)에 따라 api_key/oauth_token 을 환경에서 읽어 전달한다.
    키/토큰 자체는 로그에 남기지 않는다.
    """
    try:
        digest = {
            "entities": getattr(gz, "inventory", {}).get("entities", []),
            "persons": settings.get("persons") or {},
            "modes": settings.get("modes") or {},
        }
        llm = await llm_parse(sentence, digest, settings, backend=backend,
                              api_key=_api_key(), oauth_token=_oauth_token())
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
# 모드 자동 등록 (§1.3·§5) — 규칙 저장 시 참조 모드가 settings 에 없으면 즉석 등록
# ---------------------------------------------------------------------------
def _referenced_modes(model: dict) -> set:
    """model(서브룰 포함) 전체에서 mode/set_mode 노드가 참조하는 모드 이름을 수집한다."""
    found: set = set()
    stack = [model]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") in ("mode", "set_mode"):
                name = node.get("mode")
                if isinstance(name, str) and name:
                    found.add(name)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _autoregister_modes(app: web.Application, model: dict) -> list:
    """model 이 참조하는 모드 중 settings 에 없는 것을 initial off 로 자동 등록한다(§1.3·§5).

    settings 인플레이스 갱신 → save_soon → gazetteer 재빌드 → mode_state.sync_settings(있으면).
    검증 前에 호출해야 mode 노드가 "설정에 없는 모드" 오류 없이 통과한다. 반환: 경고 메시지 목록.
    """
    refs = _referenced_modes(model)
    if not refs:
        return []
    store = app["settings_store"]
    settings = store.data
    if not isinstance(settings, dict):
        settings = {}
        store.data = settings
    modes = settings.get("modes")
    if not isinstance(modes, dict):
        modes = {}
        settings["modes"] = modes
    added = []
    for name in sorted(refs):
        if name not in modes:
            modes[name] = {"initial": "off", "on_action": None, "off_action": None}
            added.append(name)
    if not added:
        return []
    store.save_soon()
    try:
        app["gazetteer_fn"]()               # 새 모드 표면형 반영
    except Exception:
        log.exception("gazetteer 재빌드 실패(모드 자동 등록)")
    ms = _mode_state(app)
    if ms is not None and hasattr(ms, "sync_settings"):
        try:
            ms.sync_settings(settings)      # 새 모드를 초기값으로 런타임에 추가
        except Exception:
            log.exception("mode_state 동기화 실패(모드 자동 등록)")
    return [f"설정에 없던 모드를 새로 추가했어요(초기값 꺼짐): {n}" for n in added]


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
    # 모드 자동 등록은 검증 前 settings 반영(§5).
    warnings = _autoregister_modes(request.app, model)
    errors = validate_rule_model(model, _inventory(request.app), _mode_names(request.app))
    if errors:
        return _error("invalid_rule", "입력한 규칙이 올바르지 않습니다.", 400, errors=errors)
    rule = _build_rule(body, model)
    saved = request.app["rule_store"].upsert(rule)
    request.app["engine"].reload_rule(saved["id"])
    return _json({"rule": saved, "warnings": warnings})


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
    warnings = _autoregister_modes(request.app, model)
    errors = validate_rule_model(model, _inventory(request.app), _mode_names(request.app))
    if errors:
        return _error("invalid_rule", "입력한 규칙이 올바르지 않습니다.", 400, errors=errors)
    rule = _build_rule(body, model)
    rule["id"] = rid
    # enabled 는 토글 전용 필드이므로 편집 저장 시 기존 값을 보존한다.
    rule["enabled"] = existing.get("enabled", True)
    saved = store.upsert(rule)
    request.app["engine"].reload_rule(rid)
    return _json({"rule": saved, "warnings": warnings})


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
# 모드 (SPEC-V3 §1.4)
# ---------------------------------------------------------------------------
async def handle_modes_list(request: web.Request) -> web.Response:
    """GET api/v2/modes → {"modes":[{name,state,initial,has_on_action,has_off_action}]}."""
    ms = _mode_state(request.app)
    if ms is None:
        return _json({"modes": _modes_from_settings(request.app)})
    modes = []
    for name in ms.names():
        d = ms.definition(name) if hasattr(ms, "definition") else {}
        modes.append({
            "name": name,
            "state": ms.get(name),
            "initial": d.get("initial", "off"),
            "has_on_action": bool(d.get("on_action")),
            "has_off_action": bool(d.get("off_action")),
        })
    return _json({"modes": modes})


async def handle_mode_toggle(request: web.Request) -> web.Response:
    """POST api/v2/modes/{name} body {"on":bool} → engine.set_mode → {"modes":{name:state}}."""
    name = request.match_info["name"]
    body = await request.json()
    on = bool((body or {}).get("on"))
    engine = request.app["engine"]
    engine.set_mode(name, on, "manual")
    ms = _mode_state(request.app)
    if ms is not None:
        modes = ms.snapshot()
    else:
        st = engine.status()
        modes = st.get("modes", {}) if isinstance(st, dict) else {}
    return _json({"modes": modes})


# ---------------------------------------------------------------------------
# 수동 단어 매핑 (SPEC-V3 §3.4)
# ---------------------------------------------------------------------------
async def handle_tokenize(request: web.Request) -> web.Response:
    """POST api/v2/tokenize body {"sentence"} → {"tokens":[...], "suggestions":[...]}."""
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    sentence = body.get("sentence")
    if not isinstance(sentence, str):
        return _error("bad_request", "토큰화할 문장을 입력해 주세요.", 400)
    if len(sentence) > _MAX_SENTENCE:
        return _error("bad_request", f"문장이 너무 길어요(최대 {_MAX_SENTENCE}자).", 400)
    gz = request.app["gazetteer_fn"]()
    settings = request.app["settings_store"].data or {}
    tokens = tokenize(sentence)
    suggestions = suggest_roles(tokens, gz, settings)
    return _json({"tokens": tokens, "suggestions": suggestions})


async def handle_build(request: web.Request) -> web.Response:
    """POST api/v2/build body {"sentence","assignments"} → build_model_from_tokens 결과."""
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    sentence = body.get("sentence")
    if isinstance(sentence, str) and len(sentence) > _MAX_SENTENCE:
        return _error("bad_request", f"문장이 너무 길어요(최대 {_MAX_SENTENCE}자).", 400)
    assignments = body.get("assignments")
    if not isinstance(assignments, list):
        return _error("bad_request", "토큰 매핑(assignments)을 배열로 보내 주세요.", 400)
    if len(assignments) > _MAX_ASSIGNMENTS:
        return _error("bad_request", "토큰이 너무 많아요.", 400)
    if not all(isinstance(a, dict) for a in assignments):
        return _error("bad_request", "토큰 매핑 항목은 객체(object)여야 해요.", 400)
    settings = request.app["settings_store"].data or {}
    inventory = _inventory(request.app)
    result = build_model_from_tokens(assignments, inventory, settings)
    return _json(result)


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
def _augment_settings(settings: dict, out: dict) -> dict:
    """설정 응답에 LLM 백엔드·준비상태를 덧붙인다(§5)."""
    out["llm_available"] = _llm_available(settings)
    out["llm_backend"] = _llm_backend(settings)
    out["llm_ready"] = _llm_ready()
    out["learn_enabled"] = _learn_enabled(settings)
    out["learn_available"] = _learn_available(settings)
    return out


async def handle_settings_get(request: web.Request) -> web.Response:
    data = request.app["settings_store"].data
    settings = data if isinstance(data, dict) else {}
    out = dict(settings)
    return _json(_augment_settings(settings, out))


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
        if key in ("llm_available", "llm_backend", "llm_ready",
                   "learn_available", "learn_enabled"):
            continue  # 계산 필드는 저장하지 않는다(learn 원본 dict 는 저장)
        current[key] = val
    store.save_soon()
    request.app["gazetteer_fn"]()  # gazetteer 재빌드(새 persons/modes/aliases 반영)
    # segments 경계가 바뀌면 엔진의 경계 타이머를 재스케줄한다(§7). reschedule_boundary 는
    # 엔진 에이전트가 병렬로 추가 중인 공개 메서드이므로 hasattr 로 방어적으로 호출한다.
    if "segments" in body and current.get("segments") != old_segments:
        engine = request.app.get("engine")
        if engine is not None and hasattr(engine, "reschedule_boundary"):
            engine.reschedule_boundary()
    # 모드 정의가 바뀌면 mode_state 를 동기화한다(§1.2 — 새 모드 초기화·삭제 모드 제거).
    if "modes" in body:
        ms = _mode_state(request.app)
        if ms is not None and hasattr(ms, "sync_settings"):
            try:
                ms.sync_settings(current)
            except Exception:
                log.exception("mode_state 동기화 실패(설정 저장)")
    out = dict(current)
    return _json(_augment_settings(current, out))


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
# Phase 3C — 학습 분석 (정규화·재파싱·검증은 backend.nl.learn 이 담당)
# ---------------------------------------------------------------------------
def _ws(s) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()) if isinstance(s, str) else ""


async def _learn_analyze(app: web.Application, sentence: str,
                         settings: dict) -> dict | None:
    """learn.analyze(§3) 를 호출해 UI 응답 형태로 변환한다.

    learn.analyze 는 구독 claude CLI 로 문장을 표준 문형으로 재작성 → 규칙(L1)+매처(L2)로
    재파싱 → 엔티티 실존·환각 검증까지 수행하고 {normalized, model, template_id, ok,
    warnings, entities} 를 돌려준다(정규화 자체 실패 시 None). 여기서는 미리보기 요약을
    로컬 재파싱(무비용)으로 덧붙인다.
    반환: {normalized, model, template_id, entities, preview, summary, warnings, ok} | None.
    """
    gz = app["gazetteer_fn"]()
    inventory = _inventory(app)
    res = await learn.analyze(sentence, gz, settings, inventory,
                              oauth_token=_oauth_token())
    if res is None:
        return None
    model = res.get("model") or {}
    normalized = res.get("normalized") or sentence
    summary = ""
    try:
        summary = (nl_parse(normalized, gz, settings) or {}).get("summary") or ""
    except Exception:
        log.exception("학습 미리보기 요약 계산 실패(무시)")
    return {
        "normalized": normalized,
        "model": model,
        "template_id": res.get("template_id"),
        "entities": res.get("entities") or [],
        "preview": summary,
        "summary": summary,
        "warnings": list(res.get("warnings") or []),
        "ok": bool(res.get("ok")),
    }


# ---------------------------------------------------------------------------
# Phase 3C — 학습 엔드포인트
# ---------------------------------------------------------------------------
async def handle_learn(request: web.Request) -> web.Response:
    """POST api/v2/learn {sentence} → {normalized, model, preview, warnings, ok}.

    학습 비활성/백엔드 미준비 시 409(한국어). 정규화 자체 실패 시 502.
    """
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    sentence = body.get("sentence")
    if not isinstance(sentence, str) or not sentence.strip():
        return _error("bad_request", "분석할 문장을 입력해 주세요.", 400)
    if len(sentence) > _MAX_SENTENCE:
        return _error("bad_request", f"문장이 너무 길어요(최대 {_MAX_SENTENCE}자).", 400)
    settings = request.app["settings_store"].data or {}
    if not _learn_enabled(settings):
        return _error("learn_disabled", "학습 기능이 꺼져 있어요. 설정에서 켜 주세요.", 409)
    backend = _llm_backend(settings)
    if backend == "off" or not _llm_available(settings):
        return _error("learn_unavailable",
                      "AI 백엔드가 준비되지 않았어요. 설정에서 API 키 또는 구독 CLI를 확인해 주세요.",
                      409)
    result = await _learn_analyze(request.app, sentence.strip(), settings)
    if result is None:
        return _error("learn_failed",
                      "문장을 표준 형태로 바꾸지 못했어요. 잠시 후 다시 시도해 주세요.", 502)
    return _json(result)


async def handle_learn_confirm(request: web.Request) -> web.Response:
    """POST api/v2/learn/confirm {sentence, normalized, model} → {ok, learned_id}.

    검증(validate_rule_model + 엔티티 실존) 통과 시에만 learned_patterns 에 저장한다.
    """
    body = await request.json()
    if not isinstance(body, dict):
        _raise_bad_request()
    sentence = body.get("sentence")
    normalized = body.get("normalized")
    model = body.get("model")
    if not isinstance(sentence, str) or not sentence.strip():
        return _error("bad_request", "학습할 원문 문장이 필요해요.", 400)
    if not isinstance(model, dict):
        return _error("bad_request", "학습할 규칙(model)이 필요해요.", 400)
    if not isinstance(normalized, str) or not normalized.strip():
        normalized = sentence
    # 길이 상한(analyze 의 _MAX_SENTENCE 와 대칭). 프런트를 우회한 과대 페이로드가
    # learned_patterns.yaml 을 부풀리는 것을 막는다.
    if len(sentence) > _MAX_SENTENCE or len(normalized) > _MAX_SENTENCE:
        return _error("bad_request", f"문장이 너무 길어요(최대 {_MAX_SENTENCE}자).", 400)
    settings = request.app["settings_store"].data or {}
    if not _learn_enabled(settings):
        return _error("learn_disabled", "학습 기능이 꺼져 있어요. 설정에서 켜 주세요.", 409)
    store = request.app.get("learned_store")
    if store is None:
        return _error("learn_unavailable", "학습 저장소를 사용할 수 없어요.", 409)
    # 저장 게이트: validate_rule_model 이 서비스 도메인 화이트리스트 · service↔대상 엔티티
    # 도메인 호환성 · null/빈 대상 · 엔티티 실존 · 모드 존재까지 서버측에서 강제한다(§4.2).
    # 정규형↔원문 환각(_hallucination_ok)은 analyze 단계에서 이미 검사한다.
    errors = validate_rule_model(model, _inventory(request.app),
                                 _mode_names(request.app))
    if errors:
        return _error("invalid_rule", "학습할 규칙이 올바르지 않습니다.", 400, errors=errors)
    entries = store.all()
    already = any(_ws(e.get("raw")) == _ws(sentence) for e in entries)
    if len(entries) >= _MAX_LEARNED and not already:
        return _error("too_many_learned",
                      f"학습 항목이 너무 많아요(최대 {_MAX_LEARNED}개). 설정에서 정리해 주세요.", 400)
    # LearnedStore.add(raw, normalized, model, entities): 같은 원문은 교체(무한 성장 방어).
    entities = learn.model_entities(model)
    entry = store.add(sentence.strip(), normalized, model, entities)
    return _json({"ok": True, "learned_id": entry["id"]})


async def handle_learned_list(request: web.Request) -> web.Response:
    """GET api/v2/learned → {"learned":[...]}."""
    store = request.app.get("learned_store")
    return _json({"learned": store.all() if store is not None else []})


async def handle_learned_delete(request: web.Request) -> web.Response:
    """DELETE api/v2/learned/{id} → {"ok":bool}."""
    store = request.app.get("learned_store")
    ok = store.delete(request.match_info["id"]) if store is not None else False
    return _json({"ok": ok})


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
    # SPEC-V3: 모드 · 수동 단어 매핑
    r.add_get("/api/v2/modes", handle_modes_list)
    r.add_post("/api/v2/modes/{name}", handle_mode_toggle)
    r.add_post("/api/v2/tokenize", handle_tokenize)
    r.add_post("/api/v2/build", handle_build)
    r.add_get("/api/v2/runlog", handle_runlog)
    r.add_get("/api/v2/settings", handle_settings_get)
    r.add_put("/api/v2/settings", handle_settings_put)
    # Phase 3C: CLI 학습
    r.add_post("/api/v2/learn", handle_learn)
    r.add_post("/api/v2/learn/confirm", handle_learn_confirm)
    r.add_get("/api/v2/learned", handle_learned_list)
    r.add_delete("/api/v2/learned/{id}", handle_learned_delete)
    r.add_post("/api/v2/dev/state", handle_dev_state)
