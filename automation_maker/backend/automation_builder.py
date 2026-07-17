"""UI 모델(§4) → HA 자동화 config(신문법) 변환·검증. 순수 함수만, aiohttp 의존 금지."""
from __future__ import annotations

import re

import yaml

# GET api/bootstrap 응답의 "services". 각 도메인 대표 서비스만.
KNOWN_SERVICES: dict[str, list[str]] = {
    "light": ["turn_on", "turn_off", "toggle"],
    "switch": ["turn_on", "turn_off", "toggle"],
    "fan": ["turn_on", "turn_off", "toggle", "set_percentage", "oscillate"],
    "cover": ["open_cover", "close_cover", "stop_cover", "set_cover_position", "toggle"],
    "climate": ["set_temperature", "set_hvac_mode", "turn_on", "turn_off"],
    "media_player": ["turn_on", "turn_off", "media_play", "media_pause", "media_stop",
                     "volume_set", "volume_mute"],
    "lock": ["lock", "unlock"],
    "valve": ["open_valve", "close_valve", "set_valve_position"],
    "scene": ["turn_on"],
    "script": ["turn_on", "turn_off"],
    "vacuum": ["start", "pause", "stop", "return_to_base"],
    "humidifier": ["turn_on", "turn_off", "set_humidity"],
    "button": ["press"],
    "input_boolean": ["turn_on", "turn_off", "toggle"],
    # S6(§2.6): 도메인 무관 서비스. 혼합/불명 도메인 toggle(§3.2) 및 turn_on/off 를
    # 여기서 인지한다. rule_model._ALLOWED_ACTION_DOMAINS = set(KNOWN_SERVICES) | {...}
    # 이므로 이 한 줄로 화이트리스트에도 homeassistant 가 편입된다.
    "homeassistant": ["turn_on", "turn_off", "toggle"],
}

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
# sun 오프셋: ± HH:MM(:SS) 문자열 (예: "-00:45:00", "+1:30")
_OFFSET_RE = re.compile(r"^[+-]?\d{1,2}:\d{2}(:\d{2})?$")
# time_pattern 주기: "5" 또는 "/5" 형태의 정수 표기
_TIME_PATTERN_RE = re.compile(r"^/?\d+$")

# 특정 서비스는 data에 반드시 필요한 키가 있다: action → (필수 키, 오류 메시지)
_REQUIRED_SERVICE_DATA: dict[str, tuple[str, str]] = {
    "climate.set_temperature": ("temperature", "설정 온도를 입력해 주세요."),
    "media_player.volume_set": ("volume_level", "볼륨(0~1)을 입력해 주세요."),
    "fan.set_percentage": ("percentage", "팬 세기(%)를 입력해 주세요."),
    "cover.set_cover_position": ("position", "커버 위치(%)를 입력해 주세요."),
    "humidifier.set_humidity": ("humidity", "설정 습도(%)를 입력해 주세요."),
    # S6(§3.3): 알림은 반드시 메시지가 있어야 한다(v1 검증에도 안전 강화).
    "notify.notify": ("message", "알림 메시지를 입력해 주세요."),
}


class ValidationError(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"모델 검증 실패: {len(errors)}개 오류")


# ---------------------------------------------------------------------------
# Duration 헬퍼
# ---------------------------------------------------------------------------
def _dur(d) -> str | None:
    """Duration 객체 → "HH:MM:SS". 모두 0이면 None(필드 생략)."""
    if not isinstance(d, dict):
        return None
    h = int(d.get("hours", 0) or 0)
    m = int(d.get("minutes", 0) or 0)
    s = int(d.get("seconds", 0) or 0)
    if h == 0 and m == 0 and s == 0:
        return None
    return f"{h:02d}:{m:02d}:{s:02d}"


def _has_duration(d) -> bool:
    if not isinstance(d, dict):
        return False
    return any(int(d.get(k, 0) or 0) != 0 for k in ("hours", "minutes", "seconds"))


