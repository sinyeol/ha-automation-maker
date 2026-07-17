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
# P2-금지문(prohibition) 오독 방어 — SPEC-ACCURACY-90 §3 버킷6 / SPEC-SCHEMA-90 §4.4
#   "-지 마/-지 말/못 -게/안 -게 해/-면 안 돼" 같은 금지문은 **자동화 생성 요청이 아니다**.
#   정반대 액션(가스밸브 열기 등) 생성이 최악의 안전사고이므로, 파서를 태우기 전에
#   금지문을 감지해 **모델을 만들지 않고**(ok=False, subrules=[]) 미해결로 반환한다.
#   판정은 인용부(따옴표) 안의 금지 표현은 무시하고(예: 알림 메시지 '…하지 마세요'),
#   normalize 로 완화어(좀)를 제거한 표면형에서 본다. 결정적.
# ===========================================================================
_QUOTE_SPAN_RE = re.compile(r"['\"“”‘’『』「」][^'\"“”‘’『』「」]*['\"“”‘’『』「」]")
_PROHIBIT_RE = re.compile(
    r"지도?\s*(?:좀\s*)?마(?![가-힣])"                 # -지 마 / -지도 마 (마세요·마루 등 제외)
    r"|지도?\s*(?:좀\s*)?마\s*라"                       # -지 마라
    r"|지도?\s*(?:좀\s*)?말(?=[아라자고지것세]|\s|[,.!?]|$)"  # -지 말아/말고/말라/말지/말자/말 것
    r"|못\s*[가-힣]+게"                                  # 못 -게 해 (못 켜게)
    r"|안\s*[가-힣]+게\s*(?:좀\s*)?(?:해|하|만들)"       # 안 -게 해 (안 켜지게 해)
    r"|(?:으면|면)\s*(?:절대\s*)?안\s*(?:돼|되|된다)")   # -면 (절대) 안 돼


def _is_prohibition(normalized: str) -> bool:
    """normalize 를 거친 표면형이 금지문(요청 아님)인가. 인용부 안 표현은 무시."""
    if not normalized:
        return False
    dequoted = _QUOTE_SPAN_RE.sub(" ", normalized)
    return bool(_PROHIBIT_RE.search(dequoted))


def _prohibition_result(sentence: str) -> dict:
    """금지문 감지 시 반환하는 미해결 결과(정반대 액션 절대 미생성). 앱 parse 반환형과 호환."""
    return {"ok": False,
            "model": {"alias": sentence.strip(), "description": "",
                      "mode": "single", "subrules": []},
            "chips": [], "summary": "",
            "area_id": None, "category": None,
            "unmatched": [sentence.strip()], "confidence": 0.0,
            "warnings": ["금지문(자동화 생성 요청 아님)으로 판단해 동작을 만들지 않았습니다."]}


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


# ===========================================================================
# P2-상(aspect) — SPEC-SCHEMA-90 §4.1: 결과상('-어 있-')/지속상은 **조건**, 전이형은 트리거.
#   "켜져 있으면/열려 있으면/꺼져 있으면/닫혀 있을 때/-ㄴ 상태" = 상태(조건, 승격 규칙 적용),
#   "켜지면/열리면/감지되면/풀리면/되면" = 전이(트리거). 어간 부분매칭('켜져'⊃'켜')이 상태형을
#   트리거로 오독하던 것을, 결과상 어미를 감지해 조건으로 강등하고(트리거 없으면 첫 절만 승격),
#   도메인별 상태값(cover=open/closed, lock=locked/unlocked, 그 외 on/off)을 정확히 방출한다.
# ===========================================================================
# 결과상 '-어/아/여 있-'(열려/켜져/꺼져/닫혀/잠겨/풀려 …있) + '-ㄴ 상태'. 이 절은 상태(조건).
_RESULTATIVE_RE = re.compile(
    r"(?:켜져|꺼져|열려|닫혀|잠겨|풀려|채워|비워|걸려|놓여|담겨|덮여|서|앉아|누워|들어와)\s*있"
    r"|(?:켜진|꺼진|열린|닫힌|잠긴|풀린|열려있는|닫혀있는)\s*상태")
