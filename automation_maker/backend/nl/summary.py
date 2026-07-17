"""확인 카드 요약 서술 — 파서·후처리 공용(신규 노드 포함).

parser._Parser._summary 의 모델 서술 로직을 모델 기반 순수 함수로 추출한다(단일 소스).
파서는 파싱 직후 이 함수로 요약을 만들고, 후처리(postpass)는 신규 노드(sun/sun_window/
weekday/day_of_month/interval_anchor/time_pattern/presence_agg/if)를 얹은 뒤 이 함수로
요약을 재생성한다 — 확인 카드에 미서술 노드가 남지 않게 한다(APP-PORT-PLAN §1.3 S7).

순환 import 방지: parser/postpass 를 import 하지 않고 normalize·gazetteer 만 쓴다.
"""
from __future__ import annotations

from .gazetteer import SEGMENT_WORDS
from .normalize import josa_eul_reul, josa_i_ga

_DAY_TYPE_LABELS = {"weekday": "평일", "weekend": "주말", "holiday": "공휴일"}
_SEASON_LABELS = {"spring": "봄", "summer": "여름", "autumn": "가을", "winter": "겨울"}
_WEEKDAY_LABELS = {"mon": "월", "tue": "화", "wed": "수", "thu": "목",
                   "fri": "금", "sat": "토", "sun": "일"}


# ---------------------------------------------------------------------------
# 기존 파서 헬퍼의 모델 기반 사본(_Parser._summary 의 self.* 의존 제거)
# ---------------------------------------------------------------------------
def _nm(gz, eid):
    if not eid:
        return "대상"
    e = gz.entity(eid) if gz else None
    if e:
        return e.get("name") or eid
    if gz:
        for surf, pid in gz.person_surfaces.items():
            if pid == eid:
                return surf
    return eid


def _is_motion_name(nm) -> bool:
    return bool(nm) and any(w in nm for w in ("모션", "움직임", "인기척", "동작", "재실"))


def _state_verb(gz, eid, to):
    e = gz.entity(eid) if gz else None
    dc = e.get("device_class") if e else None
    if dc in ("door", "window", "opening", "garage_door"):
        return "열리면" if to == "on" else "닫히면"
    if dc in ("motion", "occupancy", "presence"):
        return "움직임이 감지되면" if to == "on" else "움직임이 없으면"
    return "켜지면" if to == "on" else "꺼지면"


def _dur(d):
    d = d or {}
    if d.get("hours"):
        return f"{d['hours']}시간"
    if d.get("minutes"):
        return f"{d['minutes']}분"
    return f"{d.get('seconds', 0)}초"


# ---------------------------------------------------------------------------
# 신규 노드 라벨(요약·칩 공용) — 사람이 읽는 한국어(APP-PORT-PLAN §1.3 S7)
# ---------------------------------------------------------------------------
def _amount(secs: int) -> str:
    secs = abs(int(secs))
    if secs and secs % 3600 == 0:
        return f"{secs // 3600}시간"
    if secs and secs % 60 == 0:
        return f"{secs // 60}분"
    return f"{secs}초"


def sun_label(node) -> str:
    """sun 트리거: 일출/일몰(±오프셋). '해 지기 30분 전' / '해 뜰 때'."""
    ev = node.get("event")
    stem = "뜨" if ev == "sunrise" else "지"
    off = node.get("offset")
    if off:
        when = "전" if int(off) < 0 else "후"
        return f"해 {stem}기 {_amount(off)} {when}"
    return "해 뜰 때" if ev == "sunrise" else "해 질 때"


def sun_window_label(node) -> str:
    after = node.get("after", "sunset")
    before = node.get("before", "sunrise")
    a = "해 뜬 뒤" if after == "sunrise" else "해 진 뒤"
    b = "해 뜰 때까지" if before == "sunrise" else "해 질 때까지"
    return f"{a}부터 {b}"


def weekday_label(node) -> str:
    days = node.get("days") or []
    txt = "·".join(_WEEKDAY_LABELS.get(d, d) for d in days)
    return (txt + " 제외") if node.get("negate") else txt


def day_of_month_label(node) -> str:
    days = node.get("days")
    if days == "last":
        return "매달 말일"
    if isinstance(days, list) and days:
        return "매달 " + ", ".join(f"{d}일" for d in days)
    return "매달"


def interval_label(node) -> str:
    iv = node.get("interval")
    return "격주마다" if iv == 2 else f"{iv}주마다"


def time_pattern_label(node) -> str:
    for k, u in (("minutes", "분"), ("hours", "시간"), ("seconds", "초")):
        if node.get(k):
            return f"{node[k]}{u}마다"
    return "주기적으로"


