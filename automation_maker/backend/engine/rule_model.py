"""RuleModel 검증 (§6.3). v1 검증에 §3 확장 노드 포함, sun/template 거부."""
from __future__ import annotations

import re

from ..automation_builder import KNOWN_SERVICES, _validate_action

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

_DAY_TYPES = {"weekday", "weekend", "holiday"}
_SEASONS = {"spring", "summer", "autumn", "winter"}
_SEGMENT_KEYS = {"dawn", "morning", "day", "evening", "night"}

# v2 엔진이 미지원하는 유형(파서가 생성 금지, 검증에서 거부)
_UNSUPPORTED = {"sun", "template"}

# 액션에서 실행을 허용하는 서비스 도메인 화이트리스트(보안). API 직접 호출 시 임의 서비스
# (hassio.addon_stop 등)가 admin 권한으로 실행되는 것을 막는다.
# KNOWN_SERVICES 의 도메인 + scene/script/input_boolean/notify.
_ALLOWED_ACTION_DOMAINS = set(KNOWN_SERVICES) | {
    "scene", "script", "input_boolean", "notify"}


def _entity_ids(inventory) -> set[str]:
    ents = inventory.get("entities") if isinstance(inventory, dict) else inventory
    return {e.get("entity_id") for e in (ents or []) if e.get("entity_id")}


def _check_duration(d, path, errors) -> None:
    if d is None:
        return
    if not isinstance(d, dict) or not any(
        _num(d.get(k)) for k in ("hours", "minutes", "seconds")
    ):
        errors.append({"path": path, "message": "지속 시간을 입력해 주세요."})


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _check_scope(scope, path, errors) -> None:
    if not isinstance(scope, dict):
        errors.append({"path": path, "message": "대상 범위(scope) 형식이 올바르지 않습니다."})
        return
    if not (scope.get("device_class") or scope.get("domain")
            or scope.get("area_id")):
        errors.append({"path": path, "message": "대상 범위를 지정해 주세요."})


def _validate_trigger(t, path, errors, valid_ids) -> None:
    if not isinstance(t, dict):
        errors.append({"path": path, "message": "트리거 형식이 올바르지 않습니다."})
        return
    typ = t.get("type")
    if typ in _UNSUPPORTED:
        errors.append({"path": path + ".type",
                       "message": "이 트리거 유형(sun/template)은 지원하지 않습니다."})
        return
    if typ in ("state", "numeric_state"):
        _need_entity(t, path, errors, valid_ids)
        if typ == "numeric_state" and t.get("above") is None and t.get("below") is None:
            errors.append({"path": path, "message": "이상 또는 이하 값을 입력해 주세요."})
        _check_duration(t.get("for"), path + ".for", errors) if t.get("for") else None
    elif typ == "state_held":
        _need_entity(t, path, errors, valid_ids)
        if t.get("to") in (None, ""):
            errors.append({"path": path + ".to", "message": "유지할 상태 값을 지정해 주세요."})
        _check_duration(t.get("for"), path + ".for", errors)
    elif typ == "group_held":
        _check_scope(t.get("scope"), path + ".scope", errors)
        if t.get("to") in (None, ""):
            errors.append({"path": path + ".to", "message": "유지할 상태 값을 지정해 주세요."})
        _check_duration(t.get("for"), path + ".for", errors)
    elif typ == "daily":
        if not _HHMM_RE.match(str(t.get("at") or "")):
            errors.append({"path": path + ".at", "message": "시각을 HH:MM 형식으로 입력해 주세요."})
    elif typ == "segment":
        if t.get("to") not in _SEGMENT_KEYS:
            errors.append({"path": path + ".to", "message": "시간대 값이 올바르지 않습니다."})
    elif typ == "time":
        if not _TIME_RE.match(str(t.get("at") or "")):
            errors.append({"path": path + ".at", "message": "시각을 HH:MM 형식으로 입력해 주세요."})
    elif typ == "zone":
        _need_entity(t, path, errors, valid_ids)
        if not t.get("zone"):
            errors.append({"path": path + ".zone", "message": "구역(zone)을 지정해 주세요."})
    else:
        errors.append({"path": path + ".type", "message": "지원하지 않는 트리거 유형입니다."})