# 지속상 조건 표지(-ㄹ/-을/-일 때, -는/-은 동안). 액션존에서도 앞부분을 조건으로 뗀다(§4.1).
_DURATIVE_RE = re.compile(r"(?:을|ㄹ|일|는)\s*때|(?:는|은|인)\s*동안")
# 잠금 해제 전이('풀리면/풀려') — EVENT_KEYWORDS 에 없어 트리거로 안 잡히던 것을 이벤트로 인정.
_UNLOCK_RE = re.compile(r"풀리|풀려|풀린")


def _is_state_aspect(clause: str) -> bool:
    """절이 결과상/지속상(상태) 절인가 → 트리거가 아니라 조건(승격 규칙 대상)."""
    return bool(_RESULTATIVE_RE.search(clause) or _DURATIVE_RE.search(clause))


def _is_myeon_boundary_B(word: str) -> bool:
    """원본 _is_myeon_boundary + 후행 구두점 허용('높으면,' 처럼 쉼표가 붙어도 절 경계).

    VERB_STEMS(오버레이 확장 포함)를 참조하므로 '높/낮' 레벨 형용사도 경계로 인식한다.
    쉼표만 허용한다(마침표·말줄임표는 문장 흐름을 바꿀 수 있어 제외 — 회귀 방지).
    """
    w = word.rstrip(",")
    if not w.endswith("면"):
        return False
    body = w[:-1]
    if body.endswith("으"):
        body = body[:-1]
    for stem in P.VERB_STEMS:
        if body.endswith(stem):
            return True
    if body.endswith("이"):  # copula (…이면 / …사이면 / …이상이면)
        return True
    return False


def _aspect_state(clause: str, eid: Optional[str]) -> str:
    """결과상 절 + 해석된 엔티티 도메인 → 정확한 상태 문자열.
    극성: 풀리(해제)=양성, 꺼/닫/잠/없=음성, 켜/열/있=양성. '안/못' 부정은 극성 반전(XOR).
    도메인별: cover=open/closed, lock=unlocked/locked, 그 외=on/off.
    """
    neg = bool(re.search(r"(?:^|\s)안\s+[가-힣]", clause)) or bool(re.search(r"(?:^|\s)못\s", clause))
    if _UNLOCK_RE.search(clause):
        base_pos = True                     # 잠금 '풀림' = 해제 = 양성
    elif re.search(r"꺼|닫|잠|없", clause):
        base_pos = False                    # 꺼짐/닫힘/잠김/없음 = 음성
    else:
        base_pos = True                     # 켜짐/열림/있음 = 양성(기본)
    positive = base_pos != neg              # XOR
    dom = eid.split(".")[0] if eid else None
    if dom == "cover":
        return "open" if positive else "closed"
    if dom == "lock":
        return "unlocked" if positive else "locked"
    return "on" if positive else "off"


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
    # P2: 결과상('켜져 있으면')·잠금해제('풀리면')도 상태 이벤트 후보로 인정('켜져'≠'켜지'라
    # EVENT_KEYWORDS 로는 안 잡히던 것). aspect 라우팅이 트리거/조건을 최종 결정한다.
    if _RESULTATIVE_RE.search(clause) or _UNLOCK_RE.search(clause):
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