def presence_label(node, as_trigger: bool) -> str:
    q = node.get("quant")
    if as_trigger:
        base = {"first": "처음 집에 도착하면", "last": "마지막 사람이 나가면",
                "any": "누군가 집에 도착하면",
                "all": "모두 집에 도착하면"}.get(q, "재실 상태가 바뀌면")
        fr = node.get("for")
        if fr and q in ("last", "all"):
            state = "아무도 없는" if q == "last" else "모두 있는"
            base += f" ({_dur(fr)} {state} 상태 유지)"
        return base
    return {"none": "아무도 없으면", "any": "누군가 집에 있으면",
            "all": "모두 집에 있으면"}.get(q, "재실 조건")


# ---------------------------------------------------------------------------
# 노드 서술기(조건/액션) — if 분기가 재귀 재사용
# ---------------------------------------------------------------------------
def _describe_condition(c, gz) -> list:
    """조건 노드 → 서술 구절 목록(0~2개). 미지원 노드는 빈 목록."""
    typ = c.get("type")
    if typ == "time_segment":
        w = [k for k, v in SEGMENT_WORDS.items() if v == c["segments"][0]]
        return [f"{w[0] if w else ''} 시간대"]
    if typ == "day_type":
        labels = [_DAY_TYPE_LABELS.get(x, x) for x in c.get("types", [])]
        return ["/".join(labels)]
    if typ == "season":
        labels = [_SEASON_LABELS.get(x, x) for x in c.get("seasons", [])]
        return ["/".join(labels)]
    if typ == "time":
        out = []
        if c.get("after"):
            out.append(f"{c['after'][:5]} 이후")
        if c.get("before"):
            out.append(f"{c['before'][:5]} 이전")
        return out
    if typ == "numeric_state":
        nm = _nm(gz, c.get("entity_id"))
        out = []
        if c.get("above") is not None:
            out.append(f"{nm}{josa_i_ga(nm)} {c['above']} 이상")
        if c.get("below") is not None:
            out.append(f"{nm}{josa_i_ga(nm)} {c['below']} 이하")
        return out
    if typ == "state":
        nm = _nm(gz, c.get("entity_id"))
        return [f"{nm} 상태가 {c.get('state')}"]
    if typ == "mode":
        return [f"{c.get('mode')} {'켜짐' if c.get('state') == 'on' else '꺼짐'}"]
    # --- 신규 노드(S2~S5) ---
    if typ == "sun_window":
        return [sun_window_label(c)]
    if typ == "weekday":
        return [weekday_label(c)]
    if typ == "day_of_month":
        return [day_of_month_label(c)]
    if typ == "interval_anchor":
        return [interval_label(c)]
    if typ == "presence_agg":
        return [presence_label(c, False)]
    if typ == "zone":
        nm = _nm(gz, c.get("entity_id"))
        return [f"{nm}{josa_i_ga(nm)} 집에 있으면"]
    if typ == "not":
        subs = c.get("conditions") or []
        if len(subs) == 1 and subs[0].get("type") == "zone":
            nm = _nm(gz, subs[0].get("entity_id"))
            return [f"{nm}{josa_i_ga(nm)} 집에 없으면"]
        inner = []
        for sc in subs:
            inner.extend(_describe_condition(sc, gz))
        return [f"{' 그리고 '.join(inner)}(이/가) 아니면"] if inner else []
    return []


