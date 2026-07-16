"""§3 수동 단어 매핑 — 문장 토큰화 + 역할 초안 + 토큰→RuleModel 빌더.

파서가 이해 못 한 문장을 사용자가 단어별로 역할/대상에 직접 지정할 때 쓴다.
의존성 0 (표준 라이브러리 + gazetteer/normalize). §3.1/§3.2 계약을 정확히 따른다.
"""
from __future__ import annotations

import re
from typing import Optional

from .gazetteer import (DAY_TYPE_WORDS, DEVICE_CONCEPTS, MOTION_WORDS,
                        SEASON_WORDS, SEGMENT_WORDS, Gazetteer)
from .normalize import (find_clock, find_duration, find_percent,
                        find_temperature, strip_particles_simple,
                        to_duration_obj)

# ---------------------------------------------------------------------------
# §3.1 토큰화
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"\S+")


def tokenize(sentence: str) -> list[dict]:
    """공백 분절 + 조사 분리(core). start/end 는 원문 문자 오프셋.

    [{"index":0,"text":"거실조명은","core":"거실조명","start":0,"end":5}]
    """
    tokens = []
    for i, m in enumerate(_TOKEN_RE.finditer(sentence or "")):
        text = m.group(0)
        core = strip_particles_simple(text) or text
        tokens.append({"index": i, "text": text, "core": core,
                       "start": m.start(), "end": m.end()})
    return tokens


# ---------------------------------------------------------------------------
# §3.1 역할 초안(파서 힌트) — gazetteer 재사용
# ---------------------------------------------------------------------------
_ON_WORDS = ("작동", "감지", "켜지", "열리", "열림", "있", "도착", "왔", "울리", "on")
_OFF_WORDS = ("꺼지", "닫히", "닫힘", "없", "해제", "off")
_ACTION_ON = ("켜", "틀", "열어", "가동", "실행")
_ACTION_OFF = ("꺼", "끄", "멈춰", "정지", "닫아")


def suggest_roles(tokens: list[dict], gazetteer: Gazetteer,
                  settings: dict) -> list[dict]:
    """각 토큰에 후보 역할·대상을 채운 초안. 사용자가 이후 수정한다."""
    out = []
    for tok in tokens:
        text = tok["text"]
        core = tok["core"]
        sug = {"index": tok["index"], "role": "ignore"}

        # 모드(표면형이 토큰에 포함되면)
        mode_hit = next((s for s in gazetteer.mode_surfaces if s and s in text), None)
        if mode_hit:
            name = gazetteer.mode_canonical.get(mode_hit, mode_hit)
            st = "off" if re.search(r"꺼지|아니|해제", text) else "on"
            sug.update(role="mode_ref", ref=name, state=st)
        # 시간대
        elif core in SEGMENT_WORDS:
            sug.update(role="segment", ref=SEGMENT_WORDS[core])
        # 요일 구분 / 계절
        elif core in DAY_TYPE_WORDS:
            sug.update(role="daytype", ref=DAY_TYPE_WORDS[core])
        elif core in SEASON_WORDS:
            sug.update(role="season", ref=SEASON_WORDS[core])
        # 지속시간
        elif find_duration(text):
            d = find_duration(text)
            sug.update(role="duration", value=d["seconds"])
        # 퍼센트 → 값(밝기)
        elif find_percent(text):
            sug.update(role="value", value=find_percent(text)["value"], kind="brightness")
        # 온도 → 값(온도)
        elif find_temperature(text):
            sug.update(role="value", value=find_temperature(text)["value"],
                       kind="temperature")
        # 액션 동사
        elif any(w in text for w in _ACTION_OFF):
            sug.update(role="action_verb", verb="off")
        elif any(w in text for w in _ACTION_ON):
            sug.update(role="action_verb", verb="on")
        # 상태값(엔티티 뒤 이벤트 서술)
        elif any(w in text for w in _OFF_WORDS):
            sug.update(role="event_state", state="off")
        elif any(w in text for w in _ON_WORDS):
            sug.update(role="event_state", state="on")
        else:
            # 엔티티 이름 매칭 → 트리거 대상 초안
            cands = gazetteer.resolve_name(core)
            if cands:
                sug.update(role="trigger_entity", ref=cands[0]["id"])
            elif core in DEVICE_CONCEPTS or core in MOTION_WORDS:
                # 기기어 단독은 방과 합쳐 해석해야 하므로 초안은 무시로 둔다.
                sug["role"] = "ignore"
        out.append(sug)
    return out


