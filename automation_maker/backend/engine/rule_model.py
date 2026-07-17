"""RuleModel 검증 (§6.3 + SPEC-V3 §1.3·§2.3).

v1 검증에 §3 확장 노드를 포함하고 sun/template 을 거부한다. SPEC-V3 에서
subrules 순회와 mode 트리거/조건/set_mode 액션 검증을 추가한다.
"""
from __future__ import annotations

import re
from datetime import date

from ..automation_builder import KNOWN_SERVICES, _validate_action

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

_DAY_TYPES = {"weekday", "weekend", "holiday"}
_SEASONS = {"spring", "summer", "autumn", "winter"}
_SEGMENT_KEYS = {"dawn", "morning", "day", "evening", "night"}
_MODE_STATES = {"on", "off"}

# v2 엔진이 미지원하는 유형(파서가 생성 금지, 검증에서 거부)
_UNSUPPORTED = {"template"}

_SUN_EVENTS = {"sunrise", "sunset"}
_MAX_SUN_OFFSET = 43200  # ±12시간(초)
_WEEKDAYS_SET = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def _check_int_offset(v, path, errors) -> None:
    """sun offset(초): int(bool 제외), |v| ≤ 43200. None/부재는 통과(생략 가능)."""
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, int):
        errors.append({"path": path, "message": "오프셋은 정수(초)여야 합니다."})
    elif abs(v) > _MAX_SUN_OFFSET:
        errors.append({"path": path, "message": "오프셋은 ±12시간(43200초) 이내여야 합니다."})


def _check_time_pattern(t, path, errors) -> None:
    """time_pattern 주기: hours|minutes|seconds 중 **정확히 1개**, 정수 N≥1,
    minutes/seconds ≤59, hours ≤23(HA `/N` 동형, 엔진 v2 방언은 정수로 저장)."""
    present = [k for k in ("hours", "minutes", "seconds") if t.get(k) is not None]
    if len(present) != 1:
        errors.append({"path": path,
                       "message": "시/분/초 주기 중 정확히 하나만 지정해 주세요."})
        return
    key = present[0]
    v = t.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        errors.append({"path": path + "." + key,
                       "message": "주기는 1 이상의 정수여야 합니다."})
        return
    limit = 23 if key == "hours" else 59
    if v > limit:
        errors.append({"path": path + "." + key,
                       "message": f"주기는 {limit} 이하여야 합니다."})

# 액션에서 실행을 허용하는 서비스 도메인 화이트리스트(보안). API 직접 호출 시 임의 서비스
# (hassio.addon_stop 등)가 admin 권한으로 실행되는 것을 막는다.
# KNOWN_SERVICES 의 도메인 + scene/script/input_boolean/notify.
_ALLOWED_ACTION_DOMAINS = set(KNOWN_SERVICES) | {
    "scene", "script", "input_boolean", "notify"}

# turn_on/turn_off/toggle 처럼 대상 엔티티 도메인을 가리지 않는 서비스 도메인. 그 외 화이트리스트
# 도메인은 모두 "service 도메인 == 대상 엔티티 도메인" 규약을 따르므로(light.turn_on→light.*,
# fan.turn_on→fan.*), 아래 목록에 없는 도메인은 대상 엔티티 도메인 일치를 강제한다.
_DOMAIN_AGNOSTIC_SERVICE_DOMAINS = {"homeassistant", "notify"}