def _validate_condition(c, path, errors, valid_ids, n_triggers) -> None:
    if not isinstance(c, dict):
        errors.append({"path": path, "message": "조건 형식이 올바르지 않습니다."})
        return
    typ = c.get("type")
    if typ in _UNSUPPORTED:
        errors.append({"path": path + ".type",
                       "message": "이 조건 유형(sun/template)은 지원하지 않습니다."})
        return
    if typ == "state":
        _need_entity(c, path, errors, valid_ids)
        if c.get("state") in (None, ""):
            errors.append({"path": path + ".state", "message": "상태 값을 입력해 주세요."})
    elif typ == "numeric_state":
        _need_entity(c, path, errors, valid_ids)
        if c.get("above") is None and c.get("below") is None:
            errors.append({"path": path, "message": "이상 또는 이하 값을 입력해 주세요."})
    elif typ == "time":
        if not c.get("after") and not c.get("before") and not c.get("weekday"):
            errors.append({"path": path, "message": "시각 또는 요일을 지정해 주세요."})
    elif typ == "time_segment":
        _need_list(c.get("segments"), _SEGMENT_KEYS, path + ".segments", errors, "시간대")
    elif typ == "day_type":
        _need_list(c.get("types"), _DAY_TYPES, path + ".types", errors, "요일 구분")
    elif typ == "season":
        _need_list(c.get("seasons"), _SEASONS, path + ".seasons", errors, "계절")
    elif typ == "held":
        _need_entity(c, path, errors, valid_ids)
        if c.get("state") in (None, ""):
            errors.append({"path": path + ".state", "message": "상태 값을 입력해 주세요."})
        _check_duration(c.get("for"), path + ".for", errors)
    elif typ == "group_state":
        _check_scope(c.get("scope"), path + ".scope", errors)
        if c.get("state") in (None, ""):
            errors.append({"path": path + ".state", "message": "상태 값을 입력해 주세요."})
        if c.get("for"):
            _check_duration(c.get("for"), path + ".for", errors)
    elif typ == "zone":
        _need_entity(c, path, errors, valid_ids)
        if not c.get("zone"):
            errors.append({"path": path + ".zone", "message": "구역(zone)을 지정해 주세요."})
    elif typ == "trigger":
        tid = c.get("id")
        if not (isinstance(tid, str) and tid.isdigit() and 0 <= int(tid) < n_triggers):
            errors.append({"path": path + ".id", "message": "존재하는 트리거 번호를 지정해 주세요."})
    elif typ in ("and", "or", "not"):
        subs = c.get("conditions")
        if not isinstance(subs, list) or not subs:
            errors.append({"path": path + ".conditions", "message": "하위 조건을 추가해 주세요."})
        else:
            for i, s in enumerate(subs):
                _validate_condition(s, f"{path}.conditions[{i}]", errors, valid_ids, n_triggers)
    else:
        errors.append({"path": path + ".type", "message": "지원하지 않는 조건 유형입니다."})


def _need_entity(node, path, errors, valid_ids) -> None:
    eid = node.get("entity_id")
    if not eid:
        errors.append({"path": path + ".entity_id", "message": "엔티티를 선택해 주세요."})
    elif valid_ids and eid not in valid_ids:
        errors.append({"path": path + ".entity_id", "message": "존재하지 않는 엔티티입니다."})


def _need_list(vals, allowed, path, errors, label) -> None:
    if not isinstance(vals, list) or not vals:
        errors.append({"path": path, "message": f"{label}을(를) 하나 이상 지정해 주세요."})
    elif any(v not in allowed for v in vals):
        errors.append({"path": path, "message": f"{label} 값이 올바르지 않습니다."})


def validate_rule_model(model: dict, inventory) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(model, dict):
        return [{"path": "", "message": "규칙 모델 형식이 올바르지 않습니다."}]

    valid_ids = _entity_ids(inventory)

    if model.get("condition_mode", "and") not in ("and", "or"):
        errors.append({"path": "condition_mode", "message": "조건 결합 방식이 올바르지 않습니다."})

    triggers = model.get("triggers")
    n_triggers = len(triggers) if isinstance(triggers, list) else 0
    if not isinstance(triggers, list) or not triggers:
        errors.append({"path": "triggers", "message": "트리거를 하나 이상 추가해 주세요."})
    else:
        for i, t in enumerate(triggers):
            _validate_trigger(t, f"triggers[{i}]", errors, valid_ids)

    conditions = model.get("conditions", [])
    if isinstance(conditions, list):
        for i, c in enumerate(conditions):
            _validate_condition(c, f"conditions[{i}]", errors, valid_ids, n_triggers)

    actions = model.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append({"path": "actions", "message": "실행할 동작을 하나 이상 추가해 주세요."})
    else:
        for i, a in enumerate(actions):
            _validate_action(a, f"actions[{i}]", errors, n_triggers)
        # 서비스 도메인 화이트리스트(보안): 중첩 액션(choose/if/repeat/parallel 등)까지
        # 훑어 미허용 도메인(hassio.* 등)의 service 액션을 거부한다.
        stack = list(actions)
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("type") == "service":
                    action = str(node.get("action") or "")
                    domain = action.split(".", 1)[0] if "." in action else ""
                    if domain and domain not in _ALLOWED_ACTION_DOMAINS:
                        errors.append({
                            "path": "actions",
                            "message": f"지원하지 않는 동작이에요: {action}"})
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    return errors