def _build_state_event_B(self, clause, as_trigger):
    """P2: 도메인·부정·결과상 극성을 반영한 상태 이벤트(원본 _build_state_event 대체).

    - 상태값을 `_aspect_state` 로 계산: cover=open/closed, lock=unlocked/locked, 그 외 on/off.
    - '안/못' 부정과 결과상(꺼져/닫혀/잠겨)·잠금해제(풀리)를 정확히 반영('안 켜져 있으면'=off,
      '커튼 열리면'=open, '잠금 풀리면'=unlocked).
    """
    role = "trigger" if as_trigger else "condition"
    slot = (f"triggers[{len(self.triggers)}].entity_id" if as_trigger
            else f"conditions[{len(self.conditions)}].entity_id")
    hit = self._find_entity_in_clause(clause)
    if hit:
        surf, ids = hit
        cands = [self.gz._cand(self.gz.entity(i), 0.9, "이름 일치") for i in ids]
        chip = self._chip(surf, role, slot, cands, self._span_of(surf))
    else:
        concept = P._find_concept(clause)
        area = P._find_area(self.gz, clause) or self.default_area
        cands = self.gz.resolve_concept(concept, area, clause) if concept else []
        chip = self._chip(clause.strip(), role, slot, cands)
    eid = chip.chosen
    # P2: 문(도어센서)에 잠금/해제 표현이 붙으면 같은 방의 lock 으로 재매핑('현관문 잠금 풀리면'
    # → lock.entrance_door). 명시된 switch/밸브는 대상이 아니므로 binary_sensor 일 때만.
    if eid and eid.split(".")[0] == "binary_sensor" \
            and (_UNLOCK_RE.search(clause)
                 or re.search(r"잠그|잠가|잠궈|잠금|잠기|잠겨|도어\s*락", clause)):
        e = self.gz.entity(eid)
        area = e.get("area_id") if e else None
        locks = [x["entity_id"] for x in self.gz.entities
                 if x["domain"] == "lock" and (area is None or x.get("area_id") == area)]
        if locks:
            eid = locks[0]
    st = _aspect_state(clause, eid)
    if as_trigger:
        self.triggers.append({"type": "state", "entity_id": eid, "to": st})
    else:
        self.conditions.append({"type": "state", "entity_id": eid, "state": st})
    # P2: 제어 가능한 상태 대상은 뒤따르는 대상-생략 액션이 물려받게 한다('가스밸브가 안 잠겨
    # 있으면 잠가'·'불 안 켜져 있을 때만 켜줘' → 같은 대상에 잠금/점등). 명시 대상이 있으면 무효.
    if eid and eid.split(".")[0] in (
            "light", "switch", "fan", "media_player", "climate", "cover", "lock"):
        self.inh_action_entity = eid


# P2 지속상 조건이 액션존 앞에 붙는 경우('… 감지되는 동안엔 조명 켜'/'… 닫혀 있을 때만 켜').
#   면 경계가 없어 액션존에 남은 선행 지속상 절을 조건으로 떼어낸다(§4.1).
_DURATIVE_SPLIT_RE = re.compile(
    r"^(?P<cond>.*?(?:(?:을|ㄹ|일|는)\s*때(?:만|는|엔|에)?"
    r"|(?:는|은|인)\s*동안(?:엔|은|에)?))\s+(?P<act>.+)$")


def _split_durative_condition(clause: str):
    """액션 절이 '지속상 조건 + 액션'이면 (조건부, 액션부)로 분리. 아니면 (None, clause)."""
    m = _DURATIVE_SPLIT_RE.match(clause)
    if not m:
        return None, clause
    cond = m.group("cond").strip()
    act = m.group("act").strip()
    # 가드: 조건부에 상태/수치/모션 신호가 있고, 액션부에 실제 명령 동사가 있을 때만 분리.
    if not (_RESULTATIVE_RE.search(cond) or _between(cond)
            or re.search(r"감지|모션|움직|인기척|사람|높|낮|사이", cond)):
        return None, clause
    if not any(h in act for h in P.COMMAND_HINTS):
        return None, clause
    return cond, act


def _route_condition_segment_B(self, seg: str):
    """지속상 조건부 세그먼트를 수치/모션/상태 조건으로 방출(as_trigger=False).

    세그먼트에 방이 안 적혀 있으면 트리거 엔티티의 방을 임시 기본값으로 써 해석한다
    ('욕실 환풍기 켜지면 … 모션 감지되는 동안엔' → 욕실 모션). 처리 후 원상 복원.
    """
    saved_area = self.default_area
    if self.default_area is None and self.triggers:
        teid = self.triggers[0].get("entity_id")
        if isinstance(teid, str):
            te = self.gz.entity(teid)
            if te and te.get("area_id"):
                self.default_area = te["area_id"]
    try:
        if _between(seg) or re.search(r"이상|이하|초과|미만|넘|높아|낮아|높|낮", seg):
            self._emit_numeric_aspect(seg, as_trigger=False)
        elif re.search(r"움직임|모션|인기척|동작|감지|사람|움직이", seg):
            self._build_event_clause(seg, False)
        else:
            self._build_state_event(seg, False)
    finally:
        self.default_area = saved_area


def _process_consequent_B(self, clauses):
    for clause in clauses:
        seg, rest = _split_durative_condition(clause)
        # 지속상 조건은 트리거가 이미 있을 때만 떼어낸다(순수 명령문 보호).
        if seg is not None and self.triggers:
            self._route_condition_segment_B(seg)
            if rest.strip():
                self._build_action(rest)
        else:
            self._build_action(clause)