# ---------------------------------------------------------------------------
# build: 트리거
# ---------------------------------------------------------------------------
def _build_trigger(t: dict) -> dict:
    typ = t["type"]
    if typ == "state":
        out = {"trigger": "state", "entity_id": t["entity_id"]}
        if t.get("from") not in (None, ""):
            out["from"] = t["from"]
        if t.get("to") not in (None, ""):
            out["to"] = t["to"]
        dur = _dur(t.get("for"))
        if dur:
            out["for"] = dur
        return out
    if typ == "numeric_state":
        out = {"trigger": "numeric_state", "entity_id": t["entity_id"]}
        if t.get("above") is not None:
            out["above"] = t["above"]
        if t.get("below") is not None:
            out["below"] = t["below"]
        dur = _dur(t.get("for"))
        if dur:
            out["for"] = dur
        return out
    if typ == "time":
        return {"trigger": "time", "at": t["at"]}
    if typ == "time_pattern":
        out = {"trigger": "time_pattern"}
        for k in ("hours", "minutes", "seconds"):
            if t.get(k) not in (None, ""):
                out[k] = t[k]
        return out
    if typ == "sun":
        out = {"trigger": "sun", "event": t["event"]}
        if t.get("offset") not in (None, ""):
            out["offset"] = t["offset"]
        return out
    if typ == "zone":
        return {"trigger": "zone", "entity_id": t["entity_id"],
                "zone": t["zone"], "event": t["event"]}
    if typ == "template":
        out = {"trigger": "template", "value_template": t["value_template"]}
        dur = _dur(t.get("for"))
        if dur:
            out["for"] = dur
        return out
    if typ == "homeassistant":
        return {"trigger": "homeassistant", "event": t["event"]}
    raise ValidationError([{"path": "triggers", "message": "지원하지 않는 트리거 유형입니다."}])


# ---------------------------------------------------------------------------
# build: 조건
# ---------------------------------------------------------------------------
def _build_condition(c: dict) -> dict:
    typ = c["type"]
    if typ == "state":
        out = {"condition": "state", "entity_id": c["entity_id"], "state": c["state"]}
        dur = _dur(c.get("for"))
        if dur:
            out["for"] = dur
        return out
    if typ == "numeric_state":
        out = {"condition": "numeric_state", "entity_id": c["entity_id"]}
        if c.get("above") is not None:
            out["above"] = c["above"]
        if c.get("below") is not None:
            out["below"] = c["below"]
        return out
    if typ == "time":
        out = {"condition": "time"}
        if c.get("after") not in (None, ""):
            out["after"] = c["after"]
        if c.get("before") not in (None, ""):
            out["before"] = c["before"]
        if c.get("weekday"):
            out["weekday"] = list(c["weekday"])
        return out
    if typ == "sun":
        out = {"condition": "sun"}
        if c.get("after") not in (None, ""):
            out["after"] = c["after"]
        if c.get("before") not in (None, ""):
            out["before"] = c["before"]
        if c.get("after_offset") not in (None, ""):
            out["after_offset"] = c["after_offset"]
        if c.get("before_offset") not in (None, ""):
            out["before_offset"] = c["before_offset"]
        return out
    if typ == "zone":
        return {"condition": "zone", "entity_id": c["entity_id"], "zone": c["zone"]}
    if typ == "template":
        return {"condition": "template", "value_template": c["value_template"]}
    if typ == "trigger":
        return {"condition": "trigger", "id": c["id"]}
    if typ in ("and", "or", "not"):
        return {"condition": typ,
                "conditions": [_build_condition(x) for x in c["conditions"]]}
    raise ValidationError([{"path": "conditions", "message": "지원하지 않는 조건 유형입니다."}])