# ---------------------------------------------------------------------------
# §3.2 토큰 매핑 → RuleModel 빌더
# ---------------------------------------------------------------------------
_STATE_ON = {"on", "detected", "open", "opened"}
_STATE_OFF = {"off", "clear", "close", "closed"}


def _norm_state(st: Optional[str]) -> str:
    if st in _STATE_OFF:
        return "off"
    return "on"


def _domain_of(gz: Gazetteer, entity_id: Optional[str]) -> Optional[str]:
    if not entity_id:
        return None
    e = gz.entity(entity_id)
    if e:
        return e.get("domain")
    return entity_id.split(".", 1)[0] if "." in entity_id else None


def _make_service(gz: Gazetteer, target: str, verb: str,
                  value: Optional[dict]) -> Optional[dict]:
    """action_target + action_verb(+value) → service 노드."""
    if not target:
        return None
    domain = _domain_of(gz, target) or "homeassistant"
    off = verb == "off"
    data = {}
    if domain == "light":
        action = "light.turn_off" if off else "light.turn_on"
        if value and value.get("kind") == "brightness" and not off:
            data["brightness_pct"] = value.get("value")
    elif domain == "fan":
        action = "fan.turn_off" if off else "fan.turn_on"
        if value and value.get("kind") == "brightness" and not off:
            data["percentage"] = value.get("value")
    elif domain == "climate":
        action = "climate.turn_off" if off else "climate.turn_on"
        if value and value.get("kind") == "temperature" and not off:
            data["temperature"] = value.get("value")
    elif domain == "cover":
        action = "cover.close_cover" if off else "cover.open_cover"
    elif domain == "lock":
        action = "lock.lock" if off else "lock.unlock"
    elif domain in ("switch", "media_player"):
        action = f"{domain}.turn_{'off' if off else 'on'}"
    else:
        action = f"{domain}.turn_{'off' if off else 'on'}"
    node = {"type": "service", "action": action, "target": {"entity_id": [target]}}
    if data:
        node["data"] = node.get("data", {})
        node["data"].update(data)
    return node