def _process_antecedent_B(self, clauses, frames):
    """P2 상(aspect) 라우팅 — 원본 _process_antecedent 대체.

    결과상/지속상 절은 전이 절과 분리해 **조건**으로 배치하고, 트리거가 하나도 없을 때만
    첫 상태 절을 트리거로 승격한다(§4.1). 전이 이벤트/시각/모드/수치 트리거는 원본과 동일.
    """
    held = [c for c in clauses if P._SENTINEL_RE.fullmatch(c.strip())]
    other = [c for c in clauses if not P._SENTINEL_RE.fullmatch(c.strip())]

    for c in held:
        self._build_held(c, frames)

    modes = [self._detect_mode(c) for c in other]
    event_clauses = [c for c, mi in zip(other, modes)
                     if self._clause_is_event(c) and not (mi and mi[0] == "trigger")]
    # 상태상(결과상/지속상) 절 vs 전이 절. 전이 절만 '주 트리거' 후보다.
    state_events = [c for c in event_clauses if _is_state_aspect(c)]
    trans_events = [c for c in event_clauses if c not in state_events]

    has_primary = bool(held) or bool(trans_events) \
        or any(mi and mi[0] == "trigger" for mi in modes)
    boundary_clause = other[-1] if other else None

    for c, mi in zip(other, modes):
        self._emit_calendar_aspect(c)
        self._emit_time_aspect(c)
        if mi and mi[0] == "condition":
            self._emit_mode_condition(mi[1], mi[2], c)
        promote = (c is boundary_clause) and not has_primary and not self.triggers
        self._emit_numeric_aspect(c, as_trigger=promote)

    for c, mi in zip(other, modes):
        if mi and mi[0] == "trigger":
            self._emit_mode_trigger(mi[1], mi[2], c)

    # 전이 이벤트: held 있으면 전부 조건, 없으면 마지막이 트리거(원본 의미 유지).
    for i, c in enumerate(trans_events):
        as_trigger = (not held) and (i == len(trans_events) - 1)
        self._build_event_clause(c, as_trigger)

    # 상태상 절: 기본 조건. 트리거가 하나도 없으면 첫 상태 절만 진입에지 트리거로 승격(§4.1).
    for i, c in enumerate(state_events):
        promote = (not held) and (not self.triggers) and (i == 0)
        self._build_event_clause(c, promote)


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


# P2 수치 between('A(도)에서 B(도) 사이') → above=min, below=max (SPEC §4.2, 경계 미포함).
_BETWEEN_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*도?\s*(?:에서|부터)\s*(-?\d+(?:\.\d+)?)\s*도?\s*(?:사이|까지)")


def _between(clause: str):
    m = _BETWEEN_RE.search(clause)
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    return (min(a, b), max(a, b))


def _emit_numeric_aspect_B(self, clause, as_trigger=False):
    """F1 + P2 — 수치 비교/레벨/between 측면. 개념어(온도/습도)가 없어도 'N도' 온도 리터럴이
    있으면 온도 센서로 추론한다. '높으면/낮으면/사이면'(레벨/between)도 방출한다(§4.2)."""
    if not (re.search(r"이상|이하|초과|미만|넘|올라가|내려가|떨어지|밑|높아|낮아|높|낮", clause)
            or _between(clause)):
        return
    if P._find_concept(clause) is None and P.find_temperature(clause) is None:
        return
    self._build_numeric(clause, as_trigger=as_trigger)