# ---------------------------------------------------------------------------
# build: 액션
# ---------------------------------------------------------------------------
def _build_action(a: dict) -> dict:
    typ = a["type"]
    if typ == "service":
        out = {"action": a["action"]}
        target = {k: v for k, v in (a.get("target") or {}).items() if v}
        if target:
            out["target"] = target
        data = a.get("data") or {}
        if data:
            out["data"] = data
        return out
    if typ == "delay":
        return {"delay": _dur(a["duration"])}
    if typ == "wait_template":
        out = {"wait_template": a["wait_template"]}
        to = _dur(a.get("timeout"))
        if to:
            out["timeout"] = to
        if "continue_on_timeout" in a:
            out["continue_on_timeout"] = bool(a["continue_on_timeout"])
        return out
    if typ == "wait_for_trigger":
        out = {"wait_for_trigger": [_build_trigger(t) for t in a["triggers"]]}
        to = _dur(a.get("timeout"))
        if to:
            out["timeout"] = to
        if "continue_on_timeout" in a:
            out["continue_on_timeout"] = bool(a["continue_on_timeout"])
        return out
    if typ == "condition":
        return _build_condition(a["condition"])
    if typ == "choose":
        out = {"choose": [
            {"conditions": [_build_condition(c) for c in (o.get("conditions") or [])],
             "sequence": [_build_action(x) for x in o["sequence"]]}
            for o in a["options"]]}
        if a.get("default"):
            out["default"] = [_build_action(x) for x in a["default"]]
        return out
    if typ == "if":
        out = {"if": [_build_condition(c) for c in a["if"]],
               "then": [_build_action(x) for x in a["then"]]}
        if a.get("else"):
            out["else"] = [_build_action(x) for x in a["else"]]
        return out
    if typ == "repeat":
        kind = a["kind"]
        rep: dict = {}
        if kind == "count":
            rep["count"] = a["count"]
        elif kind == "while":
            rep["while"] = [_build_condition(c) for c in (a.get("conditions") or [])]
        elif kind == "until":
            rep["until"] = [_build_condition(c) for c in (a.get("conditions") or [])]
        rep["sequence"] = [_build_action(x) for x in a["sequence"]]
        return {"repeat": rep}
    if typ == "parallel":
        return {"parallel": [{"sequence": [_build_action(x) for x in br]}
                             for br in a["branches"]]}
    if typ == "stop":
        return {"stop": a["message"]}
    raise ValidationError([{"path": "actions", "message": "지원하지 않는 동작 유형입니다."}])


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def validate_model(model: dict) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(model, dict):
        return [{"path": "", "message": "자동화 모델 형식이 올바르지 않습니다."}]

    if not str(model.get("alias") or "").strip():
        errors.append({"path": "alias", "message": "이름을 입력해 주세요."})

    mode = model.get("mode", "single")
    if mode not in ("single", "restart", "queued", "parallel"):
        errors.append({"path": "mode", "message": "실행 모드가 올바르지 않습니다."})

    if model.get("condition_mode", "and") not in ("and", "or"):
        errors.append({"path": "condition_mode", "message": "조건 결합 방식이 올바르지 않습니다."})

    triggers = model.get("triggers")
    n_triggers = len(triggers) if isinstance(triggers, list) else 0
    if not isinstance(triggers, list) or not triggers:
        errors.append({"path": "triggers", "message": "트리거를 하나 이상 추가해 주세요."})
    else:
        for i, t in enumerate(triggers):
            _validate_trigger(t, f"triggers[{i}]", errors)

    conditions = model.get("conditions", [])
    if isinstance(conditions, list):
        for i, c in enumerate(conditions):
            _validate_condition(c, f"conditions[{i}]", errors, n_triggers)

    actions = model.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append({"path": "actions", "message": "실행할 동작을 하나 이상 추가해 주세요."})
    else:
        for i, a in enumerate(actions):
            _validate_action(a, f"actions[{i}]", errors, n_triggers)

    return errors


def _validate_trigger(t, path, errors):
    if not isinstance(t, dict):
        errors.append({"path": path, "message": "트리거 형식이 올바르지 않습니다."})
        return
    typ = t.get("type")
    if typ == "state":
        if not t.get("entity_id"):
            errors.append({"path": path + ".entity_id", "message": "엔티티를 선택해 주세요."})
    elif typ == "numeric_state":
        if not t.get("entity_id"):
            errors.append({"path": path + ".entity_id", "message": "엔티티를 선택해 주세요."})
        if t.get("above") is None and t.get("below") is None:
            errors.append({"path": path, "message": "이상 또는 이하 값 중 하나 이상을 입력해 주세요."})
    elif typ == "time":
        if not t.get("at") or not _TIME_RE.match(str(t.get("at"))):
            errors.append({"path": path + ".at", "message": "시각을 HH:MM 형식으로 입력해 주세요."})
    elif typ == "time_pattern":
        present = [k for k in ("hours", "minutes", "seconds") if t.get(k) not in (None, "")]
        if not present:
            errors.append({"path": path, "message": "시/분/초 주기 중 하나 이상을 입력해 주세요."})
        for k in present:
            if not _TIME_PATTERN_RE.match(str(t.get(k))):
                errors.append({"path": path + "." + k,
                               "message": "주기는 숫자 또는 '/숫자' 형식으로 입력해 주세요."})
    elif typ == "sun":
        if t.get("event") not in ("sunrise", "sunset"):
            errors.append({"path": path + ".event", "message": "일출 또는 일몰을 선택해 주세요."})
        off = t.get("offset")
        if off not in (None, "") and not _OFFSET_RE.match(str(off)):
            errors.append({"path": path + ".offset",
                           "message": "오프셋은 ±HH:MM(:SS) 형식으로 입력해 주세요."})
    elif typ == "zone":
        if not t.get("entity_id"):
            errors.append({"path": path + ".entity_id", "message": "사람 엔티티를 선택해 주세요."})
        if not t.get("zone"):
            errors.append({"path": path + ".zone", "message": "구역(zone)을 선택해 주세요."})
        if t.get("event") not in ("enter", "leave"):
            errors.append({"path": path + ".event", "message": "도착 또는 벗어남을 선택해 주세요."})
    elif typ == "template":
        if not str(t.get("value_template") or "").strip():
            errors.append({"path": path + ".value_template", "message": "템플릿을 입력해 주세요."})
    elif typ == "homeassistant":
        if t.get("event") not in ("start", "shutdown"):
            errors.append({"path": path + ".event", "message": "시작 또는 종료를 선택해 주세요."})
    else:
        errors.append({"path": path + ".type", "message": "지원하지 않는 트리거 유형입니다."})


