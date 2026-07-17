"""SPEC-ACCURACY-90 §4 — 모델 후처리(augment) 일급 모듈.

Phase 7 이식(APP-PORT-PLAN §1.3): 오프라인 오버레이의 `parse_patched` 후처리(1196~2558행)를
앱 일급 모듈로 이식한다. `parser.parse()` 가 `_Parser.parse()` 반환 직후 `apply()` 를 호출한다.

apply(result, sentence, normalized, gz, settings, now_fn=None) -> result

S1 범위(APP-PORT-PLAN 게이트): **기존 엔진 노드로 실행 가능한 항목만** 이식한다.
  #19 환기팬→ERV 재매핑 · #24 수치 에지 마무리 · #25 held-for · #26 부정 NOT ·
  #27 light params(색/색온도/상대밝기/transition) · #28 toggle(단 domain.toggle 만 —
  homeassistant.toggle 은 S6) · #29 notify(인용/-다고) · #30 repeat(count/until) ·
  #31 duration revert · 날씨형 전이 numeric_state.
S2 범위: #20 sun 트리거 / sun_window 조건(일몰·일출±오프셋·밤창) 활성화 — 엔진(_schedule_sun)·
  evaluator(sun_window)·검증기(rule_model)가 이 노드를 알므로 auto_disabled 되지 않는다.
S3 범위: #21 weekday / day_of_month / interval_anchor 조건(요일 집합·negate·매달 N일/말일·
  격주) 활성화 — evaluator(순수 평가)·검증기가 이 노드를 안다. interval_anchor.anchor 는
  주입된 now(기본 실제 datetime)가 속한 주의 월요일로 산출(결정성은 now_fn 주입으로만).
S4 범위: #22 time_pattern 트리거(N분/시간/초 마다) 활성화 — 엔진(_schedule_pattern)·검증기가
  이 노드를 알므로 auto_disabled 되지 않는다. repeat 케이던스와는 is_repeat 게이트로 구분한다.
그 밖의 신규 노드(presence_agg)를 방출하는 항목(#23, #32 신규노드 분기)은 S5+ 까지 비활성.

내부 순서(오버레이 _augment_time_calendar 의 기존노드 부분 + calendar/sun + negation/erv):
  _remap_erv_fan → _augment_calendar → 날씨형 numeric → _augment_sun → _augment_numeric_edge →
  _augment_held_for → _augment_actions_only(repeat/notify/revert/toggle/light) → _augment_negation_not
결정적(random 미사용, Date 는 now_fn 주입으로만 통제). result 를 in-place 수정 후 반환한다.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

_QUOTE_SPAN_RE = re.compile(r"['\"“”‘’『』「」][^'\"“”‘’『』「」]*['\"“”‘’『』「」]")

# ---------------------------------------------------------------------------
# #20 sun/sun_window 표면형(APP-PORT-PLAN §1.3, 오버레이 _SUNSET_RE/_SUNRISE_RE 이식)
# ---------------------------------------------------------------------------
# 일몰=sunset('해...지'/어두워지/노을/땅거미), 일출=sunrise. '해 지'는 문두/공백 뒤 독립어일
# 때만(습해지/눅눅해지 등 '-해지-' 합성어의 '해지' 오탐 방지 negative lookbehind).
_SUNSET_RE = re.compile(r"일몰|어두워지|어두워진|캄캄|노을|땅거미|(?<![가-힣])해\s*가?\s*(?:완전히\s*)?지")
_SUNRISE_RE = re.compile(r"일출|동\s*트|동\s*틀|여명|날\s*이?\s*밝|해\s*가?\s*뜨|해\s*뜰|해뜨")
# 밤창(해 진 뒤~해 뜰 때) 명시 표현. 맨 '밤/새벽'(단독 세그먼트)은 제외 — 기존 gold 가
# time_segment 를 쓰는 문장('새벽에 …움직이면')과 충돌하므로 특정 표현만 sun_window 로.
_NIGHTWIN_RE = re.compile(r"밤사이|밤새|한밤|밤중|해\s*진\s*뒤|해\s*지고\s*난|어두운\s*동안|어두울\s*때")


# ---------------------------------------------------------------------------
# 공용 유틸 (순환 import 방지 위해 gz 헬퍼는 여기서 최소 재구현)
# ---------------------------------------------------------------------------
def _subrules(model: dict) -> list:
    if not isinstance(model, dict):
        return []
    subs = model.get("subrules")
    if isinstance(subs, list):
        return [s for s in subs if isinstance(s, dict)]
    return [model]


def _primary_subrule(model: dict):
    """단일 서브룰 뷰(mutable). 다중 서브룰이면 None(대상 축 아님)."""
    if not isinstance(model, dict):
        return None
    subs = model.get("subrules")
    if isinstance(subs, list):
        return subs[0] if len(subs) == 1 else None
    if "triggers" in model:  # 단일 경로: 최상위가 곧 서브룰
        return model
    return None


def _find_area(gz, text: str) -> Optional[str]:
    """gz.room_surfaces 최장일치 방 id(파서 _find_area 와 동형 — 순환 import 회피용 사본)."""
    if gz is None or not text:
        return None
    best, best_len = None, 0
    for surf, aid in getattr(gz, "room_surfaces", {}).items():
        if surf in text and len(surf) > best_len:
            best, best_len = aid, len(surf)
    return best


def _target_ids(node: dict) -> list:
    """service 액션의 target.entity_id 를 리스트로(문자열/리스트/부재 모두 처리)."""
    tgt = node.get("target")
    if not isinstance(tgt, dict):
        return []
    ids = tgt.get("entity_id")
    if isinstance(ids, str):
        return [ids]
    if isinstance(ids, list):
        return [x for x in ids if isinstance(x, str)]
    return []


_UNLOCK_RE = re.compile(r"풀리|풀려|풀린")


def _aspect_state(clause: str, eid: Optional[str]) -> str:
    """결과상 절 + 해석된 엔티티 도메인 → 정확한 상태 문자열(§4.1).
    극성: 풀리(해제)=양성, 꺼/닫/잠/없=음성, 켜/열/있=양성. '안/못' 부정은 극성 반전(XOR).
    도메인별: cover=open/closed, lock=unlocked/locked, 그 외=on/off.
    """
    neg = bool(re.search(r"(?:^|\s)안\s+[가-힣]", clause)) or bool(re.search(r"(?:^|\s)못\s", clause))
    if _UNLOCK_RE.search(clause):
        base_pos = True
    elif re.search(r"꺼|닫|잠|없", clause):
        base_pos = False
    else:
        base_pos = True
    positive = base_pos != neg
    dom = eid.split(".")[0] if eid else None
    if dom == "cover":
        return "open" if positive else "closed"
    if dom == "lock":
        return "unlocked" if positive else "locked"
    return "on" if positive else "off"


def _mark_savable(result: dict, sub: dict) -> None:
    """오버레이가 트리거를 세워 모델이 완결(트리거+액션)되면 result 를 저장가능(ok)으로.
    앱 parse 가 트리거 미검출로 ok=False 로 표시한 문장을 후처리가 살렸을 때, L2 매처가
    이 exact 결과를 덮어써 회귀내는 것을 막는다(§1.3 결정사항)."""
    if result is None or not sub.get("triggers") or not sub.get("actions"):
        return
    result["ok"] = True
    result["unmatched"] = []
    if not result.get("confidence") or result["confidence"] < 0.6:
        result["confidence"] = 0.7


# ===========================================================================
# #19 환기팬 → 전열교환기(ERV) 재매핑 (습도/공기질 문맥)
# ===========================================================================
def _remap_erv_fan(result: dict, normalized: str, gz) -> None:
    if gz is None or "환기팬" not in normalized:
        return
    if not re.search(r"습도|미세먼지|초미세|공기\s*질|공기질", normalized):
        return
    erv = next((e["entity_id"] for e in gz.entities if e["domain"] == "fan"
                and ("전열교환기" in (e.get("name") or "") or e["entity_id"].endswith("erv"))), None)
    bath = next((e["entity_id"] for e in gz.entities if e["domain"] == "fan"
                 and "환풍기" in (e.get("name") or "")), None)
    if not erv or not bath:
        return
    model = result.get("model") or {}
    for sub in _subrules(model):
        for a in sub.get("actions", []):
            tgt = a.get("target")
            if not isinstance(tgt, dict):
                continue
            ids = tgt.get("entity_id")
            if isinstance(ids, str) and ids == bath:
                tgt["entity_id"] = erv
            elif isinstance(ids, list):
                tgt["entity_id"] = [erv if x == bath else x for x in ids]


# ===========================================================================
# 날씨형 전이 → numeric_state(습도/온도). '습해지/눅눅'=습도 above, '더워지'=온도 above,
#   '추워지'=온도 below. 명시 임계 우선, 없으면 관례 기본(습도 70·추위 20; 더위는 명시 필수).
# ===========================================================================
_WEATHER_HUMID_RE = re.compile(r"습해지|눅눅|축축|꿉꿉|습하|후덥")
_WEATHER_HOT_RE = re.compile(r"더워지|더워|더우|무더|후텁")
_WEATHER_COLD_RE = re.compile(r"추워지|추워|추우|쌀쌀|서늘|썰렁")
_WEATHER_THRESH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|퍼센트|프로|도)?\s*(?:이상|이하|초과|미만|보다)")


def _weather_numeric(normalized: str, gz, default_area):
    if gz is None:
        return None
    humid = _WEATHER_HUMID_RE.search(normalized)
    hot = _WEATHER_HOT_RE.search(normalized)
    cold = _WEATHER_COLD_RE.search(normalized)
    if not (humid or hot or cold):
        return None
    dc = "humidity" if humid else "temperature"
    area = _find_area(gz, normalized) or default_area
    cands = gz.resolve_concept({"domain": "sensor", "device_class": dc}, area, normalized)
    if not cands:
        return None
    node = {"type": "numeric_state", "entity_id": cands[0]["id"]}
    m = _WEATHER_THRESH_RE.search(normalized)
    explicit = float(m.group(1)) if m else None
    if humid:
        node["above"] = explicit if explicit is not None else 70.0
    elif hot:
        if explicit is None:
            return None
        node["above"] = explicit
    else:  # cold
        node["below"] = explicit if explicit is not None else 20.0
    return node


# ===========================================================================
# #24 수치 에지 마무리 — 범위이탈/이중에지(두 트리거)·between-트리거·단일에지 fallback
# ===========================================================================
_NUM_EXIT_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*도?\s*(?:에서|부터)\s*(-?\d+(?:\.\d+)?)\s*도?\s*"
    r"(?:사이|범위)\s*(?:를|에서|밖)?\s*(?:벗어|이탈)")
_NUM_DUAL_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*도?\s*(?:아래|밑|이하)\s*로?\s*(?:떨어지|내려가|낮아)"
    r".*?(?:거나|또는).*?"
    r"(-?\d+(?:\.\d+)?)\s*도?\s*(?:위|이상|초과)\s*로?\s*(?:올라가|넘|높아|초과)")
_NUM_BETWEEN_TRIG_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*도?\s*(?:에서|부터)\s*(-?\d+(?:\.\d+)?)\s*도?\s*사이")


def _numeric_sensor_id(text: str, gz) -> Optional[str]:
    """수치 센서 엔티티를 device_class 키워드로 강제 해석(온도/습도/미세먼지/전력)."""
    if re.search(r"습도", text):
        dc = "humidity"
    elif re.search(r"미세먼지|미세\s*먼지|pm", text, re.I):
        dc = "pm25"
    elif re.search(r"전력|와트|소비\s*전력", text):
        dc = "power"
    elif re.search(r"온도|\d+\s*도", text):
        dc = "temperature"
    else:
        return None
    area = _find_area(gz, text)
    cands = gz.resolve_concept({"domain": "sensor", "device_class": dc}, area, text)
    return cands[0]["id"] if cands else None


def _drop_sensor_service(sub: dict) -> None:
    """센서(sensor.*)를 대상으로 하는 오파싱 service 액션 제거(센서는 제어 불가)."""
    sub["actions"] = [a for a in sub["actions"]
                      if not (a.get("type") == "service"
                              and any(x.split(".")[0] == "sensor"
                                      for x in _target_ids(a)))]


def _augment_numeric_edge(normalized: str, sub: dict, gz, result=None) -> None:
    if gz is None:
        return
    em = _NUM_EXIT_RE.search(normalized)
    dm = None if em else _NUM_DUAL_RE.search(normalized)
    if em or dm:
        if em:
            a, b = float(em.group(1)), float(em.group(2))
            lo, hi = min(a, b), max(a, b)
        else:
            lo, hi = float(dm.group(1)), float(dm.group(2))
        eid = _numeric_sensor_id(normalized, gz)
        if eid is None:
            return
        sub["triggers"] = [
            {"type": "numeric_state", "entity_id": eid, "below": lo},
            {"type": "numeric_state", "entity_id": eid, "above": hi},
        ]
        _drop_sensor_service(sub)
        _mark_savable(result, sub)
        return
    if sub["triggers"] or any(c.get("type") == "numeric_state" for c in sub["conditions"]):
        return
    bm = _NUM_BETWEEN_TRIG_RE.search(normalized)
    if bm:
        lo, hi = float(bm.group(1)), float(bm.group(2))
        eid = _numeric_sensor_id(normalized, gz)
        if eid is None:
            return
        sub["triggers"] = [{"type": "numeric_state", "entity_id": eid,
                            "above": min(lo, hi), "below": max(lo, hi)}]
        _drop_sensor_service(sub)
        _mark_savable(result, sub)
        return
    below_m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:도|와트|퍼센트|프로|%)?\s*(?:이하|미만|밑|아래)", normalized)
    above_m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:도|와트|퍼센트|프로|%)?\s*(?:이상|초과)"
        r"|(\d+(?:\.\d+)?)\s*(?:도|와트|퍼센트|프로|%)?\s*(?:을|를)?\s*넘", normalized)
    if bool(below_m) == bool(above_m):
        return
    eid = _numeric_sensor_id(normalized, gz)
    if eid is None:
        return
    node = {"type": "numeric_state", "entity_id": eid}
    if below_m:
        node["below"] = float(below_m.group(1))
    else:
        node["above"] = float(above_m.group(1) or above_m.group(2))
    sub["triggers"] = [node]
    _drop_sensor_service(sub)
    _mark_savable(result, sub)


# ===========================================================================
# #25 held-for: "N분/시간 넘게 …켜져/열려 있으면" → state 트리거 for 지속
# ===========================================================================
_HELD_FOR_RE = re.compile(r"(\d+)\s*(분|시간|초)\s*(?:넘게|이상|동안|째|내내|계속)")


def _augment_held_for(normalized: str, sub: dict) -> None:
    m = _HELD_FOR_RE.search(normalized)
    if not m:
        return
    if not re.search(r"켜져|열려|꺼져|닫혀|잠겨|풀려|있으면|있을\s*때|있는데|있는\s*동안",
                     normalized):
        return
    unit = {"분": "minutes", "시간": "hours", "초": "seconds"}[m.group(2)]
    dur = {unit: int(m.group(1))}
    for i, t in enumerate(list(sub["triggers"])):
        if t.get("type") == "state" and "for" not in t:
            t["for"] = dur
            return
        if t.get("type") == "numeric_state":
            eid = t.get("entity_id")
            dom = eid.split(".")[0] if eid else ""
            if dom in ("fan", "light", "switch", "media_player", "climate",
                       "cover", "lock", "binary_sensor"):
                stv = _aspect_state(_QUOTE_SPAN_RE.sub(" ", normalized), eid)
                sub["triggers"][i] = {"type": "state", "entity_id": eid,
                                      "to": stv, "for": dur}
                sub["conditions"] = [
                    c for c in sub["conditions"]
                    if not (c.get("type") == "time"
                            or (c.get("type") == "state"
                                and c.get("entity_id") == eid))]
                return


# ===========================================================================
# #29 notify — 인용 메시지 + 알림 동사 → notify.notify(원문 sentence 에서 인용부 추출)
# ===========================================================================
_NOTIFY_VERB_RE = re.compile(
    r"알려|말해|말하|말씀|보내|방송|안내|얘기|알림|전해|전달|물어|여쭤|공지")


def _detect_notify(sent: str):
    if not _NOTIFY_VERB_RE.search(sent):
        return None
    qm = re.search(r"['\"“”‘’『』「」]"
                   r"([^'\"“”‘’『』「」]+)"
                   r"['\"“”‘’『』「」]", sent)
    if qm:
        message = qm.group(1).strip()
    else:
        qm2 = re.search(r"([가-힣]+)(다|라|냐|자)고(?![가-힣])", sent)
        if not qm2:
            return None
        message = (qm2.group(1) + qm2.group(2)).strip()
    if not message:
        return None
    data = {"message": message}
    phone = re.search(r"폰|휴대폰|핸드폰|모바일|스마트폰", sent)
    speaker = re.search(r"스피커|방송", sent)
    phone_neg = re.search(r"(?:폰|휴대폰|핸드폰|모바일|스마트폰)\s*(?:말고|대신|아니라|아니고)", sent)
    speaker_neg = re.search(r"(?:스피커|방송)\s*(?:말고|대신|아니라|아니고)", sent)
    if phone and not phone_neg:
        data["target"] = "mobile"
    elif speaker and not speaker_neg:
        data["target"] = "speaker"
    return data


# ===========================================================================
# #22 time_pattern(N분/시간/초 마다) — APP-PORT-PLAN §1.3·§2.3, S4
#   오버레이 _detect_time_pattern 이식. repeat 케이던스(간격/마다)와는 apply() 의
#   is_repeat 게이트로 구분한다(repeat 아닐 때만 방출).
# ===========================================================================
def _detect_time_pattern(text: str):
    """N분/시간/초 마다·간격·에 한 번 → (unit_key, value). 없으면 None(§1.2)."""
    # 'N시 M분마다'(벽시계+마다='매일 그 시각')는 daily 트리거지 주기가 아니다 — 제외.
    if re.search(r"\d\s*시\s*\d+\s*분\s*마다", text):
        return None
    m = re.search(r"매?\s*(\d+)\s*(시간|분|초)\s*(?:마다|간격|걸러|에\s*(?:\d+\s*)?번|당)", text)
    if not m:
        m = re.search(r"(\d+)\s*(시간|분|초)\s*에\s*한", text)  # 'N시간에 한 번'
    if not m:
        return None
    key = {"시간": "hours", "분": "minutes", "초": "seconds"}[m.group(2)]
    return key, int(m.group(1))


# ===========================================================================
# #30 repeat 풀구조 — 'N번 깜빡'(count) / '때까지 계속'(until)
# ===========================================================================
_NATIVE_CNT = r"(?:\d+|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|여러)"
_REPEAT_NATIVE_CNT = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6,
                      "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
_REPEAT_CNT_ARABIC_RE = re.compile(r"(\d+)\s*(?:번|차례|회)")
_REPEAT_CNT_NATIVE_RE = re.compile(
    r"(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:번|차례|회)")


def _is_repeat_action(text: str) -> bool:
    if re.search(_NATIVE_CNT + r"\s*(?:번|차례|회)\s*(?:만)?\s*"
                 r"[가-힣]{0,4}?(?:깜빡|반짝|반복|보내|점멸|열었다|껌뻑)", text):
        return True
    if re.search(r"깜빡깜빡|깜빡거|반짝반짝", text):
        return True
    if re.search(r"때까지\s*(?:.*?)(?:계속|깜빡|반짝|알려|알림|반복)", text):
        return True
    if re.search(r"계속\s*[가-힣\s]*?(?:깜빡|반짝|알려|알림)", text):
        return True
    return False


def _detect_repeat_count(text: str):
    m = _REPEAT_CNT_ARABIC_RE.search(text)
    if m:
        return int(m.group(1))
    m2 = _REPEAT_CNT_NATIVE_RE.search(text)
    if m2:
        return _REPEAT_NATIVE_CNT[m2.group(1)]
    return None


def _repeat_on_off_services(action: str):
    domain = (action or "").split(".")[0] or "homeassistant"
    if domain == "cover":
        return "cover.open_cover", "cover.close_cover"
    return domain + ".turn_on", domain + ".turn_off"


def _build_count_repeat(sub: dict, normalized: str):
    n = _detect_repeat_count(normalized)
    if n is None:
        return None
    base = next((a for a in (sub.get("actions") or [])
                 if isinstance(a, dict) and a.get("type") == "service"
                 and isinstance(a.get("target"), dict)
                 and a["target"].get("entity_id")), None)
    if base is None:
        return None
    on_svc, off_svc = _repeat_on_off_services(base.get("action", ""))
    target = base["target"]
    seq = [
        {"type": "service", "action": on_svc, "target": target},
        {"type": "delay", "duration": {"seconds": 1}},
        {"type": "service", "action": off_svc, "target": target},
        {"type": "delay", "duration": {"seconds": 1}},
    ]
    return {"type": "repeat", "kind": "count", "count": n, "sequence": seq}


def _build_until_repeat(sub: dict, normalized: str):
    if "때까지" not in normalized:
        return None
    trigs = [t for t in (sub.get("triggers") or [])
             if isinstance(t, dict) and t.get("type") == "state"
             and t.get("entity_id") and t.get("to") in ("on", "off")]
    if len(trigs) != 1:
        return None
    base = next((a for a in (sub.get("actions") or [])
                 if isinstance(a, dict) and a.get("type") == "service"
                 and isinstance(a.get("target"), dict)
                 and a["target"].get("entity_id")), None)
    if base is None:
        return None
    on_svc, off_svc = _repeat_on_off_services(base.get("action", ""))
    target = base["target"]
    inv_state = "off" if trigs[0]["to"] == "on" else "on"
    until_cond = {"type": "state", "entity_id": trigs[0]["entity_id"], "state": inv_state}
    seq = [
        {"type": "service", "action": on_svc, "target": target},
        {"type": "delay", "duration": {"seconds": 1}},
        {"type": "service", "action": off_svc, "target": target},
        {"type": "delay", "duration": {"seconds": 1}},
    ]
    return {"type": "repeat", "kind": "until", "conditions": [until_cond], "sequence": seq}


# ===========================================================================
# #27 액션 파라미터 — 색 팔레트/색온도/상대밝기/transition
# ===========================================================================
_RGB_PALETTE = [
    (re.compile(r"빨강|빨간|빨갛|붉은|적색|레드"), [255, 0, 0]),
    (re.compile(r"주황|주홍|오렌지"), [255, 126, 0]),
    (re.compile(r"노랑|노란|노랗|옐로"), [255, 220, 0]),
    (re.compile(r"초록|녹색|연두|그린"), [0, 255, 0]),
    (re.compile(r"파랑|파란|파랗|블루"), [0, 0, 255]),
    (re.compile(r"보라|자주색|퍼플|바이올렛"), [160, 32, 240]),
    (re.compile(r"분홍|핑크"), [255, 105, 180]),
    (re.compile(r"흰색|하얀색|백색|화이트"), [255, 255, 255]),
]
_KELVIN_PALETTE = [
    (re.compile(r"전구색|따뜻한\s*색|따뜻한색|따뜻하게|웜\s*화이트|온백색"), 2700),
    (re.compile(r"주백색|중백색|자연색"), 4000),
    (re.compile(r"주광색|하얀\s*불|하얀불|시원한\s*색|쿨\s*화이트|형광색"), 6500),
]


def _light_service_data(text: str, action: str) -> dict:
    data: dict = {}
    is_off = action.endswith("turn_off")
    if not is_off:
        matched = False
        for rx, kelvin in _KELVIN_PALETTE:
            if rx.search(text):
                data["color_temp_kelvin"] = kelvin
                matched = True
                break
        if not matched:
            for rx, rgb in _RGB_PALETTE:
                if rx.search(text):
                    data["rgb_color"] = list(rgb)
                    break
        step_sign = None
        if re.search(r"더\s*밝게|더밝게|밝게\s*좀|더\s*환하게", text):
            step_sign = 1
        elif re.search(r"어둡게", text) and not re.search(r"제일\s*어둡|가장\s*어둡|최소", text):
            step_sign = -1
        if step_sign is not None:
            pm = re.search(r"(\d+)\s*(?:퍼센트|프로|%|퍼)", text)
            data["brightness_step_pct"] = step_sign * (int(pm.group(1)) if pm else 20)
    tm = re.search(r"(\d+)\s*초\s*(?:에\s*걸쳐|동안|만큼|간)", text)
    if tm:
        data["transition"] = int(tm.group(1))
    elif re.search(r"천천히", text):
        data["transition"] = 10
    elif re.search(r"서서히|부드럽게|살살|스르르|자연스럽게", text):
        data["transition"] = 5
    return data


def _apply_light_params(text: str, sub: dict) -> None:
    for a in sub["actions"]:
        if a.get("type") != "service":
            continue
        act = a.get("action")
        if not (isinstance(act, str) and act.startswith("light.turn_")):
            continue
        extra = _light_service_data(text, act)
        if not extra:
            continue
        data = dict(a.get("data") or {})
        if "brightness_step_pct" in extra:
            data.pop("brightness_pct", None)   # 상대/절대 동시 금지(§3.1)
        data.update(extra)
        a["data"] = data


# ===========================================================================
# #28 toggle — 반대로/토글 → <domain>.toggle. S1 은 단일 도메인만(homeassistant.toggle 은 S6).
# ===========================================================================
_TOGGLE_RE = re.compile(r"반대\s*(?:로|상태)|토글")
_TOGGLABLE = {"turn_on", "turn_off", "open_cover", "close_cover", "lock", "unlock"}


def _apply_toggle(sub: dict) -> bool:
    svc = [a for a in sub["actions"]
           if a.get("type") == "service" and isinstance(a.get("action"), str)
           and a["action"].split(".", 1)[-1] in _TOGGLABLE]
    ids: list = []
    for a in svc:
        for x in _target_ids(a):
            if x not in ids:
                ids.append(x)
    if not ids:
        return False
    doms: list = []
    for x in ids:
        d = x.split(".")[0]
        if d not in doms:
            doms.append(d)
    # S1: 단일 도메인만 domain.toggle. 혼합 도메인(homeassistant.toggle)은 S6 까지 미적용.
    if len(doms) != 1:
        return False
    act = f"{doms[0]}.toggle"
    others = [a for a in sub["actions"] if a not in svc]
    sub["actions"] = [{"type": "service", "action": act,
                       "target": {"entity_id": ids}}] + others
    return True


# ===========================================================================
# #31 한정지속 복원 — "N분만 켰다가 꺼" → [act1, delay, act2] (결과상 트리거 승격)
# ===========================================================================
_REVERT_RE = re.compile(
    r"(\d+)\s*(분|시간|초)\s*(?:만|정도|가량|쯤|동안)?\s*(?:좀\s*|딱\s*|그냥\s*|정도\s*)*"
    r"(?:[가-힣]+\s+)?"
    r"(?P<v1>켰다|켜졌|켜놨|켜놓|켜뒀|틀었|틀어|돌리|돌려|열었|열어|껐다|꺼놨|꺼졌|꺼뒀|껐)")
_REVERT_SERVICES = {
    "light": ("light.turn_on", "light.turn_off"),
    "fan": ("fan.turn_on", "fan.turn_off"),
    "switch": ("switch.turn_on", "switch.turn_off"),
    "climate": ("climate.turn_on", "climate.turn_off"),
    "media_player": ("media_player.turn_on", "media_player.turn_off"),
    "cover": ("cover.open_cover", "cover.close_cover"),
    "lock": ("lock.unlock", "lock.lock"),
}
_REVERT_OFF_FIRST = {"껐다", "꺼놨", "꺼졌", "꺼뒀", "껐"}


def _apply_revert(normalized: str, sub: dict) -> bool:
    m = _REVERT_RE.search(normalized)
    if not m:
        return False
    ids = domain = None
    for a in sub["actions"]:
        if a.get("type") != "service":
            continue
        t = _target_ids(a)
        if t:
            ids, domain = t, t[0].split(".")[0]
            break
    if not ids or domain not in _REVERT_SERVICES:
        return False
    on_svc, off_svc = _REVERT_SERVICES[domain]
    off_first = m.group("v1") in _REVERT_OFF_FIRST
    unit = {"분": "minutes", "시간": "hours", "초": "seconds"}[m.group(2)]
    dur = {unit: int(m.group(1))}
    first, second = (off_svc, on_svc) if off_first else (on_svc, off_svc)
    sub["actions"] = [
        {"type": "service", "action": first, "target": {"entity_id": list(ids)}},
        {"type": "delay", "duration": dur},
        {"type": "service", "action": second, "target": {"entity_id": list(ids)}},
    ]
    if not sub["triggers"] and len(ids) == 1:
        am = re.search(r"(?:켜져|꺼져|열려|닫혀|잠겨|풀려)\s*있", normalized)
        if am:
            stv = _aspect_state(_QUOTE_SPAN_RE.sub(" ", normalized), ids[0])
            sub["triggers"] = [{"type": "state", "entity_id": ids[0], "to": stv}]
    return True


# ===========================================================================
# 액션측 후처리 — repeat → notify → revert → toggle → light params (기존 노드만)
# ===========================================================================
def _augment_actions_only(sentence: str, normalized: str, sub: dict, is_repeat: bool) -> None:
    if is_repeat:
        if not sub["triggers"]:
            for c in list(sub["conditions"]):
                if c.get("type") == "state":
                    sub["triggers"].append({"type": "state",
                                            "entity_id": c.get("entity_id"),
                                            "to": c.get("state")})
                    sub["conditions"].remove(c)
                    break
                if c.get("type") == "numeric_state":
                    nt = {"type": "numeric_state", "entity_id": c.get("entity_id")}
                    if c.get("above") is not None:
                        nt["above"] = c["above"]
                    if c.get("below") is not None:
                        nt["below"] = c["below"]
                    sub["triggers"].append(nt)
                    sub["conditions"].remove(c)
                    break
        rep = _build_count_repeat(sub, normalized)
        if rep is None:
            rep = _build_until_repeat(sub, normalized)
        sub["actions"] = [rep] if rep is not None else [{"type": "repeat"}]
        # S1: 잔여 조건 중 신규 노드(weekday/day_of_month/interval_anchor/sun_window)만 보존
        #     — 이들은 S2+ 이전엔 방출되지 않으므로 실질적으로 조건을 비운다(repeat gold 규약).
        sub["conditions"] = [c for c in sub["conditions"]
                             if c.get("type") in ("weekday", "day_of_month",
                                                  "interval_anchor", "sun_window")]
        return
    nd = _detect_notify(sentence)
    if nd is not None:
        sub["actions"] = [{"type": "service", "action": "notify.notify", "data": nd}]
        return
    if _apply_revert(normalized, sub):
        return
    if _TOGGLE_RE.search(normalized) and _apply_toggle(sub):
        return
    _apply_light_params(sentence, sub)


# ===========================================================================
# #26 부정 NOT 래퍼 — "N 넘지 않으면/안 넘으면" = NOT[numeric_state above N]
# ===========================================================================
_NUM_NEG_ABOVE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:도|퍼센트|프로|%|와트)?\s*(?:을|를)?\s*"
    r"(?:(?:안|못)\s*넘|넘지\s*않|초과하지\s*않|이상\s*(?:이\s*)?아니)")
_NUM_NEG_BELOW_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:도|퍼센트|프로|%|와트)?\s*(?:이하|미만)?\s*(?:로\s*)?"
    r"(?:(?:안|못)\s*(?:내려가|떨어지)|내려가지\s*않|떨어지지\s*않|이하\s*아니|미만\s*아니)")


def _augment_negation_not(sentence: str, normalized: str, result: dict, gz) -> None:
    if not isinstance(result, dict) or gz is None:
        return
    model = result.get("model")
    if not isinstance(model, dict):
        return
    sub = _primary_subrule(model)
    if sub is None:
        return
    am = _NUM_NEG_ABOVE_RE.search(normalized)
    bm = None if am else _NUM_NEG_BELOW_RE.search(normalized)
    if not (am or bm):
        return
    if any(t.get("type") == "numeric_state" for t in sub.get("triggers", []) or []):
        return
    if any(c.get("type") == "numeric_state" for c in sub.get("conditions", []) or []):
        return
    eid = _numeric_sensor_id(normalized, gz)
    if eid is None:
        return
    key = "above" if am else "below"
    val = float((am or bm).group(1))
    inner = {"type": "numeric_state", "entity_id": eid, key: val}
    sub.setdefault("conditions", [])
    if not any(c.get("type") == "not" for c in sub["conditions"]):
        sub["conditions"].append({"type": "not", "conditions": [inner]})


# ===========================================================================
# #20 sun 트리거 / sun_window 조건 (APP-PORT-PLAN §1.3·§2.1, 오버레이 _augment_time_calendar 이식)
# ===========================================================================
def _sun_offset(text: str) -> int:
    """offset(초). 'N분/시간/초' 합산, '시간 반'=+1800. '전/이전/앞'=음수, 그 외 양수."""
    secs = 0
    for m in re.finditer(r"(\d+)\s*(시간|분|초)", text):
        secs += int(m.group(1)) * {"시간": 3600, "분": 60, "초": 1}[m.group(2)]
    if "반" in text and "시간" in text:
        secs += 1800
    if secs == 0:
        return 0
    # 음수는 'N분/시간 (반) 전/이전/앞' 만 — '완전히' 등에 든 '전' 오탐 방지.
    return -secs if re.search(r"(?:분|시간|초)\s*(?:반\s*)?(?:전|이전|앞)", text) else secs


def _augment_sun(normalized: str, sub: dict, result=None) -> None:
    """일몰/일출(±오프셋)·밤창 → sun 트리거 / sun_window 조건(§2.1).

    - 실제 이벤트(상태/수치/존) 트리거·조건이 있으면 그것이 주 트리거, 밤/어두운 창=sun_window
      조건(밤/새벽 세그먼트 제거 후, 트리거가 비면 상태/수치 조건을 진입에지 트리거로 승격).
    - 트리거가 아예 없고 일몰/일출만 있으면 sun 트리거(offset). 오프셋 표현이 만든 spurious
      delay 액션·time 조건은 제거(sun 오프셋이 곧 그 시간차 — gold sun 은 delay/time 없음).
    """
    trigs = sub["triggers"]
    conds = sub["conditions"]
    sun_evt = "sunrise" if _SUNRISE_RE.search(normalized) else (
        "sunset" if _SUNSET_RE.search(normalized) else None)
    nightwin = bool(_NIGHTWIN_RE.search(normalized))
    real_event = any(n.get("type") in ("state", "numeric_state", "zone",
                                       "state_held", "group_held")
                     for n in list(trigs) + list(conds))
    if (sun_evt or nightwin) and real_event:
        sub["triggers"] = [t for t in trigs if not (
            t.get("type") == "segment" and t.get("to") in ("night", "dawn"))]
        sub["conditions"] = [c for c in sub["conditions"] if not (
            c.get("type") == "time_segment"
            and (set(c.get("segments") or []) & {"night", "dawn"}))]
        if not sub["triggers"]:
            promoted, rest = None, []
            for c in sub["conditions"]:
                if promoted is None and c.get("type") in ("state", "numeric_state"):
                    promoted = c
                else:
                    rest.append(c)
            if promoted is not None:
                if promoted.get("type") == "state":
                    sub["triggers"].append({"type": "state",
                                            "entity_id": promoted.get("entity_id"),
                                            "to": promoted.get("state")})
                else:
                    nt = {"type": "numeric_state", "entity_id": promoted.get("entity_id")}
                    if promoted.get("above") is not None:
                        nt["above"] = promoted["above"]
                    if promoted.get("below") is not None:
                        nt["below"] = promoted["below"]
                    sub["triggers"].append(nt)
                sub["conditions"] = rest
        if not any(c.get("type") == "sun_window" for c in sub["conditions"]):
            sub["conditions"].append({"type": "sun_window",
                                      "after": "sunset", "before": "sunrise"})
        _mark_savable(result, sub)
    elif sun_evt and not trigs:
        node = {"type": "sun", "event": sun_evt}
        off = _sun_offset(normalized)
        if off:
            node["offset"] = off
        sub["triggers"].append(node)
        sub["actions"] = [a for a in sub["actions"] if a.get("type") != "delay"]
        sub["conditions"] = [c for c in sub["conditions"] if c.get("type") != "time"]
        _mark_savable(result, sub)


def _augment_time_pattern(normalized: str, sub: dict, result=None) -> None:
    """N분/시간/초 마다 → time_pattern 트리거(§1.2·§2.3, #22).

    패턴이 주 트리거이므로 기존 상태/수치 트리거는 조건으로 강등하고, 잔여 daily/segment
    트리거는 제거한다(다른 유형 트리거는 보존). apply() 가 is_repeat 이 아닐 때만 호출하므로
    repeat 케이던스('1초 간격으로 세 번 깜빡')와 충돌하지 않는다.
    """
    tp = _detect_time_pattern(normalized)
    if not tp:
        return
    if any(t.get("type") == "time_pattern" for t in sub["triggers"]):
        return
    new_trigs = [{"type": "time_pattern", tp[0]: tp[1]}]
    for t in sub["triggers"]:
        if t.get("type") == "state":
            sub["conditions"].append({"type": "state",
                                      "entity_id": t.get("entity_id"),
                                      "state": t.get("to")})
        elif t.get("type") == "numeric_state":
            c = {"type": "numeric_state", "entity_id": t.get("entity_id")}
            if t.get("above") is not None:
                c["above"] = t["above"]
            if t.get("below") is not None:
                c["below"] = t["below"]
            sub["conditions"].append(c)
        elif t.get("type") in ("daily", "segment"):
            pass  # 패턴이 주 트리거 — 잔여 시각/세그먼트 트리거 제거
        else:
            new_trigs.append(t)
    sub["triggers"] = new_trigs
    _mark_savable(result, sub)


# ===========================================================================
# #21 weekday / day_of_month / interval_anchor (APP-PORT-PLAN §1.3·§2.5, S3)
#   오버레이 _detect_weekdays/_detect_day_of_month/_detect_interval + _augment_time_calendar
#   달력 절 이식. interval_anchor.anchor 는 고정상수가 아니라 **주입된 now(기본 실제
#   datetime)가 속한 주의 월요일**로 산출 — 결정성은 now_fn 주입으로만 보장한다(Date 직접
#   호출은 이 한 지점의 now_fn 폴백뿐, 스캐터 금지).
# ===========================================================================
_DAY_MAP = {"월": "mon", "화": "tue", "수": "wed", "목": "thu",
            "금": "fri", "토": "sat", "일": "sun"}
_WEEKDAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _days_of_token(tok: str) -> list:
    """요일 토큰 → days 목록. 주말/평일/주중·'X요일'·요일 축약(월수금)."""
    if "주말" in tok:
        return ["sat", "sun"]
    if "평일" in tok or "주중" in tok:
        return ["mon", "tue", "wed", "thu", "fri"]
    days: list = []
    for m in re.finditer(r"([월화수목금토일])요일", tok):
        d = _DAY_MAP[m.group(1)]
        if d not in days:
            days.append(d)
    if days:
        return days
    if 2 <= len(tok) <= 6 and all(ch in _DAY_MAP for ch in tok):  # 월수금/화목토/화목
        for ch in tok:
            d = _DAY_MAP[ch]
            if d not in days:
                days.append(d)
    return days


def _detect_weekdays(text: str):
    """요일 집합(days, negate, is_bare) 또는 (None, False, False)(§2.2).

    부정(빼고/말고/제외)·개별 요일·요일 축약(월수금)만 weekday 노드로 방출한다.
    맨 '평일/주말/주중'(긍정)은 기존 gold 가 day_type 을 쓰므로 is_bare=True 로 표시해
    호출측이 이벤트 트리거 유무로 승격을 결정한다(동일 표면형 라벨 규약 회귀 방지).
    """
    neg = re.search(
        r"((?:[월화수목금토일]요일\s*(?:이랑|랑|하고|과|와|,)\s*)+[월화수목금토일]요일"  # 병렬 요일
        r"|주말|평일|주중|[월화수목금토일]요일|[월화수목금토일]{2,})"
        r"\s*(?:만)?\s*(?:은|는)?\s*(?:빼고|말고|제외)", text)
    if neg:
        d = _days_of_token(neg.group(1))
        if d:
            return d, True, False
    days: list = []
    explicit = False
    for m in re.finditer(r"([월화수목금토일])요일", text):
        explicit = True
        dd = _DAY_MAP[m.group(1)]
        if dd not in days:
            days.append(dd)
    for tok in re.split(r"[\s,]+", text):
        if 2 <= len(tok) <= 6 and all(ch in _DAY_MAP for ch in tok):
            explicit = True
            for ch in tok:
                dd = _DAY_MAP[ch]
                if dd not in days:
                    days.append(dd)
    bareword = None
    if "평일" in text or "주중" in text:
        bareword = ["mon", "tue", "wed", "thu", "fri"]
    elif "주말" in text:
        bareword = ["sat", "sun"]
    if bareword:
        for dd in bareword:
            if dd not in days:
                days.append(dd)
    if not days:
        return None, False, False
    days.sort(key=_WEEKDAY_ORDER.index)
    # is_bare: 맨 평일/주말/주중만(개별 요일·축약 없음) — 기존 day_type gold 보호 게이트용.
    return days, False, (bareword is not None and not explicit)


def _detect_day_of_month(text: str):
    """매달 N일 → [N…], 말일/마지막 날 → 'last', 짝수날/홀수날 → 목록. 없으면 None(§2.3)."""
    if re.search(r"말일|마지막\s*날|월말", text):
        return "last"
    if re.search(r"짝수\s*날", text):
        return [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30]
    if re.search(r"홀수\s*날", text):
        return [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31]
    if re.search(r"매\s*달|매월|이번\s*달|다음\s*달", text):
        days = [int(m.group(1)) for m in re.finditer(r"(\d{1,2})\s*일", text)]
        days = [d for d in days if 1 <= d <= 31]
        if days:
            return days
    return None


def _detect_interval(text: str):
    """격주/N주에 한 번/N주마다 → interval 정수(≥2). 없으면 None(§2.4)."""
    if "격주" in text:
        return 2
    # 'N주에 한 번'의 '한'은 normalize 로 '1'이 되기도 하므로 (\d+|한) 둘 다 허용.
    m = re.search(r"(\d+)\s*주\s*(?:에\s*(?:\d+|한)\s*번|마다|간격|걸러)", text)
    if m and int(m.group(1)) >= 2:
        return int(m.group(1))
    return None


def _monday_iso(now_fn) -> str:
    """주입된 now(기본 실제 datetime)가 속한 주의 월요일 ISO 날짜 = interval_anchor.anchor.

    now 는 주입으로만 통제한다 — 측정/테스트가 now_fn 을 넘기면 결정적, 미주입 시 벽시계.
    Date 직접호출은 이 폴백 한 지점뿐(SPEC §2.4 라벨 규약: 기준일 없으면 그 주 월요일).
    """
    now = (now_fn or (lambda: datetime.now()))()
    monday = now.date() - timedelta(days=now.weekday())
    return monday.isoformat()


def _augment_calendar(normalized: str, sub: dict, now_fn=None) -> None:
    """요일/매달 N일/격주 조건을 결정적으로 얹는다(§2.2~2.4). 기존 노드 트리거는 보존."""
    # 요일(§2.2). 개별 요일/축약/부정은 항상 weekday. 맨 평일/주말(긍정)은 이벤트(상태/수치/
    # 존) 트리거가 있으면서 시간대(segment) 동반 문맥이 아닐 때만 weekday 로 승격한다 —
    # 기존 day_type gold 보존: "주말 아침에 …움직이면"(day_type+time_segment)은 승격하지 않고,
    # "평일에 현관문 열리면"(bare+이벤트, 시간대 없음)만 weekday 로 올린다(test_regr_6 vs _16).
    wd, wneg, wbare = _detect_weekdays(normalized)
    if wd:
        if wbare:
            edge = any(t.get("type") in ("state", "numeric_state", "zone",
                                         "state_held", "group_held")
                       for t in sub["triggers"])
            has_segment = any(c.get("type") == "time_segment"
                              for c in sub["conditions"]) \
                or any(t.get("type") == "segment" for t in sub["triggers"])
            do_promote = edge and not has_segment
        else:
            do_promote = True
        if do_promote:
            sub["conditions"] = [c for c in sub["conditions"]
                                 if c.get("type") != "day_type"]
            if not any(c.get("type") == "weekday" for c in sub["conditions"]):
                sub["conditions"].append({"type": "weekday", "days": wd,
                                          "negate": wneg})

    # 매달 N일/말일(§2.3)
    dom = _detect_day_of_month(normalized)
    if dom is not None and not any(c.get("type") == "day_of_month"
                                   for c in sub["conditions"]):
        sub["conditions"].append({"type": "day_of_month", "days": dom})

    # 격주/N주기(§2.4). anchor = 주입 now 가 속한 주의 월요일(결정성).
    iv = _detect_interval(normalized)
    if iv is not None and not any(c.get("type") == "interval_anchor"
                                  for c in sub["conditions"]):
        sub["conditions"].append({"type": "interval_anchor", "unit": "week",
                                  "interval": iv, "anchor": _monday_iso(now_fn)})


# ---------------------------------------------------------------------------
# 공개 진입점
# ---------------------------------------------------------------------------
def apply(result: dict, sentence: str, normalized: str, gz, settings,
          now_fn=None) -> dict:
    """모델 후처리 파이프라인. result 를 수정 후 반환.

    방출 신규 노드: sun/sun_window(S2) + weekday/day_of_month/interval_anchor(S3) +
    time_pattern(S4). presence_agg 는 아직 미방출(S5+). now_fn 은 interval_anchor.anchor
    결정성용(주입 없으면 벽시계 — _monday_iso 참조).
    """
    if not isinstance(result, dict):
        return result
    _remap_erv_fan(result, normalized, gz)
    sub = _primary_subrule(result.get("model") or {})
    if sub is None:
        # 다중 서브룰/비정형: 액션 후처리 대상 아님(else/다중절은 S7).
        return result
    sub.setdefault("triggers", [])
    sub.setdefault("conditions", [])
    sub.setdefault("actions", [])

    is_repeat = _is_repeat_action(normalized)

    _augment_calendar(normalized, sub, now_fn)   # #21 weekday/day_of_month/interval_anchor (S3)

    # 날씨형 전이 → numeric_state(기존 노드). 수치 노드가 아직 없을 때만.
    if not any(t.get("type") == "numeric_state" for t in sub["triggers"]) \
            and not any(c.get("type") == "numeric_state" for c in sub["conditions"]):
        wnode = _weather_numeric(normalized, gz, None)
        if wnode is not None:
            sub["triggers"] = [t for t in sub["triggers"]
                               if t.get("type") not in ("segment", "daily")]
            sub["triggers"].insert(0, wnode)
            _drop_sensor_service(sub)
            _mark_savable(result, sub)

    _augment_sun(normalized, sub, result)   # #20 sun/sun_window (S2)
    if not is_repeat:
        _augment_time_pattern(normalized, sub, result)   # #22 time_pattern (S4)
    _augment_numeric_edge(normalized, sub, gz, result)
    _augment_held_for(normalized, sub)
    _augment_actions_only(sentence, normalized, sub, is_repeat)
    _augment_negation_not(sentence, normalized, result, gz)
    return result