class _Builder:
    def __init__(self, gz: Gazetteer):
        self.gz = gz
        self.subrules: list[dict] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.dropped = 0             # 상태 미지정으로 버려진 엔티티 토큰 수(ok 반영)
        # 서브룰 로컬 상태
        self._reset_subrule()
        # 서브룰 간 상속(§2.1)
        self.inh_target: Optional[str] = None

    def _reset_subrule(self):
        self.triggers: list[dict] = []
        self.conditions: list[dict] = []
        self.actions: list[dict] = []
        self.zone = "pre"            # pre-action(트리거/조건존) → post(액션존)
        self.last_entity: Optional[dict] = None  # {"ref","intent"}
        self.pending_duration: Optional[int] = None
        self.pending_target: Optional[str] = None
        self.pending_value: Optional[dict] = None
        self.has_trigger = False

    def _warn_unconsumed(self):
        """상태/수치를 지정받지 못해 소비되지 않은 엔티티 토큰을 경고하고 버린다(§3.2).

        trigger_entity/condition_entity 뒤에 event_state/numeric 가 오지 않으면 last_entity 가
        노드 없이 사라진다. 조용히 버리지 않고 사용자에게 알린다(경고 + ok 반영)."""
        ent = self.last_entity
        ref = ent and ent.get("ref")
        if ref:
            e = self.gz.entity(ref)
            name = (e or {}).get("name") or ref
            self.warnings.append(f"'{name}'에 상태를 지정하지 않아 무시했어요.")
            self.dropped += 1
        self.last_entity = None

    def _flush_subrule(self):
        # 서브룰 마감 시 상태 미지정으로 남은 엔티티가 있으면 경고 후 버린다(§3.2).
        self._warn_unconsumed()
        self.subrules.append({"triggers": self.triggers,
                              "condition_mode": "and",
                              "conditions": self.conditions,
                              "actions": self.actions})
        self._reset_subrule()

    def _apply_event_state(self, state: str):
        ref = self.last_entity and self.last_entity.get("ref")
        if not ref:
            self.errors.append("상태값을 붙일 대상(엔티티)이 없어요.")
            return
        intent = self.last_entity.get("intent", "trigger")
        st = _norm_state(state)
        if self.zone == "pre" and intent != "condition" and not self.has_trigger:
            # 트리거존 첫 event_state 엔티티 = 트리거
            if self.pending_duration:
                self.triggers.append({"type": "state_held", "entity_id": ref,
                                      "to": st,
                                      "for": to_duration_obj(self.pending_duration)})
                self.pending_duration = None
            else:
                self.triggers.append({"type": "state", "entity_id": ref, "to": st})
            self.has_trigger = True
        else:
            # 나머지 = 조건
            if self.pending_duration:
                self.conditions.append({"type": "held", "entity_id": ref, "state": st,
                                        "for": to_duration_obj(self.pending_duration)})
                self.pending_duration = None
            else:
                self.conditions.append({"type": "state", "entity_id": ref, "state": st})
        self.last_entity = None

    def _apply_numeric(self, a: dict):
        ref = self.last_entity and self.last_entity.get("ref")
        if not ref:
            self.errors.append("수치 조건을 붙일 대상(센서)이 없어요.")
            return
        node = {"type": "numeric_state", "entity_id": ref}
        val = a.get("value")
        if a.get("cmp") == "below":
            node["below"] = val
        else:
            node["above"] = val
        if self.zone == "pre" and not self.has_trigger \
                and self.last_entity.get("intent") != "condition":
            self.triggers.append(node)
            self.has_trigger = True
        else:
            self.conditions.append(node)
        self.last_entity = None

    def _apply_action_verb(self, a: dict):
        self.zone = "post"
        verb = a.get("verb", "on")
        if verb == "set_mode":
            self.actions.append({"type": "set_mode", "mode": a.get("ref"),
                                 "to": a.get("state", "on")})
            self.pending_value = None
            return
        target = self.pending_target or self.inh_target
        if not target:
            self.errors.append("동작을 적용할 대상을 지정하지 않았어요.")
            self.pending_value = None
            return
        node = _make_service(self.gz, target, verb, self.pending_value)
        if node:
            self.actions.append(node)
            self.inh_target = target
        self.pending_target = None
        self.pending_value = None

    def feed(self, a: dict):
        role = a.get("role", "ignore")
        if role == "boundary":
            self._flush_subrule()
            return
        if role == "ignore":
            pass
        elif role in ("trigger_entity", "condition_entity"):
            # 앞 엔티티가 상태를 못 받고 남아 있으면(연속 엔티티 토큰) 경고 후 버린다.
            self._warn_unconsumed()
            ref = a.get("ref")
            if not ref:
                self.errors.append("대상 엔티티가 지정되지 않은 토큰이 있어요.")
            self.last_entity = {"ref": ref,
                                "intent": "condition" if role == "condition_entity"
                                else "trigger"}
        elif role == "action_target":
            self.pending_target = a.get("ref")
            if a.get("ref"):
                self.inh_target = a.get("ref")
        elif role == "event_state":
            self._apply_event_state(a.get("state", "on"))
        elif role == "numeric":
            self._apply_numeric(a)
        elif role == "duration":
            if self.zone == "pre":
                self.pending_duration = a.get("value")
            else:
                self.actions.append({"type": "delay",
                                     "duration": to_duration_obj(a.get("value") or 0)})
        elif role == "segment":
            self.conditions.append({"type": "time_segment",
                                    "segments": [a.get("ref")]})
        elif role == "mode_ref":
            self.conditions.append({"type": "mode", "mode": a.get("ref"),
                                    "state": a.get("state", "on")})
        elif role == "daytype":
            self.conditions.append({"type": "day_type", "types": [a.get("ref")]})
        elif role == "season":
            self.conditions.append({"type": "season", "seasons": [a.get("ref")]})
        elif role == "value":
            self.pending_value = {"kind": a.get("kind", "brightness"),
                                  "value": a.get("value")}
        elif role == "action_verb":
            self._apply_action_verb(a)
        # 토큰에 boundary 플래그가 붙어 있으면 처리 후 서브룰 분리(§3.2)
        if a.get("boundary"):
            self._flush_subrule()