def _validate_condition(c, path, errors, n_triggers=0):
    if not isinstance(c, dict):
        errors.append({"path": path, "message": "조건 형식이 올바르지 않습니다."})
        return
    typ = c.get("type")
    if typ == "state":
        if not c.get("entity_id"):
            errors.append({"path": path + ".entity_id", "message": "엔티티를 선택해 주세요."})
        if c.get("state") in (None, ""):
            errors.append({"path": path + ".state", "message": "상태 값을 입력해 주세요."})
    elif typ == "numeric_state":
        if not c.get("entity_id"):
            errors.append({"path": path + ".entity_id", "message": "엔티티를 선택해 주세요."})
        if c.get("above") is None and c.get("below") is None:
            errors.append({"path": path, "message": "이상 또는 이하 값 중 하나 이상을 입력해 주세요."})
    elif typ == "time":
        if not c.get("after") and not c.get("before") and not c.get("weekday"):
            errors.append({"path": path,
                           "message": "시작/종료 시각 또는 요일 중 하나 이상을 지정해 주세요."})
        for k in ("after", "before"):
            v = c.get(k)
            if v and not _TIME_RE.match(str(v)):
                errors.append({"path": path + "." + k, "message": "시각을 HH:MM:SS 형식으로 입력해 주세요."})
        wd = c.get("weekday")
        if wd and any(x not in _WEEKDAYS for x in wd):
            errors.append({"path": path + ".weekday", "message": "요일 값이 올바르지 않습니다."})
    elif typ == "sun":
        if not c.get("after") and not c.get("before"):
            errors.append({"path": path,
                           "message": "일출/일몰 기준 시점을 하나 이상 선택해 주세요."})
        for k in ("after", "before"):
            v = c.get(k)
            if v and v not in ("sunrise", "sunset"):
                errors.append({"path": path + "." + k, "message": "일출 또는 일몰이어야 합니다."})
        for k in ("after_offset", "before_offset"):
            v = c.get(k)
            if v not in (None, "") and not _OFFSET_RE.match(str(v)):
                errors.append({"path": path + "." + k,
                               "message": "오프셋은 ±HH:MM(:SS) 형식으로 입력해 주세요."})
    elif typ == "zone":
        if not c.get("entity_id"):
            errors.append({"path": path + ".entity_id", "message": "엔티티를 선택해 주세요."})
        if not c.get("zone"):
            errors.append({"path": path + ".zone", "message": "구역(zone)을 선택해 주세요."})
    elif typ == "template":
        if not str(c.get("value_template") or "").strip():
            errors.append({"path": path + ".value_template", "message": "템플릿을 입력해 주세요."})
    elif typ == "trigger":
        tid = c.get("id")
        if tid in (None, ""):
            errors.append({"path": path + ".id", "message": "트리거 번호를 지정해 주세요."})
        elif not (isinstance(tid, str) and tid.isdigit() and 0 <= int(tid) < n_triggers):
            errors.append({"path": path + ".id", "message": "존재하는 트리거 번호를 지정해 주세요."})
    elif typ in ("and", "or", "not"):
        subs = c.get("conditions")
        if not isinstance(subs, list) or not subs:
            errors.append({"path": path + ".conditions", "message": "하위 조건을 하나 이상 추가해 주세요."})
        else:
            for i, s in enumerate(subs):
                _validate_condition(s, f"{path}.conditions[{i}]", errors, n_triggers)
    else:
        errors.append({"path": path + ".type", "message": "지원하지 않는 조건 유형입니다."})