def _p(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


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


def _check_presence(node, path, errors, valid_ids, allowed_quants, for_quants) -> None:
    """presence_agg 공용 검증(트리거/조건, SPEC-SCHEMA-90 §1.3·§2.5).

    allowed_quants = 이 위치에서 허용하는 quant 집합(트리거 first/last/any/all,
    조건 none/any/all). for_quants = for(지속시간)를 허용하는 quant 집합(트리거는
    last/all 만, 조건은 공집합). persons 는 생략 가능하나 있으면 비어있지 않은 person.*
    목록이고 valid_ids 에 실존해야 한다.
    """
    q = node.get("quant")
    if q not in allowed_quants:
        errors.append({"path": path + ".quant",
                       "message": "인원 양화(quant) 값이 올바르지 않습니다."})
    persons = node.get("persons")
    if persons is not None:
        if (not isinstance(persons, list) or not persons
                or any(not isinstance(p, str) for p in persons)):
            errors.append({"path": path + ".persons",
                           "message": "사람(person) 목록을 올바르게 지정해 주세요."})
        else:
            for p in persons:
                if not p.startswith("person."):
                    errors.append({"path": path + ".persons",
                                   "message": f"사람(person) 엔티티가 아니에요: {p}"})
                elif valid_ids and p not in valid_ids:
                    errors.append({"path": path + ".persons",
                                   "message": f"존재하지 않는 사람이에요: {p}"})
    fr = node.get("for")
    if fr is not None:
        if q not in for_quants:
            errors.append({"path": path + ".for",
                           "message": "이 양화에는 지속시간(for)을 지정할 수 없습니다."})
        else:
            _check_duration(fr, path + ".for", errors)


def _check_scope(scope, path, errors) -> None:
    if not isinstance(scope, dict):
        errors.append({"path": path, "message": "대상 범위(scope) 형식이 올바르지 않습니다."})
        return
    if not (scope.get("device_class") or scope.get("domain")
            or scope.get("area_id")):
        errors.append({"path": path, "message": "대상 범위를 지정해 주세요."})


def _check_mode_ref(node, path, errors, mode_names, key) -> None:
    """mode 트리거/조건/set_mode 공통 검증. key = 상태 필드명("to"|"state")."""
    mode = node.get("mode")
    if not isinstance(mode, str) or not mode:
        errors.append({"path": path + ".mode", "message": "모드 이름을 지정해 주세요."})
    elif mode_names is not None and mode not in mode_names:
        errors.append({"path": path + ".mode", "message": f"설정에 없는 모드예요: {mode}"})
    if node.get(key) not in _MODE_STATES:
        errors.append({"path": _p(path, key), "message": "모드 상태는 켬(on) 또는 끔(off)이어야 해요."})


def _validate_trigger(t, path, errors, valid_ids, mode_names=None) -> None:
    if not isinstance(t, dict):
        errors.append({"path": path, "message": "트리거 형식이 올바르지 않습니다."})
        return
    typ = t.get("type")
    if typ in _UNSUPPORTED:
        errors.append({"path": path + ".type",
                       "message": "이 트리거 유형(template)은 지원하지 않습니다."})
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
    elif typ == "sun":
        if t.get("event") not in _SUN_EVENTS:
            errors.append({"path": path + ".event", "message": "일출 또는 일몰을 선택해 주세요."})
        _check_int_offset(t.get("offset"), path + ".offset", errors)
    elif typ == "time_pattern":
        _check_time_pattern(t, path, errors)
    elif typ == "presence_agg":
        _check_presence(t, path, errors, valid_ids,
                        {"first", "last", "any", "all"}, {"last", "all"})
    elif typ == "segment":
        if t.get("to") not in _SEGMENT_KEYS:
            errors.append({"path": path + ".to", "message": "시간대 값이 올바르지 않습니다."})
    elif typ == "mode":
        _check_mode_ref(t, path, errors, mode_names, "to")
    elif typ == "time":
        if not _TIME_RE.match(str(t.get("at") or "")):
            errors.append({"path": path + ".at", "message": "시각을 HH:MM 형식으로 입력해 주세요."})
    elif typ == "zone":
        _need_entity(t, path, errors, valid_ids)
        if not t.get("zone"):
            errors.append({"path": path + ".zone", "message": "구역(zone)을 지정해 주세요."})
    else:
        errors.append({"path": path + ".type", "message": "지원하지 않는 트리거 유형입니다."})


def _validate_condition(c, path, errors, valid_ids, n_triggers, mode_names=None) -> None:
    if not isinstance(c, dict):
        errors.append({"path": path, "message": "조건 형식이 올바르지 않습니다."})
        return
    typ = c.get("type")
    if typ in _UNSUPPORTED:
        errors.append({"path": path + ".type",
                       "message": "이 조건 유형(template)은 지원하지 않습니다."})
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
    elif typ == "sun_window":
        for key in ("after", "before"):
            if c.get(key) not in _SUN_EVENTS:
                errors.append({"path": path + "." + key,
                               "message": "일출 또는 일몰을 지정해 주세요."})
        _check_int_offset(c.get("after_offset"), path + ".after_offset", errors)
        _check_int_offset(c.get("before_offset"), path + ".before_offset", errors)
    elif typ == "weekday":
        days = c.get("days")
        if (not isinstance(days, list) or not days
                or any(d not in _WEEKDAYS_SET for d in days)):
            errors.append({"path": path + ".days",
                           "message": "요일을 하나 이상 올바르게 지정해 주세요."})
        if not isinstance(c.get("negate"), bool):
            errors.append({"path": path + ".negate",
                           "message": "요일 제외 여부(negate)는 참/거짓이어야 합니다."})
    elif typ == "day_of_month":
        days = c.get("days")
        ok_list = (isinstance(days, list) and days
                   and all(isinstance(d, int) and not isinstance(d, bool)
                           and 1 <= d <= 31 for d in days))
        if days != "last" and not ok_list:
            errors.append({"path": path + ".days",
                           "message": "1~31 사이의 날짜 목록 또는 'last'(말일)를 지정해 주세요."})
    elif typ == "interval_anchor":
        if c.get("unit") != "week":
            errors.append({"path": path + ".unit",
                           "message": "간격 단위는 주(week)만 지원합니다."})
        iv = c.get("interval")
        if isinstance(iv, bool) or not isinstance(iv, int) or iv < 2:
            errors.append({"path": path + ".interval",
                           "message": "간격은 2 이상의 정수여야 합니다."})
        try:
            date.fromisoformat(str(c.get("anchor")))
        except (ValueError, TypeError):
            errors.append({"path": path + ".anchor",
                           "message": "기준일은 YYYY-MM-DD 형식이어야 합니다."})
    elif typ == "presence_agg":
        _check_presence(c, path, errors, valid_ids, {"none", "any", "all"}, set())
    elif typ == "time_segment":
        _need_list(c.get("segments"), _SEGMENT_KEYS, path + ".segments", errors, "시간대")
    elif typ == "day_type":
        _need_list(c.get("types"), _DAY_TYPES, path + ".types", errors, "요일 구분")
    elif typ == "season":
        _need_list(c.get("seasons"), _SEASONS, path + ".seasons", errors, "계절")
    elif typ == "mode":
        _check_mode_ref(c, path, errors, mode_names, "state")
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
                _validate_condition(s, f"{path}.conditions[{i}]", errors, valid_ids,
                                    n_triggers, mode_names)
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


def _validate_action_node(a, path, errors, n_triggers, mode_names) -> None:
    """set_mode 는 엔진 전용 액션이라 별도 검증, 나머지는 v1 검증기로 위임한다."""
    if isinstance(a, dict) and a.get("type") == "set_mode":
        _check_mode_ref(a, path, errors, mode_names, "to")
        return
    _validate_action(a, path, errors, n_triggers)


def _scan_service_actions(actions, base_path, errors, valid_ids) -> None:
    """중첩 액션(choose/if/repeat/parallel 등)까지 훑어 service 액션을 검사한다.

    - 미허용 도메인의 service 액션을 거부(보안 화이트리스트).
    - target.entity_id 가 명시됐는데 비어 있거나 None/빈문자를 포함하면 거부(매처·LLM 이
      미해석 slot 을 [None]/[] 로 흘려보내는 것 방어).
    - service 도메인과 대상 엔티티 도메인의 호환성 검사(fan.turn_on 을 light 엔티티에 거는
      매처 오매핑을 차단 — 이 함수가 매처/LLM/학습 모든 저장 경로의 유일한 안전망).
    - target.entity_id(리스트/스칼라 모두)가 인벤토리에 실존하는지 검증(SPEC-V3 §4.2).
      로컬/LLM/수동 모든 저장 경로가 존재하지 않는 대상을 저장하지 못하게 막는다.
      valid_ids 가 비어 있으면(인벤토리 미제공) 실존 검사만 건너뛴다 — 널/도메인 검사는
      엔티티 id 접두사만으로 판정하므로 인벤토리 없이도 수행한다.
    """
    stack = list(actions)
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "service":
                action = str(node.get("action") or "")
                domain = action.split(".", 1)[0] if "." in action else ""
                if domain and domain not in _ALLOWED_ACTION_DOMAINS:
                    errors.append({"path": base_path, "message": f"지원하지 않는 동작이에요: {action}"})
                tgt = node.get("target")
                has_target = isinstance(tgt, dict) and "entity_id" in tgt
                ids = tgt.get("entity_id") if isinstance(tgt, dict) else None
                as_list = ids if isinstance(ids, list) else ([ids] if ids is not None else [])
                # (a) 명시된 대상이 비었거나 None/빈문자를 포함 → 거부(미해석 slot 방어)
                if has_target and (not as_list or any(not i for i in as_list)):
                    errors.append({"path": base_path + ".target.entity_id",
                                   "message": "대상 엔티티가 비어 있어요."})
                present = [i for i in as_list if i]
                # (b) service 도메인 ↔ 대상 엔티티 도메인 호환성(도메인 무관 서비스는 예외)
                if domain and domain not in _DOMAIN_AGNOSTIC_SERVICE_DOMAINS:
                    mism = [i for i in present
                            if "." in i and i.split(".", 1)[0] != domain]
                    if mism:
                        errors.append({"path": base_path + ".target.entity_id",
                                       "message": f"동작과 대상 기기 종류가 맞지 않아요: {action} ↔ "
                                                  + ", ".join(mism)})
                # (c) 실존 검사(인벤토리 있을 때만)
                if valid_ids:
                    missing = [i for i in present if i not in valid_ids]
                    if missing:
                        errors.append({"path": base_path + ".target.entity_id",
                                       "message": "존재하지 않는 엔티티입니다: " + ", ".join(missing)})
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _validate_subrule(sub, prefix, errors, valid_ids, mode_names) -> None:
    if not isinstance(sub, dict):
        errors.append({"path": prefix or "subrule", "message": "규칙 형식이 올바르지 않습니다."})
        return

    if sub.get("condition_mode", "and") not in ("and", "or"):
        errors.append({"path": _p(prefix, "condition_mode"),
                       "message": "조건 결합 방식이 올바르지 않습니다."})

    triggers = sub.get("triggers")
    n_triggers = len(triggers) if isinstance(triggers, list) else 0
    tbase = _p(prefix, "triggers")
    if not isinstance(triggers, list) or not triggers:
        errors.append({"path": tbase, "message": "트리거를 하나 이상 추가해 주세요."})
    else:
        for i, t in enumerate(triggers):
            _validate_trigger(t, f"{tbase}[{i}]", errors, valid_ids, mode_names)

    conditions = sub.get("conditions", [])
    cbase = _p(prefix, "conditions")
    if isinstance(conditions, list):
        for i, c in enumerate(conditions):
            _validate_condition(c, f"{cbase}[{i}]", errors, valid_ids, n_triggers, mode_names)

    actions = sub.get("actions")
    abase = _p(prefix, "actions")
    if not isinstance(actions, list) or not actions:
        errors.append({"path": abase, "message": "실행할 동작을 하나 이상 추가해 주세요."})
    else:
        for i, a in enumerate(actions):
            _validate_action_node(a, f"{abase}[{i}]", errors, n_triggers, mode_names)
        _scan_service_actions(actions, abase, errors, valid_ids)


def validate_rule_model(model: dict, inventory, mode_names=None) -> list[dict]:
    """RuleModel 검증. mode_names 가 주어지면 mode 노드가 참조하는 모드 존재까지 확인한다
    (None 이면 존재 검증은 건너뛴다 — 자동 등록은 api_v2 담당, SPEC-V3 §1.3)."""
    errors: list[dict] = []
    if not isinstance(model, dict):
        return [{"path": "", "message": "규칙 모델 형식이 올바르지 않습니다."}]

    valid_ids = _entity_ids(inventory)

    subrules = model.get("subrules")
    if isinstance(subrules, list):
        if not subrules:
            errors.append({"path": "subrules", "message": "규칙을 하나 이상 추가해 주세요."})
        for si, sub in enumerate(subrules):
            _validate_subrule(sub, f"subrules[{si}]", errors, valid_ids, mode_names)
    else:
        # 하위호환: subrules 가 없으면 최상위 4필드를 단일 서브룰로 검증(경로 접두사 없음)
        _validate_subrule(model, "", errors, valid_ids, mode_names)

    return errors