def _describe_action(a, gz) -> str:
    """액션 노드 → 서술 구절. 기존 파서 _summary adesc 로직 보존 + 신규(if/notify/toggle)."""
    typ = a.get("type")
    if typ == "delay":
        return f"{_dur(a.get('duration', {}))} 뒤에"
    if typ == "set_mode":
        return (f"{a.get('mode')}{josa_eul_reul(a.get('mode') or '')} "
                f"{'켭니다' if a.get('to') == 'on' else '끕니다'}")
    if typ == "if":
        conds = []
        for c in a.get("if") or []:
            conds.extend(_describe_condition(c, gz))
        cond_txt = ", ".join(conds) if conds else "조건에 맞으면"
        joiner = "" if cond_txt.endswith(("면", "때")) else "이면"
        then_txt = ", ".join(x for x in (_describe_action(y, gz)
                                         for y in (a.get("then") or [])) if x)
        else_txt = ", ".join(x for x in (_describe_action(y, gz)
                                         for y in (a.get("else") or [])) if x)
        head = f"{cond_txt}{joiner}".strip()
        if else_txt:
            return f"{head} {then_txt}, 아니면 {else_txt}"
        return f"{head} {then_txt}"
    if typ == "repeat":
        return "반복 동작을 실행합니다"
    act = a.get("action", "")
    tgt = a.get("target", {}).get("entity_id", [])
    nm = _nm(gz, tgt[0]) if tgt else act.split(".")[0]
    if act == "notify.notify":
        msg = (a.get("data") or {}).get("message")
        return f"'{msg}'라고 알립니다" if msg else "알림을 보냅니다"
    if act.endswith("turn_on") or act == "cover.open_cover":
        extra = ""
        if a.get("data", {}).get("brightness_pct"):
            extra = f" {a['data']['brightness_pct']}% 밝기로"
        verb = "엽니다" if act == "cover.open_cover" else "켭니다"
        return f"{nm}{josa_eul_reul(nm)}{extra} {verb}"
    if act.endswith("turn_off") or act == "cover.close_cover":
        verb = "닫습니다" if act == "cover.close_cover" else "끕니다"
        return f"{nm}{josa_eul_reul(nm)} {verb}"
    if act.endswith("toggle"):
        return f"{nm}{josa_eul_reul(nm)} 반대로 바꿉니다"
    if act == "climate.set_fan_mode":
        return f"팬 모드를 {a['data'].get('fan_mode')}로 설정합니다"
    if act and act.startswith("scene."):
        return "모드를 전환합니다"
    return f"{nm} 동작을 실행합니다"


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------
def summarize_subrule(triggers, conditions, actions, gz) -> str:
    """단일 서브룰(트리거/조건/액션) → 확인 카드 한국어 요약(파서 _summary 와 동형)."""
    triggers = triggers or []
    conditions = conditions or []
    actions = actions or []
    parts = []
    tdesc = []
    zone_persons = []
    for t in triggers:
        typ = t.get("type")
        if typ == "zone":
            zone_persons.append(_nm(gz, t.get("entity_id")))
            continue
        if typ == "mode":
            tdesc.append(f"{t.get('mode')}{josa_i_ga(t.get('mode') or '')} "
                         f"{'켜지면' if t.get('to') == 'on' else '꺼지면'}")
            continue
        if typ == "state":
            nm = _nm(gz, t.get("entity_id"))
            verb = _state_verb(gz, t.get('entity_id'), t.get('to'))
            if _is_motion_name(nm):
                verb = verb.replace("움직임이 ", "")
            tdesc.append(f"{nm}{josa_i_ga(nm)} {verb}")
        elif typ == "state_held":
            nm = _nm(gz, t.get("entity_id"))
            tdesc.append(f"{nm}{josa_i_ga(nm)} {_dur(t['for'])} 동안 "
                         f"{'없으면' if t.get('to') == 'off' else '있으면'}")
        elif typ == "group_held":
            tdesc.append(f"다른 곳 움직임이 {_dur(t['for'])} 동안 없으면")
        elif typ == "numeric_state":
            nm = _nm(gz, t.get("entity_id"))
            if t.get("above") is not None:
                tdesc.append(f"{nm}{josa_i_ga(nm)} {t['above']} 이상이 되면")
            elif t.get("below") is not None:
                tdesc.append(f"{nm}{josa_i_ga(nm)} {t['below']} 이하가 되면")
        elif typ == "segment":
            w = [k for k, v in SEGMENT_WORDS.items() if v == t.get("to")]
            word = w[0] if w else ""
            tdesc.append(f"{word}{josa_i_ga(word)} 되면")
        elif typ == "daily":
            tdesc.append(f"매일 {t.get('at')}에")
        elif typ == "sun":
            tdesc.append(sun_label(t))
        elif typ == "time_pattern":
            tdesc.append(time_pattern_label(t))
        elif typ == "presence_agg":
            tdesc.append(presence_label(t, True))
    if zone_persons:
        joined = ' 또는 '.join(zone_persons)
        tdesc.insert(0, f"{joined}{josa_i_ga(joined)} 집에 도착하면")
    if tdesc:
        parts.append(" 또는 ".join(tdesc))
    cdesc = []
    for c in conditions:
        cdesc.extend(_describe_condition(c, gz))
    adesc = []
    for a in actions:
        phrase = _describe_action(a, gz)
        if phrase:
            adesc.append(phrase)
    cond_txt = (", " + " 그리고 ".join(cdesc)) if cdesc else ""
    return f"{' '.join(parts)}{cond_txt} → {', '.join(adesc)}." if parts else \
           f"{', '.join(adesc)}."


def summarize_model(model: dict, gz) -> str:
    """전체 model(단일/다중 서브룰) → 요약. 다중은 서브룰별 요약을 ' 그리고 '로 잇는다."""
    if not isinstance(model, dict):
        return ""
    subs = model.get("subrules")
    if isinstance(subs, list) and subs:
        parts = [summarize_subrule(s.get("triggers"), s.get("conditions"),
                                   s.get("actions"), gz)
                 for s in subs if isinstance(s, dict)]
        return " 그리고 ".join(p for p in parts if p)
    return summarize_subrule(model.get("triggers"), model.get("conditions"),
                             model.get("actions"), gz)