def _validate_action(a, path, errors, n_triggers=0):
    if not isinstance(a, dict):
        errors.append({"path": path, "message": "동작 형식이 올바르지 않습니다."})
        return
    typ = a.get("type")
    if typ == "service":
        action = str(a.get("action") or "")
        if not a.get("action") or "." not in action:
            errors.append({"path": path + ".action", "message": "실행할 서비스를 선택해 주세요."})
        else:
            req = _REQUIRED_SERVICE_DATA.get(action)
            if req is not None:
                key, msg = req
                data = a.get("data")
                if not isinstance(data, dict) or data.get(key) is None:
                    errors.append({"path": path + ".data." + key, "message": msg})
    elif typ == "delay":
        if not _has_duration(a.get("duration")):
            errors.append({"path": path + ".duration", "message": "지연 시간을 입력해 주세요."})
    elif typ == "wait_template":
        if not str(a.get("wait_template") or "").strip():
            errors.append({"path": path + ".wait_template", "message": "대기 템플릿을 입력해 주세요."})
    elif typ == "wait_for_trigger":
        trs = a.get("triggers")
        if not isinstance(trs, list) or not trs:
            errors.append({"path": path + ".triggers", "message": "대기할 트리거를 하나 이상 추가해 주세요."})
        else:
            for i, t in enumerate(trs):
                _validate_trigger(t, f"{path}.triggers[{i}]", errors)
    elif typ == "condition":
        cond = a.get("condition")
        if not isinstance(cond, dict):
            errors.append({"path": path + ".condition", "message": "조건을 설정해 주세요."})
        else:
            _validate_condition(cond, path + ".condition", errors, n_triggers)
    elif typ == "choose":
        opts = a.get("options")
        if not isinstance(opts, list) or not opts:
            errors.append({"path": path + ".options", "message": "선택 분기를 하나 이상 추가해 주세요."})
        else:
            for i, o in enumerate(opts):
                op = f"{path}.options[{i}]"
                for j, cc in enumerate(o.get("conditions") or []):
                    _validate_condition(cc, f"{op}.conditions[{j}]", errors, n_triggers)
                seq = o.get("sequence")
                if not isinstance(seq, list) or not seq:
                    errors.append({"path": op + ".sequence", "message": "실행할 동작을 추가해 주세요."})
                else:
                    for j, ac in enumerate(seq):
                        _validate_action(ac, f"{op}.sequence[{j}]", errors, n_triggers)
        for i, ac in enumerate(a.get("default") or []):
            _validate_action(ac, f"{path}.default[{i}]", errors, n_triggers)
    elif typ == "if":
        ifs = a.get("if")
        if not isinstance(ifs, list) or not ifs:
            errors.append({"path": path + ".if", "message": "조건을 하나 이상 추가해 주세요."})
        else:
            for i, cc in enumerate(ifs):
                _validate_condition(cc, f"{path}.if[{i}]", errors, n_triggers)
        then = a.get("then")
        if not isinstance(then, list) or not then:
            errors.append({"path": path + ".then", "message": "조건이 참일 때 실행할 동작을 추가해 주세요."})
        else:
            for i, ac in enumerate(then):
                _validate_action(ac, f"{path}.then[{i}]", errors, n_triggers)
        for i, ac in enumerate(a.get("else") or []):
            _validate_action(ac, f"{path}.else[{i}]", errors, n_triggers)
    elif typ == "repeat":
        kind = a.get("kind")
        if kind not in ("count", "while", "until"):
            errors.append({"path": path + ".kind", "message": "반복 방식을 선택해 주세요."})
        elif kind == "count":
            cnt = a.get("count")
            if not isinstance(cnt, int) or isinstance(cnt, bool) or cnt < 1:
                errors.append({"path": path + ".count", "message": "반복 횟수는 1 이상의 정수여야 합니다."})
        else:
            subs = a.get("conditions")
            if not isinstance(subs, list) or not subs:
                errors.append({"path": path + ".conditions", "message": "반복 조건을 하나 이상 추가해 주세요."})
            else:
                for i, cc in enumerate(subs):
                    _validate_condition(cc, f"{path}.conditions[{i}]", errors, n_triggers)
        seq = a.get("sequence")
        if not isinstance(seq, list) or not seq:
            errors.append({"path": path + ".sequence", "message": "반복할 동작을 추가해 주세요."})
        else:
            for i, ac in enumerate(seq):
                _validate_action(ac, f"{path}.sequence[{i}]", errors, n_triggers)
    elif typ == "parallel":
        branches = a.get("branches")
        if not isinstance(branches, list) or not branches:
            errors.append({"path": path + ".branches", "message": "병렬 분기를 하나 이상 추가해 주세요."})
        else:
            for i, br in enumerate(branches):
                if not isinstance(br, list) or not br:
                    errors.append({"path": f"{path}.branches[{i}]", "message": "분기에 동작을 추가해 주세요."})
                else:
                    for j, ac in enumerate(br):
                        _validate_action(ac, f"{path}.branches[{i}][{j}]", errors, n_triggers)
    elif typ == "stop":
        if not str(a.get("message") or "").strip():
            errors.append({"path": path + ".message", "message": "중지 사유(메시지)를 입력해 주세요."})
    else:
        errors.append({"path": path + ".type", "message": "지원하지 않는 동작 유형입니다."})