def _build_numeric_B(self, clause, as_trigger=False):
    """B/E5 + P2 — numeric_state. 음수 처리 · between(사이) 결합 · 방 전파."""
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
    btw = _between(clause)
    if btw:
        above, below = btw            # P2: '20도에서 24도 사이' → above 20 + below 24
    elif re.search(r"이상|초과|넘|올라가|높", clause):
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
# Phase 3a — 시간·달력 신규 노드 (SPEC-SCHEMA-90 §1·§2). 후처리 오버레이.
#   앱 parse 결과(model)에 sun/time_pattern 트리거·sun_window/weekday/day_of_month/
#   interval_anchor 조건·repeat/notify 액션을 결정적으로 얹는다. 신규 노드의 부수 필드
#   (offset/minutes/days/negate/interval/anchor/repeat 내부)는 structural_match 비교
#   대상이 아니므로(핵심필드만 비교) 노드 종류·핵심필드만 정확히 맞추면 exact 가 된다.
#   단일 서브룰(area single)에만 적용 — 다중 서브룰 문장은 대상 축이 아니라 건너뛴다.
# ===========================================================================
# 일몰/일출 표면형(§1.1). '해...지'(완전히 지 포함)·어두워지·노을·땅거미=sunset,
#   일출·동트/동틀·여명·해 뜨/해뜨=sunrise. '해제/해줘'는 '해'+비'지' 라 미매치(안전).
_SUNSET_RE = re.compile(r"일몰|어두워지|어두워진|캄캄|노을|땅거미|해\s*가?\s*(?:완전히\s*)?지")
_SUNRISE_RE = re.compile(r"일출|동\s*트|동\s*틀|여명|날\s*이?\s*밝|해\s*가?\s*뜨|해\s*뜰|해뜨")
# 밤창(해 진 뒤~해 뜰 때) 명시 표현. 맨 '밤/새벽'(단독 세그먼트)은 제외 — 기존 gold 가
#   time_segment 를 쓰는 문장('새벽에 …움직이면')과 충돌하므로 특정 표현만 sun_window 로.
_NIGHTWIN_RE = re.compile(r"밤사이|밤새|한밤|밤중|해\s*진\s*뒤|해\s*지고\s*난|어두운\s*동안|어두울\s*때")

_DAY_MAP = {"월": "mon", "화": "tue", "수": "wed", "목": "thu",
            "금": "fri", "토": "sat", "일": "sun"}
_WEEKDAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# SPEC §2.4 라벨 규약: 문장에 기준일 없으면 anchor = 라벨 작성일(2026-07-17, 금)이 속한
#   주의 월요일 = 2026-07-13. Date 계산 금지 → 고정 상수.
_INTERVAL_ANCHOR = "2026-07-13"


