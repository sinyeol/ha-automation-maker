"""SPEC-ACCURACY §2.5 / §2.6 — A그룹 규칙 수정 오프라인 오버레이 (앱 미수정).

앱 파일(`automation_maker/backend/nl/parser.py`·`gazetteer.py`)을 **전혀 수정하지 않고**,
파서 모듈의 상수·함수·메서드를 `contextlib` 컨텍스트 안에서만 임시 monkeypatch 해
A1~A10 규칙 수정을 적용한 뒤 앱 `parse()` 를 호출하고, 끝나면 원상 복원한다.

- 전역 오염 없음: 모든 패치는 `_apply_overlay()` 컨텍스트 매니저 안에서만 살아있고 finally 로
  복원된다. 결정적(랜덤/시각 미사용).
- `parse_patched(sentence, gz, settings, pins={})` 는 앱 `parse` 와 **동일한 반환 형식**.
- `build_overlay_gazetteer(inventory, settings)` 는 A1(모드 동의어)을 gazetteer 인스턴스에
  얹은 확장 gazetteer 를 만든다(모듈 전역은 건드리지 않음).

각 수정 지점에 A번호 주석. 실측 회귀 0 (자체검증은 파일 하단 참고 / notes).
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
from typing import Optional

# --- 앱 import 루트 배선 (읽기 전용) --------------------------------------
_APP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from backend.nl import parser as P  # noqa: E402  (앱 파서 모듈 — 읽기 전용, 런타임 패치만)
from backend.nl.gazetteer import Gazetteer  # noqa: E402

try:  # 패키지/스크립트 양쪽 실행 지원
    from . import normalize90 as _n90  # noqa: E402  (§3 버킷1 표면형 정규화)
except ImportError:  # pragma: no cover
    import normalize90 as _n90  # noqa: E402


# ===========================================================================
# A6 — 조명 접미사: DEVICE_CONCEPTS 오버레이
#   무드등→{light,label:무드등}, 메인등→{light,label:메인등}, 접미사 "등"→light.
#   label 이름매칭 보너스(resolve_concept +0.05 / '메인' +0.03)로 정확 엔티티 선택
#   ("거실 무드등"·"거실 메인등" 구분). "[방]등"(거실등/안방등)은 방 접두 area + "등" 개념.
# ===========================================================================
_A6_CONCEPTS: dict[str, dict] = {
    "무드등": {"domain": "light", "label": "무드등"},
    "메인등": {"domain": "light", "label": "메인등"},
    "등": {"domain": "light", "label": "조명"},
}

# ===========================================================================
# A1 — 모드 동의어: 취침 모드/취침모드/취침 → 정규명 "슬립 모드"
#   gazetteer 인스턴스의 mode_surfaces/mode_canonical 에만 얹는다(모듈 전역 미변경).
# ===========================================================================
_A1_MODE_SYNONYMS: dict[str, list[str]] = {
    # A1 + 라운드2: 취침/수면 시점 표현을 슬립 모드로. '잘 때/수면 모드' 등은 무경계
    # 문두 트리거로도 쓰인다(_split_daily_no_boundary_B).
    "슬립 모드": ["취침 모드", "취침모드", "취침",
               "잘 때", "잠잘 때", "잘때", "수면 모드", "수면모드", "잠들 때"],
}

# B1 사람 동의어(아내/부인/집사람 → settings.persons 의 '와이프' 매핑).
_B1_PERSON_MAP: dict[str, str] = {"아내": "와이프", "부인": "와이프", "집사람": "와이프"}


def build_overlay_gazetteer(inventory: dict, settings: dict) -> Gazetteer:
    """A1 모드 동의어 + B1 사람 동의어를 얹은 확장 gazetteer(인스턴스 오버레이)."""
    gz = Gazetteer.build(inventory, settings)
    for canon, syns in _A1_MODE_SYNONYMS.items():
        spec = gz.mode_surfaces.get(canon) or gz.mode_surfaces.get(canon.replace(" ", ""))
        if spec is None:
            continue  # 이 인벤토리에 해당 모드가 없으면 동의어도 추가하지 않음
        for surf in syns:
            gz.mode_surfaces.setdefault(surf, spec)          # A1
            gz.mode_canonical.setdefault(surf, canon)        # A1
    # B1: 아내/부인 → 와이프(person entity). settings.persons 에 대상 표면형이 있을 때만.
    for syn, base in _B1_PERSON_MAP.items():
        pid = gz.person_surfaces.get(base)
        if pid:
            gz.person_surfaces.setdefault(syn, pid)
    return gz


# ===========================================================================
# A4 — 지속시간: _duration_frames 의 p1 정규식 `동안` → `(?:동안|간)` ("5분간")
#   원본 함수를 그대로 복제하되 p1 만 수정한다(모듈 전역 함수 패치).
# ===========================================================================
def _duration_frames_A4(text: str):
    frames = []

    def repl(subj, loc, neg, seconds):
        idx = len(frames)
        frames.append({"subj": subj, "loc": loc, "neg": neg, "seconds": seconds,
                       "boundary": True})
        return f" \x00F{idx}\x00 "

    p2 = re.compile(
        r"(?P<loc>[가-힣]+(?:에서|에는|에|은|는)\s+)?(?P<subj>[가-힣A-Za-z]+)\s*(?:이|가)?\s*"
        r"(?P<neg>없|있)는\s*상태\s*(?:로|가)?\s*(?P<n>\d+)\s*(?P<u>초|분|시간)\s*"
        r"(?:동안\s*)?(?:이|가)?\s*(?:되면|지나면|유지되면|지\s*나면)")
    # A4: '동안' → '(?:동안|간)' 로 확장해 "5분간 …" 도 지속시간 프레임으로 인식.
    p1 = re.compile(
        r"(?P<locpre>[가-힣]+(?:에서|에는|에)\s+)?"
        r"(?P<n>\d+)\s*(?P<u>초|분|시간)\s*(?:동안|간)\s*(?P<loc>[가-힣]+(?:에서|에는|에)\s+)?"
        r"(?P<subj>[가-힣A-Za-z]+)\s*(?:이|가)?\s*(?P<neg>없|있)으?면")

    unit = {"초": 1, "분": 60, "시간": 3600}

    def _sub(m):
        gd = m.groupdict()
        secs = int(gd["n"]) * unit[gd["u"]]
        loc = (gd.get("loc") or gd.get("locpre") or "").strip()
        return repl(gd["subj"].strip(), loc, gd["neg"] == "없", secs)

    text = p2.sub(_sub, text)
    text = p1.sub(_sub, text)
    return text, frames


# ===========================================================================
# A2 — 모드 극성 트리거: _detect_mode 에서 켜지 체크 앞에
#   `해제|취소|종료|풀리|풀려|해지` → trigger off ("취침모드가 해제되면").
# ===========================================================================
def _detect_mode_A2(self, clause: str):
    best = None
    best_len = 0
    best_end = 0
    for surf in self.gz.mode_surfaces:
        idx = clause.find(surf)
        if idx >= 0 and len(surf) > best_len:
            best, best_len, best_end = surf, len(surf), idx + len(surf)
    if not best:
        return None
    name = self.gz.mode_canonical.get(best, best)
    tail = clause[best_end:]
    # A2: 모드 해제 계열 표현은 off 트리거(켜지/꺼지 체크보다 앞).
    if re.search(r"해제|취소|종료|풀리|풀려|해지", tail):
        return ("trigger", name, "off")
    if "꺼지" in tail:
        return ("trigger", name, "off")
    if "켜지" in tail:
        return ("trigger", name, "on")
    if "아니" in tail:
        return ("condition", name, "off")
    return ("condition", name, "on")


# ===========================================================================
# A8 — 세그먼트 오승격 차단: _emit_time_aspect 가 세그먼트를 트리거로 올리는 조건을
#   "세그먼트 단어 + 되면/전환" 으로 한정. "저녁에 … 감지되면" 의 오승격(감지'되면'에
#   반응)을 막고 time_segment 조건으로 방출.
# ===========================================================================
def _emit_time_aspect_A8(self, clause: str):
    clk = P.find_clock(clause)
    if clk:
        after = re.search(r"이후|부터|넘|지나", clause)
        before = re.search(r"이전|까지|전에", clause)
        is_daily = ("매일" in self.sentence or re.search(r"되면", clause)) \
            and not after and not before
        if is_daily:
            self._build_daily_trigger(clk)
        else:
            self._build_time_condition(clause)
        return
    for seg in P.SEGMENT_WORDS:
        if seg in clause:
            # A8: 전환 표현(되면/시작/넘어가/바뀌/전환)이 세그먼트 단어 바로 뒤에 붙을 때만
            # 트리거로 승격. 다른 서술어의 '되면'(예: 감지되면)에는 반응하지 않는다.
            if re.search(re.escape(seg) + r"(?:이|가)?\s*(?:되면|되\b|시작하|넘어가|바뀌|전환)",
                         clause):
                self._build_segment_trigger(clause)
            else:
                self._build_segment_condition(clause)
            return


# ===========================================================================
# A3 / A9 / A10 — _build_action 복제 + 스코프/모드 off 보강.
#   A9: 모드 액션 분기에 취소|풀|해지|종료 → set_mode off.
#   A3: 선두 스코프어 "모든/집의 모든" 뿐 아니라 "전부/다" 도 전량 스코프.
#   A10: 기기어 없는 전량("다 꺼/전부 꺼") → 조명(light) 전체 off/on.
# ===========================================================================
_SCOPE_RE = re.compile(r"\s*(집의\s+모든|모든|전부|다)(?=\s)")


def _build_action_A3910(self, clause: str):
    # 모드 전환("슬립 모드로 바꿔/켜/꺼")
    for name, spec in self.gz.mode_surfaces.items():
        if name in clause:
            canon = self.gz.mode_canonical.get(name, name)
            # A9: off 표현에 취소|풀|해지|종료 추가.
            to = "off" if re.search(r"꺼|끄|해제|취소|풀|해지|종료|off", clause) else "on"
            is_v3 = isinstance(spec, dict) and any(
                k in spec for k in ("initial", "on_action", "off_action"))
            if is_v3:
                self.actions.append({"type": "set_mode", "mode": canon, "to": to})
                label = f"{canon} 켜기" if to == "on" else f"{canon} 끄기"
            else:
                node = {"type": "service", "action": spec.get("action")}
                if spec.get("target"):
                    node["target"] = spec["target"]
                if spec.get("data"):
                    node["data"] = spec["data"]
                self.actions.append(node)
                label = name
            self.chips.append(P._Chip(name, "action",
                                      f"actions[{len(self.actions)-1}]",
                                      [{"id": f"mode:{canon}", "label": label,
                                        "sublabel": "모드 전환", "score": 1.0}]))
            return

    # 지연: "N(초|분|시간) (뒤|후|있다가|이따가)에" → delay 액션(원본 동일).
    dm = re.search(r"(\d+)\s*(초|분|시간)\s*(?:뒤|후|있다가|이따가?)\s*에?", clause)
    if dm:
        unit = {"초": 1, "분": 60, "시간": 3600}
        secs = int(dm.group(1)) * unit[dm.group(2)]
        self.actions.append({"type": "delay", "duration": P.to_duration_obj(secs)})
        self.chips.append(P._Chip(dm.group(0).strip(), "value",
                                  f"actions[{len(self.actions)-1}].duration",
                                  [{"id": "delay", "label": dm.group(0).strip(),
                                    "sublabel": "지연 후 실행", "score": 1.0}],
                                  self._span_of(dm.group(0).strip())))
        clause = (clause[:dm.start()] + " " + clause[dm.end():]).strip()

    # 명령 판정 (B6: 잠가/잠그/차단/소등 → off. lock 도메인은 _domain_service 에서 별도 처리.)
    turn_off = bool(re.search(r"꺼|끄|멈춰|정지|닫아|잠가|잠그|잠궈|차단|소등", clause))
    # B1: 잠금 풀어/해제 → unlock(=turn_on). '풀리'(모드 트리거)와 구분해 액션 존만.
    turn_on = bool(re.search(r"켜|틀|열어|가동|작동|실행|바꿔|풀어|풀고|해제|점등|밝게", clause)) \
        and not turn_off

    # B/스코프 확장: '전체/온 집/집 안/집 전체/모두 … 다' 전량 스코프(모든 외 표현).
    # 배제 스코프(빼고/제외/남기고)는 위에서 이미 처리했으므로 제외.
    if (turn_on or turn_off) \
            and re.search(r"전체|전부|온\s*집|집\s*안|집안|집\s*전체|모두|온집", clause) \
            and not re.search(r"빼|제외|남기", clause):
        concept = P._find_concept(clause) or {"domain": "light", "label": "조명"}
        ids = self.gz.entities_by_concept(concept)
        if ids:
            action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
            self.actions.append({"type": "service", "action": action,
                                 "target": {"entity_id": ids}})
            self.chips.append(P._Chip(
                "전체", "action", f"actions[{len(self.actions)-1}].target",
                [{"id": f"all:{concept['domain']}",
                  "label": f"모든 {concept.get('label','')}", "sublabel": f"{len(ids)}개",
                  "score": 0.9}]))
            return

    # B5 배제 스코프: "X 빼고/제외하고/(만) 남기고/말고 (나머지) 다" → 해당 도메인 전체 중
    # 방 X 를 제외한 off/on. concept 미검출 시 조명 전체로 가정.
    em = re.search(r"([가-힣]+)\s*(?:빼고|제외하고|남기고|말고)", clause)
    if em and (turn_on or turn_off):
        area_word = em.group(1)
        if area_word.endswith("만"):
            area_word = area_word[:-1]
        except_area = P._find_area(self.gz, area_word)
        concept = P._find_concept(clause) or {"domain": "light", "label": "조명"}
        if except_area:
            ids = self.gz.entities_by_concept(concept, except_area_id=except_area)
            action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
            self.actions.append({"type": "service", "action": action,
                                 "target": {"entity_id": ids}})
            self.chips.append(P._Chip(em.group(0).strip(), "action",
                                      f"actions[{len(self.actions)-1}].target",
                                      [{"id": f"except:{concept['domain']}",
                                        "label": f"{area_word} 제외 {concept.get('label','')}",
                                        "sublabel": f"{len(ids)}개", "score": 0.9}]))
            return

    # A3/A10: 선두 스코프어(모든/집의 모든/전부/다) → 전량 스코프. 기기어가 있으면 그
    # 도메인 전체, 없으면 조명 전체(A10). "다른/다시" 는 _SCOPE_RE 의 (?=\s) 로 배제.
    sm = _SCOPE_RE.match(clause)
    if sm and (turn_on or turn_off):
        concept = P._find_concept(clause)
        if concept is None:
            concept = {"domain": "light", "label": "조명"}  # A10
        ids = self.gz.entities_by_concept(concept)
        action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
        self.actions.append({"type": "service", "action": action,
                             "target": {"entity_id": ids}})
        self.chips.append(P._Chip(sm.group(0).strip() or "전체", "action",
                                  f"actions[{len(self.actions)-1}].target",
                                  [{"id": f"all:{concept['domain']}",
                                    "label": f"모든 {concept.get('label','')}",
                                    "sublabel": f"{len(ids)}개", "score": 0.9}]))
        return

    # ---- 이하 원본 _build_action 로직 그대로 ----
    # B2: 값 해석 확장(절반/최대/약하게/단위없는 N으로). find_percent 대체.
    pct = _value_dict(clause)
    preset = None
    pm = re.search(r"([가-힣A-Za-z0-9]+)\s*(?:으로|로)\s*(?:틀|켜|바꿔|설정)", clause)
    if pm:
        cand = pm.group(1)
        if not P.find_percent(cand) and cand not in ("그것", "거기"):
            preset = cand

    targets, unresolved_targets = self._split_targets(clause)
    if not targets and unresolved_targets:
        for u in unresolved_targets:
            self._emit_unresolved_target(u)
        return
    if not targets and self.default_target:
        targets = [self.default_target.get("text", "")]
    if not targets and self.inh_action_entity and (turn_on or turn_off):
        self._emit_inherited_action(clause, turn_on, turn_off, pct)
        return

    made = False
    for t in targets:
        self._emit_service(t, clause, turn_on, turn_off, pct, preset)
        made = True
    if not made:
        self.unmatched.append(clause.strip())


# ===========================================================================
# B그룹 — 기능 규칙 오버레이 확장 (SPEC-ACCURACY 라운드2, 앱 미수정)
# ===========================================================================

# B1 zone: 귀가/외출 표현. person 미검출 시 기본 사용자로 zone enter/leave 트리거.
_ZONE_ENTER_RE = re.compile(
    r"귀가|퇴근|집에?\s*(?:오|와|돌아|들어)|집에?\s*도착|도착하|들어오|왔")
_ZONE_LEAVE_RE = re.compile(
    r"외출|나가|집\s*비우|집을?\s*비우|나서|집을?\s*나\b")
# B6 누수/침수(gas/누수 개념) 이벤트
_LEAK_RE = re.compile(r"누수|물\s*새|물이\s*새|샘|침수")


def _value_pct(clause: str):
    """B2: 밝기/세기 값 해석. %/절반/최대/약하게/단위없는 'N으로' → 0~100 정수. 없으면 None."""
    p = P.find_percent(clause)
    if p:
        return p["value"]
    if re.search(r"절반|반쯤|반만|반으로|반\s*밝기|반\s*정도", clause):
        return 50
    if re.search(r"최대|제일\s*밝|가장\s*밝|풀\s*파워|풀파워|풀\s*밝|최고\s*밝|환하게", clause):
        return 100
    if re.search(r"최소|제일\s*어둡|가장\s*어둡|아주\s*약|은은|살짝|약하게|희미", clause):
        return 20
    m = re.search(r"(?<!\d)(\d{1,3})\s*(?:으로|로)\s*(?:켜|해|설정|맞춰|낮춰|올려|줄여)", clause)
    if m:
        v = int(m.group(1))
        if not re.search(str(v) + r"\s*(?:도|시|분|초|시간)", clause):
            return v
    return None


def _value_dict(clause: str):
    v = _value_pct(clause)
    return {"value": v} if v is not None else None


# ---------------------------------------------------------------------------
# B1 zone / B6 누수 — 이벤트 라우팅 오버레이
# ---------------------------------------------------------------------------
def _clause_is_event_B(self, clause: str) -> bool:
    if _ZONE_ENTER_RE.search(clause) or _ZONE_LEAVE_RE.search(clause):
        return True
    if _LEAK_RE.search(clause):
        return True
    if any(k in clause for k in P.EVENT_KEYWORDS):
        return True
    if re.search(r"움직임|모션|인기척|동작|움직이", clause):
        return True
    if "사람" in clause and re.search(r"있|없|감지|오", clause):
        return True
    return False


def _build_event_clause_B(self, clause: str, as_trigger: bool):
    # B1: 외출(leave) 우선 → 귀가(enter) → 누수 → 원본(도착/모션/상태).
    if _ZONE_LEAVE_RE.search(clause):
        self._build_zone_B(clause, "leave", as_trigger)
        return
    if _ZONE_ENTER_RE.search(clause):
        self._build_zone_B(clause, "enter", as_trigger)
        return
    if _LEAK_RE.search(clause):
        self._build_leak_B(clause, as_trigger)
        return
    if re.search(r"움직임|모션|인기척|동작|사람|움직이", clause):
        self._build_motion(clause, as_trigger)
        return
    self._build_state_event(clause, as_trigger)


def _build_zone_B(self, clause: str, event: str, as_trigger: bool):
    persons = P._find_persons(self.gz, clause)
    if not persons:
        pid = (self.settings.get("persons") or {}).get("나") or "person.user"
        persons = [("나", pid)]
    if as_trigger:
        for surf, pid in persons:
            self.triggers.append({"type": "zone", "entity_id": pid,
                                  "zone": "zone.home", "event": event})
            self.chips.append(P._Chip(
                surf, "trigger", f"triggers[{len(self.triggers)-1}].entity_id",
                [{"id": pid, "label": surf,
                  "sublabel": "집 도착" if event == "enter" else "외출", "score": 1.0}]))
    else:
        state = (self.settings.get("near_home") or {}).get("zone_state", "home")
        for surf, pid in persons:
            self._person_state_condition(surf, pid, state)


def _build_leak_B(self, clause: str, as_trigger: bool):
    concept = {"domain": "binary_sensor", "device_class": "moisture", "label": "누수"}
    area = P._find_area(self.gz, clause) or self.default_area
    cands = self.gz.resolve_concept(concept, area, clause)
    slot = (f"triggers[{len(self.triggers)}].entity_id" if as_trigger
            else f"conditions[{len(self.conditions)}].entity_id")
    chip = self._chip("누수", "trigger" if as_trigger else "condition", slot, cands)
    if as_trigger:
        self.triggers.append({"type": "state", "entity_id": chip.chosen, "to": "on"})
    else:
        self.conditions.append({"type": "state", "entity_id": chip.chosen, "state": "on"})


# ---------------------------------------------------------------------------
# B3/B2 — 도메인 서비스 오버레이(climate set_temperature · cover position · media volume)
# ---------------------------------------------------------------------------
def _domain_service_B(self, domain, eid, turn_on, turn_off, pct, preset, clause):
    data = {}
    # B1/B6: '문 잠가/잠금 풀어' 처럼 문(도어센서)에 잠금 동사가 오면 같은 방의 lock 으로 재매핑.
    if domain == "binary_sensor" and re.search(r"잠그|잠가|잠궈|잠금|풀어|풀고", clause):
        e = self.gz.entity(eid)
        area = e.get("area_id") if e else None
        locks = [x["entity_id"] for x in self.gz.entities
                 if x["domain"] == "lock" and (area is None or x.get("area_id") == area)]
        if locks:
            act = "lock.unlock" if turn_on else "lock.lock"
            self.actions.append({"type": "service", "action": act,
                                 "target": {"entity_id": [locks[0]]}})
            return
    if domain == "light":
        action = "light.turn_off" if turn_off else "light.turn_on"
        if pct and not turn_off:
            data["brightness_pct"] = pct["value"]
    elif domain == "fan":
        # B2: 값이 있고 켜/끄 명령이 아니면 set_percentage(최대로 돌려 → 100).
        if pct is not None and not turn_on and not turn_off:
            self.actions.append({"type": "service", "action": "fan.set_percentage",
                                 "target": {"entity_id": [eid]},
                                 "data": {"percentage": pct["value"]}})
            return
        action = "fan.turn_off" if turn_off else "fan.turn_on"
        if pct and not turn_off:
            data["percentage"] = pct["value"]
    elif domain == "climate":
        # B3: "N도로 맞춰/설정/해" → set_temperature (켜/끄 명령이 아닐 때).
        tm = P.find_temperature(clause)
        if tm is not None and not turn_off and not turn_on \
                and re.search(r"맞춰|맞추|맞게|설정|해줘|해\b|로\s*해", clause):
            self.actions.append({"type": "service", "action": "climate.set_temperature",
                                 "target": {"entity_id": [eid]},
                                 "data": {"temperature": int(tm["value"])}})
            return
        action = "climate.turn_off" if turn_off else "climate.turn_on"
    elif domain == "cover":
        # B2: 값이 있으면 위치 설정(절반만 열어 → position 50).
        if pct is not None:
            self.actions.append({"type": "service", "action": "cover.set_cover_position",
                                 "target": {"entity_id": [eid]},
                                 "data": {"position": pct["value"]}})
            return
        close_v = re.search(r"내려|닫|쳐|접", clause)
        open_v = re.search(r"올려|열|걷|펼|젖", clause)
        if open_v and not close_v:
            action = "cover.open_cover"
        elif close_v and not open_v:
            action = "cover.close_cover"
        else:
            action = "cover.close_cover" if turn_off else "cover.open_cover"
    elif domain == "media_player":
        # B7: 볼륨 값이 있고 켜/끄 명령이 아니면 volume_set(20% → 0.2).
        if pct is not None and not turn_on and not turn_off:
            self.actions.append({"type": "service", "action": "media_player.volume_set",
                                 "target": {"entity_id": [eid]},
                                 "data": {"volume_level": round(pct["value"] / 100.0, 2)}})
            return
        action = "media_player.turn_off" if turn_off else "media_player.turn_on"
    elif domain == "lock":
        action = "lock.unlock" if turn_on else "lock.lock"
    elif domain == "switch":
        action = "switch.turn_off" if turn_off else "switch.turn_on"
    else:
        action = f"{domain}.turn_{'off' if turn_off else 'on'}"
    node = {"type": "service", "action": action, "target": {"entity_id": [eid]}}
    if data:
        node["data"] = data
    self.actions.append(node)
    if domain == "climate" and preset:
        self.actions.append({"type": "service", "action": "climate.set_fan_mode",
                             "target": {"entity_id": [eid]},
                             "data": {"fan_mode": preset}})
        self.chips.append(P._Chip(preset, "value",
                                  f"actions[{len(self.actions)-1}].data.fan_mode",
                                  [{"id": preset, "label": preset, "sublabel": "팬 모드",
                                    "score": 0.8}]))


# ---------------------------------------------------------------------------
# B4/모드 트리거 — 경계 검출/시각·시간대 측면 오버레이
# ---------------------------------------------------------------------------
def _detect_mode_B(self, clause: str):
    """A2 확장 — 모드 표면형의 극성. '되면/들어가/문두 면' → trigger on,
    '일 때/에서/이면서' → condition (다른 이벤트와 병존하는 모드 조건)."""
    best = None
    best_len = 0
    best_end = 0
    for surf in self.gz.mode_surfaces:
        idx = clause.find(surf)
        if idx >= 0 and len(surf) > best_len:
            best, best_len, best_end = surf, len(surf), idx + len(surf)
    if not best:
        return None
    name = self.gz.mode_canonical.get(best, best)
    tail = clause[best_end:]
    # 조건 표지(일 때/에서/이면서/인 경우/중) → 모드 조건
    if re.search(r"일\s*때|일때|에서|이면서|인\s*경우|인경우|중이|중일", tail):
        return ("condition", name, "off" if "아니" in tail else "on")
    if re.search(r"해제|취소|종료|풀리|풀려|해지", tail):
        return ("trigger", name, "off")
    if "꺼지" in tail:
        return ("trigger", name, "off")
    if "켜지" in tail:
        return ("trigger", name, "on")
    if "아니" in tail:
        return ("condition", name, "off")
    # 되면/들어가/진입/시작 또는 문두 모드+면 → 모드 트리거(on)
    return ("trigger", name, "on")


def _emit_time_aspect_B(self, clause: str):
    """A8 확장 — 시각 범위(A시부터 B시까지)·markerless 시각의 daily 트리거·
    단독 시간대의 트리거 승격."""
    # 자정/정오(시 없는 표현) → daily 트리거(00:00 / 12:00).
    if not P.find_clock(clause):
        if "자정" in clause:
            self._build_daily_trigger({"hhmm": "00:00", "text": "자정", "span": (0, 2)})
            return
        if "정오" in clause:
            self._build_daily_trigger({"hhmm": "12:00", "text": "정오", "span": (0, 2)})
            return
    # B4: 시각 범위 → time 조건(after A & before B)
    rng = _time_range(clause)
    if rng:
        self.conditions.append(rng)
        self.chips.append(P._Chip(
            "시간범위", "condition", f"conditions[{len(self.conditions)-1}]",
            [{"id": "time_range", "label": f"{rng['after'][:5]}~{rng['before'][:5]}",
              "sublabel": "시간 범위", "score": 1.0}]))
        return
    clk = P.find_clock(clause)
    if clk:
        after = re.search(r"이후|부터|넘|지나", clause)
        before = re.search(r"이전|까지|전에", clause)
        # markerless 시각(밤 11시엔/아침 7시에)은 daily 트리거로. 범위표지 있으면 time 조건.
        is_daily = (not after and not before)
        if is_daily:
            self._build_daily_trigger(clk)
        else:
            self._build_time_condition(clause)
        return
    for seg in P.SEGMENT_WORDS:
        if seg in clause:
            transition = re.search(
                re.escape(seg) + r"(?:이|가)?\s*(?:되면|되\b|시작하|넘어가|바뀌|전환)", clause)
            has_event = bool(re.search(
                r"움직|모션|인기척|동작|감지|열리|닫히|도착|왔|들어|사람|누수|귀가|외출",
                clause)) or bool(P.find_clock(clause))
            if transition or not has_event:
                self._build_segment_trigger(clause)
            else:
                self._build_segment_condition(clause)
            return


def _time_range(clause: str):
    """'A시 부터 B시 까지/사이' → {type:time, after, before}. 시각 2개 + 범위표지."""
    if not re.search(r"부터", clause) or not re.search(r"까지|사이", clause):
        return None
    clks = []
    pos = 0
    while True:
        c = P.find_clock(clause[pos:])
        if not c:
            break
        clks.append(c["hhmm"])
        pos += c["span"][1]
    if len(clks) >= 2:
        return {"type": "time", "after": clks[0] + ":00", "before": clks[1] + ":00"}
    return None


def _split_daily_no_boundary_B(self, text: str):
    """A3.4 확장 — '면' 경계가 없을 때 antecedent 를 분리:
    (1) markerless 시각(밤 11시엔) → 시각 트리거, (2) 문두 모드-시점(잘 때) → 모드 트리거,
    (3) 문두 시간대(저녁엔) → 시간대 트리거."""
    clk = P.find_clock(text)
    if clk and not re.search(r"이후|이전|부터|까지|전에|사이", text):
        ce = clk["span"][1]
        cons = re.sub(r"^\s*(?:에는|엔|에|정각)\s*", "", text[ce:]).strip()
        return text[:ce].strip(), cons
    # 자정/정오(시 없는) 문두 → 시각 antecedent
    m0 = re.match(r"\s*(자정|정오)(?:에는|엔|에)?\s+", text)
    if m0:
        return text[:m0.end()].strip(), text[m0.end():].strip()
    for surf in sorted(self.gz.mode_surfaces, key=len, reverse=True):
        i = text.find(surf)
        if i >= 0 and text[:i].strip() == "":
            e = i + len(surf)
            return text[:e].strip(), text[e:].strip()
    m = re.match(r"\s*(?:주말|평일|공휴일|휴일|주중)?\s*(새벽|아침|낮|저녁|밤)"
                 r"(?:에는|엔|에|이)?\s+", text)
    if m:
        e = m.end()
        return text[:e].strip(), text[e:].strip()
    return "", text


# B6 — 가스밸브/누수 기기어 개념(이름 매칭 보너스로 정확 엔티티 선택).
_B6_CONCEPTS: dict[str, dict] = {
    "가스밸브": {"domain": "switch", "label": "가스밸브"},
    "가스": {"domain": "switch", "label": "가스밸브"},
    "밸브": {"domain": "switch", "label": "가스밸브"},
    "대기전력": {"domain": "switch", "label": "대기전력 콘센트"},
    "콘센트": {"domain": "switch", "label": "대기전력 콘센트"},
    # F2: 환기장치/선풍기 팬 동의어(이름 매칭 보너스로 정확 엔티티).
    "환기장치": {"domain": "fan", "label": "전열교환기"},
    "선풍기": {"domain": "fan", "label": "선풍기"},
    # B7: 영문 TV 표기(티비/텔레비전 외).
    "TV": {"domain": "media_player", "label": "TV"},
    "tv": {"domain": "media_player", "label": "TV"},
}


def _emit_numeric_aspect_B(self, clause, as_trigger=False):
    """F1 — 수치 비교 측면. 개념어(온도/습도)가 없어도 'N도' 온도 리터럴이 있으면
    온도 센서로 추론해 numeric_state 를 방출한다('거실이 28도 넘으면')."""
    if not re.search(r"이상|이하|초과|미만|넘|올라가|내려가|떨어지|밑|높아|낮아|높|낮", clause):
        return
    if P._find_concept(clause) is None and P.find_temperature(clause) is None:
        return
    self._build_numeric(clause, as_trigger=as_trigger)


def _build_numeric_B(self, clause, as_trigger=False):
    """B/E5 — numeric_state. 영하/마이너스 음수 처리 + 트리거 방을 default_area 로 전파."""
    concept = P._find_concept(clause)
    area = P._find_area(self.gz, clause) or self.default_area
    temp = P.find_temperature(clause)
    num = None
    if temp:
        num = temp["value"]
    else:
        m = re.search(r"(\d+(?:\.\d+)?)", clause)
        if m:
            num = float(m.group(1))
    if num is not None and re.search(r"영하|마이너스", clause):
        num = -abs(num)
    above = below = None
    if re.search(r"이상|초과|넘|올라가|높", clause):
        above = num
    elif re.search(r"이하|미만|떨어지|낮|내려가|밑", clause):
        below = num
    else:
        above = num
    if concept is None:
        concept = {"domain": "sensor", "device_class": "temperature"} if temp else None
    # E5: 트리거 방을 기본 방으로 전파(뒤 액션 대상 해석 힌트) — 미설정일 때만.
    if area and self.default_area is None:
        self.default_area = area
    cands = self.gz.resolve_concept(concept, area, clause) if concept else []
    bucket = self.triggers if as_trigger else self.conditions
    slot = f"{'triggers' if as_trigger else 'conditions'}[{len(bucket)}].entity_id"
    chip = self._chip(concept.get("label", "센서") if concept else clause,
                      "trigger" if as_trigger else "condition", slot, cands)
    node = {"type": "numeric_state", "entity_id": chip.chosen}
    if above is not None:
        node["above"] = above
    if below is not None:
        node["below"] = below
    bucket.append(node)


# ===========================================================================
# 오버레이 적용/복원 (전역 오염 없음, 결정적)
# ===========================================================================
@contextlib.contextmanager
def _apply_overlay():
    saved: dict = {}
    # --- 모듈 전역 ---
    saved["DEVICE_CONCEPTS"] = P.DEVICE_CONCEPTS
    merged = dict(P.DEVICE_CONCEPTS)
    merged.update(_A6_CONCEPTS)                              # A6
    merged.update(_B6_CONCEPTS)                              # B6 (가스밸브/콘센트)
    P.DEVICE_CONCEPTS = merged

    saved["VERB_STEMS"] = P.VERB_STEMS
    # A5 + B1/B6 절 경계 어간(움직이면/나서면/비우면/새면).
    P.VERB_STEMS = list(P.VERB_STEMS) + ["잡히", "움직이", "나서", "비우", "새"]

    saved["DAY_TYPE_WORDS"] = P.DAY_TYPE_WORDS
    dtw = dict(P.DAY_TYPE_WORDS)
    dtw["주중"] = "weekday"                                  # B4
    P.DAY_TYPE_WORDS = dtw

    # B1/B6: '잠그고/차단하고' 등 액션 연결어미가 절 경계로 인식되도록 명령 힌트 확장.
    saved["COMMAND_HINTS"] = P.COMMAND_HINTS
    P.COMMAND_HINTS = list(P.COMMAND_HINTS) + ["잠그", "잠가", "차단", "풀어", "소등", "점등"]

    saved["_duration_frames"] = P._duration_frames
    P._duration_frames = _duration_frames_A4                # A4

    # --- _Parser 메서드 ---
    saved["_detect_mode"] = P._Parser._detect_mode
    P._Parser._detect_mode = _detect_mode_B                 # A2 + 모드 트리거 확장

    saved["_emit_time_aspect"] = P._Parser._emit_time_aspect
    P._Parser._emit_time_aspect = _emit_time_aspect_B       # A8 + B4(범위/daily/시간대)

    saved["_build_action"] = P._Parser._build_action
    P._Parser._build_action = _build_action_A3910           # A3/A9/A10 + B2/B5/B6

    saved["_domain_service"] = P._Parser._domain_service
    P._Parser._domain_service = _domain_service_B           # B2/B3/B7

    saved["_build_numeric"] = P._Parser._build_numeric
    P._Parser._build_numeric = _build_numeric_B             # E5 + 음수

    saved["_emit_numeric_aspect"] = P._Parser._emit_numeric_aspect
    P._Parser._emit_numeric_aspect = _emit_numeric_aspect_B  # F1 온도 리터럴 추론

    saved["_clause_is_event"] = P._Parser._clause_is_event
    P._Parser._clause_is_event = _clause_is_event_B         # B1/B6

    saved["_build_event_clause"] = P._Parser._build_event_clause
    P._Parser._build_event_clause = _build_event_clause_B   # B1/B6

    saved["_split_daily_no_boundary"] = P._Parser._split_daily_no_boundary
    P._Parser._split_daily_no_boundary = _split_daily_no_boundary_B  # B4/모드/시간대

    # B1/B6 신규 메서드(원본에 없던 것) 바인딩.
    P._Parser._build_zone_B = _build_zone_B
    P._Parser._build_leak_B = _build_leak_B
    try:
        yield
    finally:
        P.DEVICE_CONCEPTS = saved["DEVICE_CONCEPTS"]
        P.VERB_STEMS = saved["VERB_STEMS"]
        P.DAY_TYPE_WORDS = saved["DAY_TYPE_WORDS"]
        P.COMMAND_HINTS = saved["COMMAND_HINTS"]
        P._duration_frames = saved["_duration_frames"]
        P._Parser._emit_numeric_aspect = saved["_emit_numeric_aspect"]
        P._Parser._detect_mode = saved["_detect_mode"]
        P._Parser._emit_time_aspect = saved["_emit_time_aspect"]
        P._Parser._build_action = saved["_build_action"]
        P._Parser._domain_service = saved["_domain_service"]
        P._Parser._build_numeric = saved["_build_numeric"]
        P._Parser._clause_is_event = saved["_clause_is_event"]
        P._Parser._build_event_clause = saved["_build_event_clause"]
        P._Parser._split_daily_no_boundary = saved["_split_daily_no_boundary"]
        for attr in ("_build_zone_B", "_build_leak_B"):
            if hasattr(P._Parser, attr):
                delattr(P._Parser, attr)


def parse_patched(sentence: str, gz: Gazetteer, settings: dict,
                  pins: Optional[dict] = None) -> dict:
    """A그룹 오버레이를 임시 적용해 앱 parse 를 호출하고 복원. 반환 형식은 앱 parse 동일.

    A1(모드 동의어)은 gz 인스턴스에 의존하므로, 동의어를 쓰려면 gz 를
    ``build_overlay_gazetteer(inventory, settings)`` 로 만들어 넘긴다.

    진입부에서 §3 버킷1 표면형 정규화(normalize90)를 먼저 적용해 비표준 표면(한글 수사·
    절경계 어미·존칭·완화어·후치 조건)을 정규형으로 바꾼 뒤 앱 parse 에 넘긴다(순수 additive).
    """
    with _apply_overlay():
        normalized = _n90.normalize_surface(sentence, gz=gz)
        return P.parse(normalized, gz, settings, pins or {})