# ---------------------------------------------------------------------------
# build_automation / to_yaml
# ---------------------------------------------------------------------------
def build_automation(model: dict) -> dict:
    errors = validate_model(model)
    if errors:
        raise ValidationError(errors)

    config: dict = {"alias": model["alias"].strip(),
                    "description": model.get("description") or "",
                    "mode": model.get("mode", "single")}
    if config["mode"] in ("queued", "parallel"):
        config["max"] = int(model.get("max", 10))

    config["triggers"] = [_build_trigger(t) for t in model["triggers"]]

    conditions = [_build_condition(c) for c in (model.get("conditions") or [])]
    if model.get("condition_mode") == "or" and len(conditions) >= 2:
        conditions = [{"condition": "or", "conditions": conditions}]
    config["conditions"] = conditions

    config["actions"] = [_build_action(a) for a in model["actions"]]
    return config


def to_yaml(config: dict) -> str:
    return yaml.dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# summarize — 한국어 자연어 요약 (검증 실패 모델에도 안전하게 동작)
# ---------------------------------------------------------------------------
_STATE_LABELS = {"on": "켜짐", "off": "꺼짐", "open": "열림", "closed": "닫힘",
                 "home": "집", "not_home": "외출", "locked": "잠김", "unlocked": "열림"}
_SVC_VERB = {
    "turn_on": "켜기", "turn_off": "끄기", "toggle": "전환",
    "open_cover": "열기", "close_cover": "닫기", "stop_cover": "정지",
    "set_cover_position": "위치 설정", "lock": "잠그기", "unlock": "잠금 해제",
    "open_valve": "열기", "close_valve": "닫기", "set_temperature": "온도 설정",
    "set_hvac_mode": "모드 설정", "set_percentage": "세기 설정", "set_humidity": "습도 설정",
    "volume_set": "볼륨 설정", "volume_mute": "음소거", "media_play": "재생",
    "media_pause": "일시정지", "media_stop": "정지", "press": "누르기",
    "start": "시작", "stop": "정지", "pause": "일시정지", "return_to_base": "복귀",
}


def _service_verb(action: str) -> str:
    svc = action.split(".")[-1] if "." in action else action
    return _SVC_VERB.get(svc, f"{svc} 실행")


def _dur_kor(d) -> str:
    if not isinstance(d, dict):
        return "잠시"
    h = int(d.get("hours", 0) or 0)
    m = int(d.get("minutes", 0) or 0)
    s = int(d.get("seconds", 0) or 0)
    parts = []
    if h:
        parts.append(f"{h}시간")
    if m:
        parts.append(f"{m}분")
    if s:
        parts.append(f"{s}초")
    return " ".join(parts) if parts else "잠시"