def _sun_offset(text: str) -> int:
    """§1.1 offset(초). 'N분/시간/초' 합산, '시간 반'=+1800, '전'=음수·그 외 양수."""
    secs = 0
    for m in re.finditer(r"(\d+)\s*(시간|분|초)", text):
        secs += int(m.group(1)) * {"시간": 3600, "분": 60, "초": 1}[m.group(2)]
    if "반" in text and "시간" in text:
        secs += 1800
    if secs == 0:
        return 0
    # 음수는 'N분/시간 전'(이전) 만 — '완전히' 등에 든 '전' 오탐 방지.
    return -secs if re.search(r"(?:분|시간|초)\s*(?:전|이전)", text) else secs


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
    """요일 집합(days, negate) 또는 (None, False)(§2.2).

    **부정(빼고/말고/제외)·개별 요일·요일 축약(월수금)만** weekday 노드로 방출한다.
    맨 '평일/주말/주중'(긍정)은 기존 gold 가 day_type 노드를 쓰므로 건드리지 않는다
    (동일 표면형의 라벨 규약이 데이터셋마다 달라 회귀 방지 — bare positive 는 day_type 유지).
    """
    neg = re.search(
        r"(주말|평일|주중|[월화수목금토일]요일|[월화수목금토일]{2,})"
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


_NATIVE_CNT = r"(?:\d+|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|여러)"


def _is_repeat_action(text: str) -> bool:
    """'N번/차례 깜빡·반짝·반복·보내'(count) / '…때까지 계속'(until) → repeat 액션(§3.4).

    수량사는 아라비아 숫자·토박이수(두 차례 등 normalize 미변환분) 모두 허용.
    """
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


def _detect_notify(sent: str):
    """인용부(따옴표) 메시지 + 알림 동사 → notify data. 채널(폰/스피커) 반영(§3.3·§4.5).

    인용 내부 원문은 정규화하지 않은 **원문 sentence** 에서 그대로 뜬다(따옴표만 제거).
    """
    qm = re.search(r"['\"“”‘’『』「」]"
                   r"([^'\"“”‘’『』「」]+)"
                   r"['\"“”‘’『』「」]", sent)
    if not qm:
        return None
    if not re.search(r"알려|말해|말하|보내|방송|안내|얘기|알림|전해", sent):
        return None
    data = {"message": qm.group(1).strip()}
    if re.search(r"폰|휴대폰|핸드폰|모바일|스마트폰", sent):
        data["target"] = "mobile"
    elif re.search(r"스피커|방송", sent):
        data["target"] = "speaker"
    return data


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


def _augment_time_calendar(sentence: str, normalized: str, result: dict) -> dict:
    """Phase 3a 후처리 — sun/time_pattern/sun_window/weekday/day_of_month/interval_anchor/
    repeat/notify 노드를 결정적으로 얹는다. result(model) 를 제자리 수정."""
    model = result.get("model") or {}
    sub = _primary_subrule(model)
    if sub is None:
        return result
    sub.setdefault("triggers", [])
    sub.setdefault("conditions", [])
    sub.setdefault("actions", [])

    is_repeat = _is_repeat_action(normalized)

    # --- 조건: 요일(§2.2). 개별 요일/축약/부정은 항상 weekday. 맨 평일/주말(긍정)은
    #     이벤트(상태/수치/존) 트리거가 있을 때만 weekday 로 승격(daily/segment 문맥의
    #     기존 day_type gold 는 보존 — 동일 표면형 라벨 규약이 데이터셋마다 달라 회귀 방지). ---
    wd, wneg, wbare = _detect_weekdays(normalized)
    if wd:
        edge = any(t.get("type") in ("state", "numeric_state", "zone",
                                     "state_held", "group_held")
                   for t in sub["triggers"])
        if not (wbare and not edge):
            sub["conditions"] = [c for c in sub["conditions"]
                                 if c.get("type") != "day_type"]
            if not any(c.get("type") == "weekday" for c in sub["conditions"]):
                sub["conditions"].append({"type": "weekday", "days": wd, "negate": wneg})

    # --- 조건: 매달 N일/말일(§2.3) ---
    dom = _detect_day_of_month(normalized)
    if dom is not None and not any(c.get("type") == "day_of_month" for c in sub["conditions"]):
        sub["conditions"].append({"type": "day_of_month", "days": dom})

    # --- 조건: 격주/N주기(§2.4) ---
    iv = _detect_interval(normalized)
    if iv is not None and not any(c.get("type") == "interval_anchor" for c in sub["conditions"]):
        sub["conditions"].append({"type": "interval_anchor", "unit": "week",
                                  "interval": iv, "anchor": _INTERVAL_ANCHOR})

    # --- 트리거: sun / sun_window(§1.1·§2.1) ---
    trigs = sub["triggers"]
    conds = sub["conditions"]
    sun_evt = "sunrise" if _SUNRISE_RE.search(normalized) else (
        "sunset" if _SUNSET_RE.search(normalized) else None)
    nightwin = bool(_NIGHTWIN_RE.search(normalized))
    # 실제 이벤트(상태/수치/존)가 트리거 또는 조건에 있으면 그것이 주 트리거, 밤창=조건.
    real_event = any(n.get("type") in ("state", "numeric_state", "zone",
                                       "state_held", "group_held")
                     for n in list(trigs) + list(conds))
    if (sun_evt or nightwin) and real_event:
        # 밤/어두운 창 = sun_window 조건. 밤/새벽 세그먼트(트리거·조건) 제거 후,
        # 트리거가 비면 상태/수치 조건을 진입에지 트리거로 승격(§2.1 sun_window items).
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
    elif sun_evt and not trigs:
        node = {"type": "sun", "event": sun_evt}
        off = _sun_offset(normalized)
        if off:
            node["offset"] = off
        sub["triggers"].append(node)
        # 오프셋 표현('N분 뒤/N시간 지나면')이 만든 spurious delay 액션·time 조건 제거
        # (sun 오프셋이 곧 그 시간차 — gold sun 아이템은 delay/time 없음).
        sub["actions"] = [a for a in sub["actions"] if a.get("type") != "delay"]
        sub["conditions"] = [c for c in sub["conditions"] if c.get("type") != "time"]

    # --- 트리거: time_pattern(§1.2) — repeat 케이던스(간격/마다)와 구분해 repeat 아닐 때만.
    #     기존 상태/수치 트리거는 조건으로 강등(패턴이 주 트리거). ---
    if not is_repeat:
        tp = _detect_time_pattern(normalized)
        if tp and not any(t.get("type") == "time_pattern" for t in sub["triggers"]):
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

    # --- 트리거 보강: 달력 조건은 있는데 트리거가 비면(예 '월요일부터 금요일까지'가 시각
    #     파싱을 깨뜨린 경우) 벽시계 → daily 트리거로 살린다(달력 문맥은 daily 가 규약). ---
    if not sub["triggers"] and any(
            c.get("type") in ("weekday", "day_of_month", "interval_anchor")
            for c in sub["conditions"]):
        clk = P.find_clock(normalized)
        if clk:
            sub["triggers"].append({"type": "daily", "at": clk["hhmm"]})

    # --- 액션: repeat(§3.4) 우선, 아니면 인용 notify(§3.3). ---
    if is_repeat:
        # 트리거가 비었는데(어순 문제) 상태/수치 조건이 있으면 진입에지로 승격.
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
        sub["actions"] = [{"type": "repeat"}]
        sub["conditions"] = [c for c in sub["conditions"]
                             if c.get("type") in ("weekday", "day_of_month",
                                                  "interval_anchor", "sun_window")]
    else:
        nd = _detect_notify(sentence)
        if nd is not None:
            sub["actions"] = [{"type": "service", "action": "notify.notify", "data": nd}]
    return result


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
    # P2: 수치 레벨 형용사 '높/낮'(높으면/낮으면)을 절 경계로 인정 → 조건/트리거로 방출(§4.2).
    P.VERB_STEMS = list(P.VERB_STEMS) + ["잡히", "움직이", "나서", "비우", "새", "높", "낮"]

    saved["DAY_TYPE_WORDS"] = P.DAY_TYPE_WORDS
    dtw = dict(P.DAY_TYPE_WORDS)
    dtw["주중"] = "weekday"                                  # B4
    P.DAY_TYPE_WORDS = dtw

    # B1/B6: '잠그고/차단하고' 등 액션 연결어미가 절 경계로 인식되도록 명령 힌트 확장.
    saved["COMMAND_HINTS"] = P.COMMAND_HINTS
    P.COMMAND_HINTS = list(P.COMMAND_HINTS) + ["잠그", "잠가", "차단", "풀어", "소등", "점등"]

    saved["_duration_frames"] = P._duration_frames
    P._duration_frames = _duration_frames_A4                # A4

    saved["_is_myeon_boundary"] = P._is_myeon_boundary
    P._is_myeon_boundary = _is_myeon_boundary_B             # P2 후행 구두점 허용 절 경계

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
    P._Parser._clause_is_event = _clause_is_event_B         # B1/B6 + P2 결과상/잠금해제

    saved["_build_event_clause"] = P._Parser._build_event_clause
    P._Parser._build_event_clause = _build_event_clause_B   # B1/B6

    saved["_build_state_event"] = P._Parser._build_state_event
    P._Parser._build_state_event = _build_state_event_B     # P2 도메인·부정·결과상 상태

    saved["_process_antecedent"] = P._Parser._process_antecedent
    P._Parser._process_antecedent = _process_antecedent_B   # P2 aspect 트리거/조건 라우팅

    saved["_process_consequent"] = P._Parser._process_consequent
    P._Parser._process_consequent = _process_consequent_B   # P2 지속상 조건 분리
    P._Parser._route_condition_segment_B = _route_condition_segment_B

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
        P._is_myeon_boundary = saved["_is_myeon_boundary"]
        P._Parser._emit_numeric_aspect = saved["_emit_numeric_aspect"]
        P._Parser._detect_mode = saved["_detect_mode"]
        P._Parser._emit_time_aspect = saved["_emit_time_aspect"]
        P._Parser._build_action = saved["_build_action"]
        P._Parser._domain_service = saved["_domain_service"]
        P._Parser._build_numeric = saved["_build_numeric"]
        P._Parser._clause_is_event = saved["_clause_is_event"]
        P._Parser._build_event_clause = saved["_build_event_clause"]
        P._Parser._build_state_event = saved["_build_state_event"]
        P._Parser._process_antecedent = saved["_process_antecedent"]
        P._Parser._process_consequent = saved["_process_consequent"]
        if hasattr(P._Parser, "_route_condition_segment_B"):
            delattr(P._Parser, "_route_condition_segment_B")
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
        # P2: 금지문 방어 — 정반대 액션 생성이 최악의 안전사고이므로 파서 진입 전에 차단.
        if _is_prohibition(normalized):
            return _prohibition_result(sentence)
        result = P.parse(normalized, gz, settings, pins or {})
    # Phase 3a: 시간·달력 신규 노드 후처리(오버레이 컨텍스트 밖 — 순수 model 가공, 결정적).
    return _augment_time_calendar(sentence, normalized, result)