def build_model_from_tokens(assignments: list[dict], inventory: dict,
                            settings: dict) -> dict:
    """토큰 역할 매핑 → {ok, model, summary, errors, warnings} (§3.1/§3.2).

    assignments = [{"index","role","ref","value","state","cmp","kind","verb","boundary"}]
    """
    gz = Gazetteer.build(inventory or {}, settings or {})
    builder = _Builder(gz)
    for a in sorted(assignments or [], key=lambda x: x.get("index", 0)):
        builder.feed(a)
    # 플러시되지 않는 마지막 서브룰의 미소비 엔티티도 경고 대상(다중 서브룰 말미 등).
    builder._warn_unconsumed()
    # 마지막 서브룰 마감(비어있지 않으면)
    if builder.triggers or builder.conditions or builder.actions \
            or not builder.subrules:
        builder._flush_subrule()

    # 빈 서브룰 제거
    subrules = [sr for sr in builder.subrules
                if sr["triggers"] or sr["conditions"] or sr["actions"]]
    errors = list(builder.errors)
    for i, sr in enumerate(subrules):
        if not sr["triggers"]:
            errors.append(f"{i+1}번째 규칙에 실행 조건(트리거)이 없어요.")
        if not sr["actions"]:
            errors.append(f"{i+1}번째 규칙에 실행할 동작이 없어요.")
    if not subrules:
        errors.append("해석할 토큰이 없어요.")

    # 단일 쌍이면 최상위 필드로 평탄화(하위호환), 다중이면 subrules
    if len(subrules) == 1:
        sr = subrules[0]
        model = {"alias": "", "description": "", "mode": "single",
                 "triggers": sr["triggers"], "condition_mode": "and",
                 "conditions": sr["conditions"], "actions": sr["actions"]}
    else:
        model = {"alias": "", "description": "", "mode": "single",
                 "subrules": subrules}

    # 저장 게이트(validate_rule_model)를 최종 model 에 돌려 "저장 가능"을 실제 저장 성공과
    # 일치시킨다(§3.2·§5). 구조 검사(트리거/액션 존재)만으로는 numeric 값 누락·모드 미선택
    # 같은 저장 불가 상태를 못 걸러 ok=true 로 잘못 나온다. mode_names 는 settings.modes 키.
    from ..engine.rule_model import validate_rule_model
    modes = settings.get("modes") if isinstance(settings, dict) else None
    mode_names = set(modes) if isinstance(modes, dict) else set()
    for e in validate_rule_model(model, inventory, mode_names):
        msg = e.get("message") if isinstance(e, dict) else str(e)
        if msg and msg not in errors:
            errors.append(msg)

    ok = not errors and not builder.dropped
    summary = _summarize(subrules)
    return {"ok": ok, "model": model, "summary": summary,
            "errors": errors, "warnings": builder.warnings}


def _summarize(subrules: list[dict]) -> str:
    if not subrules:
        return "매핑된 규칙이 없어요."
    if len(subrules) == 1:
        sr = subrules[0]
        return (f"트리거 {len(sr['triggers'])}개 · 조건 {len(sr['conditions'])}개 "
                f"· 동작 {len(sr['actions'])}개")
    return " 그리고 ".join(
        f"규칙{i+1}(트리거 {len(sr['triggers'])}·동작 {len(sr['actions'])})"
        for i, sr in enumerate(subrules))