def _tphrase(t, nm) -> str:
    typ = t.get("type") if isinstance(t, dict) else None
    if typ == "state":
        to = t.get("to")
        if to:
            return f"'{nm(t.get('entity_id', ''))}'이(가) {_STATE_LABELS.get(to, to)} 상태가 되면"
        return f"'{nm(t.get('entity_id', ''))}'의 상태가 바뀌면"
    if typ == "numeric_state":
        n = nm(t.get("entity_id", ""))
        ab, be = t.get("above"), t.get("below")
        if ab is not None and be is not None:
            return f"'{n}' 값이 {ab}~{be} 사이가 되면"
        if ab is not None:
            return f"'{n}' 값이 {ab}을(를) 초과하면"
        if be is not None:
            return f"'{n}' 값이 {be} 미만이 되면"
        return f"'{n}' 값이 변하면"
    if typ == "time":
        return f"{t.get('at', '지정한 시각')}이 되면"
    if typ == "time_pattern":
        return "주기적으로"
    if typ == "sun":
        return "해가 뜨면" if t.get("event") == "sunrise" else "해가 지면"
    if typ == "zone":
        n = nm(t.get("entity_id", ""))
        return f"'{n}'이(가) 구역에 도착하면" if t.get("event") == "enter" else f"'{n}'이(가) 구역을 벗어나면"
    if typ == "template":
        return "지정한 조건식이 참이 되면"
    if typ == "homeassistant":
        return "홈어시스턴트가 시작되면" if t.get("event") == "start" else "홈어시스턴트가 종료되면"
    return "특정 상황이 되면"


def _aphrase(a, nm) -> str:
    typ = a.get("type") if isinstance(a, dict) else None
    if typ == "service":
        ents = (a.get("target") or {}).get("entity_id") or []
        names = "·".join(nm(e) for e in ents) if ents else "대상"
        return f"'{names}' {_service_verb(a.get('action', ''))}"
    if typ == "delay":
        return f"{_dur_kor(a.get('duration'))} 대기"
    if typ == "wait_template":
        return "조건 충족까지 대기"
    if typ == "wait_for_trigger":
        return "특정 트리거 대기"
    if typ == "condition":
        return "조건 확인"
    if typ == "choose":
        return "상황별 분기 실행"
    if typ == "if":
        return "조건부 실행"
    if typ == "repeat":
        return "반복 실행"
    if typ == "parallel":
        return "동시 실행"
    if typ == "stop":
        return "자동화 중지"
    return "동작 실행"


def _cphrase(c, nm) -> str:
    typ = c.get("type") if isinstance(c, dict) else None
    if typ == "state":
        st = c.get("state")
        return f"'{nm(c.get('entity_id', ''))}'이(가) {_STATE_LABELS.get(st, st)}"
    if typ == "numeric_state":
        n = nm(c.get("entity_id", ""))
        ab, be = c.get("above"), c.get("below")
        if ab is not None and be is not None:
            return f"'{n}' 값이 {ab}~{be}"
        if ab is not None:
            return f"'{n}' 값이 {ab} 초과"
        if be is not None:
            return f"'{n}' 값이 {be} 미만"
        return f"'{n}' 값 조건"
    if typ == "time":
        return "지정한 시간대"
    if typ == "sun":
        return "해와 관련된 시간대"
    if typ == "zone":
        return f"'{nm(c.get('entity_id', ''))}'이(가) 특정 구역에 있음"
    if typ == "template":
        return "지정한 조건식"
    if typ == "trigger":
        return "특정 트리거로 실행됨"
    if typ in ("and", "or", "not"):
        return "복합 조건"
    return "조건"


def summarize(model: dict, entity_names: dict[str, str]) -> str:
    names = entity_names or {}
    nm = lambda e: names.get(e, e)  # noqa: E731

    trigs = [_tphrase(t, nm) for t in (model.get("triggers") or [])]
    if not trigs:
        trig_txt = "특정 상황이 되면"
    elif len(trigs) == 1:
        trig_txt = trigs[0]
    else:
        trig_txt = " 또는 ".join(trigs) + " 중 하나라도 발생하면"

    acts = [_aphrase(a, nm) for a in (model.get("actions") or [])]
    act_txt = ", ".join(acts) if acts else "동작을 실행"

    sentences = [f"{trig_txt} {act_txt}을(를) 실행합니다."]

    conds = model.get("conditions") or []
    if conds:
        joiner = " 그리고 " if model.get("condition_mode", "and") == "and" else " 또는 "
        word = "모두 만족" if model.get("condition_mode", "and") == "and" else "하나라도 만족"
        cps = joiner.join(_cphrase(c, nm) for c in conds)
        sentences.append(f"단, {cps} 조건을 {word}할 때만 실행됩니다.")

    return " ".join(sentences)
