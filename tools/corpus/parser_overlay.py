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
    r"|안\s+[가-힣]+게\s*(?:좀\s*)?(?:해|하|만들)"       # 안 -게 해 (안 켜지게 해). 공백 필수(결함3): '안전/편안'의 '안'을 부정부사로 오인 방지(_aspect_state neg 정합)
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
    "무드 조명": {"domain": "light", "label": "무드등"},
    "무드조명": {"domain": "light", "label": "무드등"},
    "메인등": {"domain": "light", "label": "메인등"},
    "메인 조명": {"domain": "light", "label": "메인등"},
    "메인조명": {"domain": "light", "label": "메인등"},
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
    # L1: 주어 생략·주어선행·부정 모션 지속 held. p1(주어후행 없/있)이 못 잡는 부재형
    #   ('5분간 안 움직이면'·'모션이 10분 동안 없으면'·'1시간 넘게 감지 못 되면') → 모션 off 지속.
    #   방(loc) 있으면 그 방, 없으면 _build_held 가 앞 규칙 센서를 재참조한다.
    p3 = re.compile(
        r"(?P<locpre>[가-힣]+(?:에서|에는|에)\s+)?"
        r"(?:[가-힣]+\s*(?:이|가)\s+)?"                     # 선행 주어(모션이/사람이) 소비
        r"(?P<n>\d+)\s*(?P<u>초|분|시간)\s*(?:동안|간|넘게)\s*(?:아무도\s*)?"
        r"(?:안\s*움직이|움직임\s*이?\s*없|인기척\s*이?\s*없|사람\s*이?\s*없"
        r"|감지\s*(?:안|못)\s*되|반응\s*이?\s*없|없)\s*(?:으)?면")

    unit = {"초": 1, "분": 60, "시간": 3600}

    def _sub(m):
        gd = m.groupdict()
        secs = int(gd["n"]) * unit[gd["u"]]
        loc = (gd.get("loc") or gd.get("locpre") or "").strip()
        return repl(gd["subj"].strip(), loc, gd["neg"] == "없", secs)

    def _sub3(m):
        gd = m.groupdict()
        secs = int(gd["n"]) * unit[gd["u"]]
        return repl("움직임", (gd.get("locpre") or "").strip(), True, secs)

    text = p2.sub(_sub, text)
    text = p1.sub(_sub, text)
    text = p3.sub(_sub3, text)
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
    turn_off = bool(re.search(r"꺼|끄|멈춰|정지|닫아|잠가|잠그|잠궈|잠근|차단|소등", clause))
    # B1: 잠금 풀어/해제 → unlock(=turn_on). '풀리'(모드 트리거)와 구분해 액션 존만.
    turn_on = bool(re.search(r"켜|틀|열어|가동|작동|실행|바꿔|풀어|풀고|해제|점등|밝게", clause)) \
        and not turn_off

    # B/스코프 확장: '전체/온 집/집 안/집 전체/모두 … 다' 전량 스코프(모든 외 표현).
    # 배제 스코프(빼고/제외/남기고)는 위에서 이미 처리했으므로 제외.
    if (turn_on or turn_off) \
            and re.search(r"전체|전부(?![가-힣])|온\s*집|집\s*안|집안|집\s*전체|모두|온집", clause) \
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

    # L1 후치 전량 스코프: '[기기] (싹) 다 꺼/켜' → 해당 도메인 전량. 방이 명시되면 그 방으로
    #   한정('거실 조명 다 꺼'→거실 라이트), 아니면 전역('조명 다 꺼'·'불 싹 다 꺼'→집 전체).
    #   도메인 혼재(보일러/AC) 오검출 방지로 라이트(또는 개념 미검출)에만 안전 적용.
    if (turn_on or turn_off) \
            and re.search(r"싹|(?<![가-힣])다\s*(?:꺼|켜|끄|잠|닫|열|멈|내려|올려)", clause) \
            and not re.search(r"빼|제외|남기|말고", clause):
        concept = P._find_concept(clause)
        if concept is None or concept.get("domain") == "light":
            concept = concept or {"domain": "light", "label": "조명"}
            area = P._find_area(self.gz, clause)
            ids = self.gz.entities_by_concept(concept, area_id=area)
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

    # B5 배제 스코프(다중): "A랑 B 빼고/제외하고/(만) 남기고/말고 (나머지) 다" → 해당 도메인
    # 전체에서 배제 방(들)·엔티티(들)를 뺀 명시 목록. 요일 배제("주말 빼고")는 area/엔티티가
    # 아니라 자연히 건너뛴다(weekday 후처리 담당). "나머지 스위치/콘센트"는 switch 도메인.
    _ex_areas: list = []
    _ex_ids: list = []
    for em in re.finditer(
            r"((?:[가-힣]+\s*(?:이랑|랑|하고|과|와|,)\s*)*[가-힣]+)"
            r"\s*(?:만)?\s*(?:빼고|제외하고|남기고|말고)", clause):
        for w in re.split(r"\s*(?:이랑|랑|하고|과|와|,)\s*", em.group(1)):
            w = w.strip().rstrip("만")
            if not w:
                continue
            a = P._find_area(self.gz, w)
            if a:
                if a not in _ex_areas:
                    _ex_areas.append(a)
                continue
            nc = self.gz.resolve_name(P.strip_particles_simple(w))
            if nc and nc[0]["id"] not in _ex_ids:
                _ex_ids.append(nc[0]["id"])
    if (_ex_areas or _ex_ids) and (turn_on or turn_off):
        if re.search(r"스위치|콘센트|전원", clause):
            concept = {"domain": "switch", "label": "스위치"}
        else:
            concept = P._find_concept(clause) or {"domain": "light", "label": "조명"}
        all_ids = self.gz.entities_by_concept(concept)
        ids = [i for i in all_ids
               if (self.gz.entity(i) or {}).get("area_id") not in _ex_areas
               and i not in _ex_ids]
        if ids:
            action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
            self.actions.append({"type": "service", "action": action,
                                 "target": {"entity_id": ids}})
            self.chips.append(P._Chip(
                "제외", "action", f"actions[{len(self.actions)-1}].target",
                [{"id": f"except:{concept['domain']}",
                  "label": f"제외 {concept.get('label','')}", "sublabel": f"{len(ids)}개",
                  "score": 0.9}]))
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
    r"외출|나가|나갈|나갔|집\s*비우|집을?\s*비우|나서|집을?\s*나\b")
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
# L1: 특정 방으로 입실/통과(들어가/지나가/방+이동'가') = 그 방 모션 이벤트.
#   집 도착/외출(zone)·다른 이벤트어(모션/개폐/온습도/누수)와 겹치지 않을 때만.
_ROOM_ENTER_RE = re.compile(r"들어가|지나가|지나쳐")


def _room_enter_motion_area(gz, clause: str):
    """방 입실/통과 트리거 절이면 그 방 area, 아니면 None(그 방 모션 센서로 라우팅용)."""
    area = P._find_area(gz, clause)
    if area is None:
        return None  # 방이 명시돼야 그 방 모션으로 본다(집=zone 은 방 표면형 없음).
    # 명시적 입실/통과 동사(들어가/지나가)는 EVENT_KEYWORDS('나가' 부분매칭) 무시하고 방 모션.
    if _ROOM_ENTER_RE.search(clause):
        return area
    # bare 이동 '가'(방+가면/가서): 집 도착·다른 이벤트/기기어가 전혀 없을 때만.
    if _ZONE_ENTER_RE.search(clause) or _ZONE_LEAVE_RE.search(clause) \
            or _LEAK_RE.search(clause):
        return None
    if re.search(r"움직임|모션|인기척|동작|움직이|누수|밸브|온도|습도|미세먼지"
                 r"|열리|열려|닫히|닫혀|감지|도착|왔|눌리|울리", clause):
        return None
    if re.search(r"(?<![가-힣])가(?:면|서|고)(?![가-힣])", clause):
        return area
    return None


def _clause_is_event_B(self, clause: str) -> bool:
    if _room_enter_motion_area(self.gz, clause) is not None:
        return True
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
    # L1: 방 입실/통과(들어가/지나가/방+가) → 그 방 모션(집 도착 zone 보다 먼저 판정).
    if _room_enter_motion_area(self.gz, clause) is not None:
        self._build_motion(clause, as_trigger)
        return
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
        # L1: 명명 엔티티도 기기개념도 없는 개폐 이벤트의 bare '문/창(문)' → door/window 센서
        #   ('문 열리면'→현관문, '작은방 창 열리면'→작은방 창문). 개폐 어휘가 있을 때만.
        if concept is None and re.search(r"열리|열려|열린|열|닫히|닫혀|닫힌|닫", clause):
            if re.search(r"창문|창(?!고)", clause):
                concept = {"domain": "binary_sensor", "device_class": "window", "label": "창문"}
            elif "문" in clause:
                concept = {"domain": "binary_sensor", "device_class": "door", "label": "문"}
        cands = self.gz.resolve_concept(concept, area, clause) if concept else []
        chip = self._chip(clause.strip(), role, slot, cands)
    eid = chip.chosen
    # §2.1 L1: 개폐 대상 없는 뒤 서브룰 전이('닫히면'/'열리면'/'없어지면')는 앞 서브룰 센서 재참조.
    if eid is None and getattr(self, "inh_trigger_entity", None):
        eid = self.inh_trigger_entity
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
    # L1 방 전파: 상태/이벤트 트리거·조건의 방을 기본 방으로 전파(뒤 액션의 대상-생략 기기어가
    #   같은 방을 상속하게 — 미설정일 때만). 해석된 엔티티의 area 를 우선 사용한다('거실 인기척
    #   있으면 무드 조명 켜' → 거실 무드등).
    if self.default_area is None and eid:
        e2 = self.gz.entity(eid)
        prop_area = (e2.get("area_id") if e2 else None) or P._find_area(self.gz, clause)
        if prop_area:
            self.default_area = prop_area
    if as_trigger:
        self.triggers.append({"type": "state", "entity_id": eid, "to": st})
    else:
        self.conditions.append({"type": "state", "entity_id": eid, "state": st})
    # P2: 제어 가능한 상태 대상은 뒤따르는 대상-생략 액션이 물려받게 한다('가스밸브가 안 잠겨
    # 있으면 잠가'·'불 안 켜져 있을 때만 켜줘' → 같은 대상에 잠금/점등). 명시 대상이 있으면 무효.
    if eid and eid.split(".")[0] in (
            "light", "switch", "fan", "media_player", "climate", "cover", "lock"):
        self.inh_action_entity = eid


# L1: 동사 관형형 '-는'(움직이는/감지되는/열리는 …)은 주제 조사 '는'이 아니다.
#   '욕실에서 사람 움직이는 거 잡히면'에서 '움직이는'을 주제로 오인해 트리거 절을 삼키던 것 방지.
_VERB_ADNOMINAL_RE = re.compile(
    r"(?:움직이|감지되|잡히|열리|닫히|켜지|꺼지|들어오|들어가|나가|지나가|풀리|잠기|울리"
    r"|생기|떨어지|올라가|내려가|오|되|나|가|넘|높아지|낮아지|없어지)는$")


def _extract_topic_B(self, antecedent: str) -> str:
    """원본 _extract_topic + 동사 관형형 '-는' 배제(트리거 절 오삼킴 방지)."""
    tokens = antecedent.split()
    for i in range(min(3, len(tokens))):
        tok = tokens[i]
        if tok.endswith("은") or tok.endswith("는"):
            if _VERB_ADNOMINAL_RE.search(tok):
                continue  # 동사 관형형(주제 아님) → 다음 후보 확인
            topic_txt = " ".join(tokens[: i + 1])
            base = topic_txt[:-1].strip()
            area = P._find_area(self.gz, base)
            concept = P._find_concept(base)
            name_cands = self.gz.resolve_name(P.strip_particles_simple(base))
            if area:
                self.default_area = area
                self.rule_area = area
            if concept:
                self.default_target = {"concept": concept, "text": base, "area": area}
            elif name_cands:
                self.default_target = {"entity": name_cands[0]["id"], "text": base,
                                       "area": area}
            if area or concept or name_cands:
                return " ".join(tokens[i + 1:])
            return antecedent
    return antecedent


def _find_action_boundary_B(self, region):
    """원본 _find_action_boundary + 후행 구두점 허용('끄고,'처럼 쉼표가 붙어도 액션 경계).
    다중 서브룰에서 '…끄고, 100을 넘으면 켜줘'의 조건 토큰(100을)이 앞 액션존에 흡수되는 것 방지."""
    if not region:
        return -1
    for j in range(len(region) - 1, -1, -1):
        tok = region[j].rstrip(",.…")
        if not ((tok.endswith("고") or tok.endswith("며"))
                and any(h in tok for h in P.COMMAND_HINTS)):
            continue
        if j > 0 and self._is_subject_token(region[j - 1]):
            continue
        return j
    return len(region) - 1


def _build_motion_B(self, clause, as_trigger):
    """원본 _build_motion + L1 방 전파. 모션 트리거/조건의 방을 기본 방으로 전파해
    뒤따르는 대상-생략 기기어('거실 인기척 있으면 무드 조명 켜')가 같은 방을 상속하게 한다."""
    neg = "없" in clause
    to = "off" if neg else "on"
    area = P._find_area(self.gz, clause) or self.default_area
    if area and self.default_area is None:
        self.default_area = area
    cands = self.gz.resolve_concept(P.MOTION_CONCEPT, area, clause)
    if as_trigger:
        slot = f"triggers[{len(self.triggers)}].entity_id"
        chip = self._chip("움직임", "trigger", slot, cands, self._span_of("움직임"))
        self.triggers.append({"type": "state", "entity_id": chip.chosen, "to": to})
    else:
        slot = f"conditions[{len(self.conditions)}].entity_id"
        chip = self._chip("움직임", "condition", slot, cands, self._span_of("움직임"))
        self.conditions.append({"type": "state", "entity_id": chip.chosen, "state": to})


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

    # L1: 실제 이벤트(모션/상태/held/수치) 트리거가 있으면 문두 상태 모드('잘 때'·'슬립모드
    #   켜져 있고')는 트리거가 아니라 조건이다 → mode 'trigger'를 'condition'으로 강등.
    #   전이형 모드('취침모드 되면')만 단독일 때 트리거로 남는다.
    numeric_present = any(_clause_has_numeric(c) for c in other)
    if bool(held) or bool(event_clauses) or numeric_present:
        modes = [("condition", mi[1], mi[2]) if (mi and mi[0] == "trigger") else mi
                 for mi in modes]

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

    # L1 방 전파(catch-all): 대상-생략 액션이 방을 상속하도록, 트리거/조건에서 해석된 첫
    #   엔티티(방 있는 것)의 방을 기본 방으로 — 미설정일 때만. state_held/numeric/state 등
    #   개별 빌더가 놓친 경로를 일괄 보정한다(person.* 등 방 없는 엔티티는 건너뜀).
    if self.default_area is None:
        for n in list(self.triggers) + list(self.conditions):
            eid = n.get("entity_id")
            if isinstance(eid, str) and "." in eid:
                e = self.gz.entity(eid)
                if e and e.get("area_id"):
                    self.default_area = e["area_id"]
                    break


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
                 r"(?:에는|엔|에|이|마다)?\s+", text)
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


def _is_weather_clause(clause: str) -> bool:
    return bool(_WEATHER_HUMID_RE.search(clause) or _WEATHER_HOT_RE.search(clause)
                or _WEATHER_COLD_RE.search(clause))


# L1: 수치 비교 표층 — 도달/찍/닿(임계 도달=above)도 포함.
_NUM_CMP_RE = r"이상|이하|초과|미만|넘|올라가|내려가|떨어지|밑|높아|낮아|높|낮|도달|닿|찍"


def _clause_has_numeric(clause: str) -> bool:
    """이 절이 numeric_state 트리거/조건을 만들 후보인가(_emit_numeric_aspect_B 게이트와 동일)."""
    weather = _is_weather_clause(clause)
    if not (weather or re.search(_NUM_CMP_RE, clause) or _between(clause)):
        return False
    if not weather and P._find_concept(clause) is None and P.find_temperature(clause) is None:
        return False
    return True


def _emit_numeric_aspect_B(self, clause, as_trigger=False):
    """F1 + P2 + L1 — 수치 비교/레벨/between/날씨형 전이. 개념어(온도/습도)가 없어도 'N도'
    온도 리터럴이나 날씨형 전이(습해지/더워지/추워지)가 있으면 센서로 추론한다(§4.2)."""
    weather = _is_weather_clause(clause)
    if not (weather or re.search(_NUM_CMP_RE, clause) or _between(clause)):
        return
    # 개념/온도 리터럴이 없어도, 앞 서브룰 센서를 물려받을 수 있으면 진행('100을 넘으면 켜줘').
    if not weather and P._find_concept(clause) is None and P.find_temperature(clause) is None \
            and not getattr(self, "inh_trigger_entity", None):
        return
    self._build_numeric(clause, as_trigger=as_trigger)


def _build_numeric_B(self, clause, as_trigger=False):
    """B/E5 + P2 + L1 — numeric_state. 음수·between·날씨형 전이(습해지/더워지/추워지)·방 전파."""
    # L1: 날씨형 전이는 습도/온도 센서 + 관례 임계로(방 전파 포함). 명시값이 있으면 그 값.
    if _is_weather_clause(clause):
        wnode = _weather_numeric(clause, self.gz, self.default_area)
        if wnode is not None:
            e = self.gz.entity(wnode["entity_id"])
            if e and e.get("area_id") and self.default_area is None:
                self.default_area = e["area_id"]      # 방 전파(대상-생략 액션 상속)
            bucket = self.triggers if as_trigger else self.conditions
            slot = f"{'triggers' if as_trigger else 'conditions'}[{len(bucket)}].entity_id"
            lbl = "습도" if wnode["entity_id"].endswith("humidity") else "온도"
            chip = self._chip(lbl, "trigger" if as_trigger else "condition", slot,
                              [self.gz._cand(e, 0.9, lbl)] if e else [])
            node = dict(wnode)
            node["entity_id"] = chip.chosen or wnode["entity_id"]
            bucket.append(node)
            return
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
    eid = chip.chosen
    # §2.1 L1: 개념 없이 수치만 있는 뒤 서브룰('100을 넘으면 켜줘')은 앞 서브룰의 센서를 재참조.
    if eid is None and getattr(self, "inh_trigger_entity", None):
        eid = self.inh_trigger_entity
    node = {"type": "numeric_state", "entity_id": eid}
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
# '해 지'(일몰)는 해가 문두/공백 뒤 독립어일 때만 — '습해지/눅눅해지/피곤해지' 등 '-해지-'
#   합성어의 '해지' 오탐 방지(negative lookbehind). 어두워지/캄캄/노을/땅거미는 그대로.
_SUNSET_RE = re.compile(r"일몰|어두워지|어두워진|캄캄|노을|땅거미|(?<![가-힣])해\s*가?\s*(?:완전히\s*)?지")
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
    # 음수는 'N분/시간 (반) 전/앞'(이전) 만 — '완전히' 등에 든 '전' 오탐 방지.
    # Phase 3b 수정: '한 시간 반 전'(→'1 시간 반 전')처럼 '반'이 사이에 껴도 '전'을 음수로 인식.
    return -secs if re.search(r"(?:분|시간|초)\s*(?:반\s*)?(?:전|이전|앞)", text) else secs


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


# --- repeat 풀구조(§3.4) — 'N번 깜빡/반짝/반복/열었다 닫았다' 를 count 루프로. ---
_REPEAT_NATIVE_CNT = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6,
                      "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
_REPEAT_CNT_ARABIC_RE = re.compile(r"(\d+)\s*(?:번|차례|회)")
_REPEAT_CNT_NATIVE_RE = re.compile(
    r"(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:번|차례|회)")


def _detect_repeat_count(text: str):
    """'N번/차례/회' → 반복 횟수(정수). 아라비아·토박이수 모두. 없으면 None(§3.4)."""
    m = _REPEAT_CNT_ARABIC_RE.search(text)
    if m:
        return int(m.group(1))
    m2 = _REPEAT_CNT_NATIVE_RE.search(text)
    if m2:
        return _REPEAT_NATIVE_CNT[m2.group(1)]
    return None


def _repeat_on_off_services(action: str):
    """액션 서비스명 → (on-서비스, off-서비스). cover 는 open/close, 그 외 turn_on/off."""
    domain = (action or "").split(".")[0] or "homeassistant"
    if domain == "cover":
        return "cover.open_cover", "cover.close_cover"
    return domain + ".turn_on", domain + ".turn_off"


def _build_count_repeat(sub: dict, normalized: str):
    """'N번 깜빡/반짝/열었다 닫았다' → {type:repeat, kind:count, count:N, sequence:[on,
    delay{s:1}, off, delay{s:1}]}. 파서가 이미 해석한 실질 액션(대상 엔티티+도메인)을
    on/off 로 전개한다. 횟수·대상 확정 불가 시 None(→ 기존 bare repeat 보존)."""
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
    """'X 열려 있으면 … 닫힐 때까지 계속 깜빡' → {type:repeat, kind:until,
    conditions:[state X 반전], sequence:[on, delay{s:1}, off, delay{s:1}]}.
    단일 state 트리거 + 전개 가능한 점멸 액션이 있을 때만(보수적). 아니면 None."""
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
    until_cond = {"type": "state", "entity_id": trigs[0]["entity_id"],
                  "state": inv_state}
    seq = [
        {"type": "service", "action": on_svc, "target": target},
        {"type": "delay", "duration": {"seconds": 1}},
        {"type": "service", "action": off_svc, "target": target},
        {"type": "delay", "duration": {"seconds": 1}},
    ]
    return {"type": "repeat", "kind": "until", "conditions": [until_cond],
            "sequence": seq}


# 알림 동사(§3.3·§4.5). '물어/여쭤'(질문형 알림) 포함.
_NOTIFY_VERB_RE = re.compile(
    r"알려|말해|말하|말씀|보내|방송|안내|얘기|알림|전해|전달|물어|여쭤|공지")


def _detect_notify(sent: str):
    """인용 메시지 + 알림 동사 → notify data. 인용부(따옴표) 우선, 없으면 -다고/-라고 경계.

    인용 내부 원문은 정규화하지 않은 **원문 sentence** 에서 그대로 뜬다(따옴표만 제거).
    채널(폰/스피커)은 '말고/대신' 부정을 반영한다(§4.5: "폰 말고 스피커로"→speaker).
    """
    if not _NOTIFY_VERB_RE.search(sent):
        return None
    qm = re.search(r"['\"“”‘’『』「」]"
                   r"([^'\"“”‘’『』「」]+)"
                   r"['\"“”‘’『』「」]", sent)
    if qm:
        message = qm.group(1).strip()
    else:
        # 따옴표 없음: 인용 표지 -다고/-라고/-냐고/-자고 앞 서술어를 메시지로(§4.5).
        #   좌경계는 공백(연속 한글 서술어)까지 — '덥다고'→'덥다', '출근했다고'→'출근했다'.
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


# ===========================================================================
# Phase 3b — 프레즌스 양화 presence_agg (SPEC-SCHEMA-90 §1.3 트리거 / §2.5 조건).
#   집(zone.home) 단위 인원 양화. 방 단위 모션('욕실 아무도 없으면')과 구분하려고
#   '집' 문맥·귀가/외출·그룹표현이 있을 때만 발화한다(방+모션은 건드리지 않음 — 회귀 방지).
# ===========================================================================
# 귀가(도착) 표지. presence 는 집 카운트 에지이므로 존/개인 도착과 별개(그룹 양화 문맥에서만).
_PRES_ARRIVE_RE = re.compile(r"들어오|들어와|들어온|들어가|귀가|재실|도착|오면|와\s*있|집에\s*있|왔")


def _presence_info(text: str):
    """프레즌스 개념 판정 → {concept: empty|some|all, first?} 또는 None.

    강한 집-프레즌스 신호(집 문맥·그룹 나감/귀가·'아무도 없다가...들어오' 전이)일 때만.
    """
    has_home = "집" in text
    prior_then_arrive = bool(re.search(r"다가", text)) and bool(_PRES_ARRIVE_RE.search(text))
    first_marker = bool(re.search(r"처음|먼저|있게\s*되|첫\s*사람", text))
    # 집이 빔(모두 나감/아무도 없음). '하나도'는 normalize 로 '1도'가 되기도 한다.
    empty = False
    if re.search(r"아무도|(?:1도|하나도)?\s*안\s*남|사람이?\s*(?:1도|하나도)?\s*없", text) and has_home:
        empty = True
    if re.search(r"(?:다들|다|모두|전원|가족들?|가족이)\s*[가-힣]{0,3}?(?:나가|외출)", text):
        empty = True
    if re.search(r"집\s*(?:을|이|안)?\s*비[우어운는면]", text):
        empty = True
    all_word = re.search(
        r"모두|둘\s*다|전원|온\s*가족|제일\s*늦게|가족이?\s*다|다\s*들어[오와온]|다\s*집에\s*있", text)
    some_word = re.search(r"누구|누가|한\s*명|아무나", text)
    has_arrive = bool(_PRES_ARRIVE_RE.search(text))
    home_ctx = has_home or bool(re.search(
        r"귀가|가족|전원|온\s*식구|제일\s*늦게\s*들어|다\s*들어[오와온]", text)) or prior_then_arrive
    if (prior_then_arrive or (some_word and first_marker)) and (home_ctx or empty):
        return {"concept": "some", "first": True}
    if empty and not prior_then_arrive:
        return {"concept": "empty"}
    if all_word and has_arrive and home_ctx:
        return {"concept": "all"}
    if some_word and home_ctx:
        return {"concept": "some", "first": bool(first_marker)}
    return None


def _presence_is_condition(text: str) -> bool:
    """프레즌스가 (트리거 아니라) 조건 위치인가. 선행 이벤트 절(-는데)·벽시계 트리거·
    모드 트리거 뒤에 오는 '있으면/없으면/있을 때'는 조건이다."""
    # 이벤트 서술어 + '-는데'(선행 트리거 절). '좋겠는데' 같은 종결형 '-는데'는 제외.
    if re.search(r"(?:열렸|열리|감지|됐|되|떨어졌|떨어지|올라갔|올라가|왔|울리)는데", text):
        return True
    # 벽시계 'N시'(N시간 제외) + 재실 서술어 → 시각 트리거 + 프레즌스 조건.
    if re.search(r"\d+\s*시(?!간)", text) and re.search(r"있|없", text):
        return True
    if re.search(r"모드가?\s*(?:켜질\s*때|되면|켜지면)", text):
        return True
    return False


_PRES_FOR_RE = re.compile(r"(\d+)\s*(분|시간|초)\s*(?:넘게|이상|동안|지나|계속)")


def _presence_for(text: str):
    """last/all 유지시간 for. 'N분 넘게/N시간 이상/N분 동안' → duration. 없으면 None."""
    m = _PRES_FOR_RE.search(text)
    if not m:
        return None
    key = {"분": "minutes", "시간": "hours", "초": "seconds"}[m.group(2)]
    return {key: int(m.group(1))}


def _augment_presence(normalized: str, sub: dict, settings: Optional[dict]) -> bool:
    """§1.3/§2.5 presence_agg 후처리. 처리했으면 True(시간·달력 augment 는 건너뜀).

    트리거 위치: 오파싱된 트리거(zone/daily 등)·조건을 걷어내고 presence_agg 트리거만 남긴다.
    조건 위치: 실제 트리거는 두고 presence_agg 조건을 더한다.
    """
    info = _presence_info(normalized)
    if info is None:
        return False
    is_cond = _presence_is_condition(normalized)
    concept = info["concept"]
    if is_cond:
        quant = "none" if concept == "empty" else ("all" if concept == "all" else "any")
    else:
        if concept == "empty":
            quant = "last"
        elif concept == "all":
            quant = "all"
        else:
            quant = "first" if info.get("first") else "any"
    node = {"type": "presence_agg", "quant": quant}
    # 특정 인물(나/와이프/우리 둘) 언급 → persons 명시. 생략 = 전체 person.*.
    if re.search(r"둘\s*다|우리\s*둘|나랑\s*와이프|와이프랑\s*나|부부|두\s*사람", normalized):
        persons = sorted(set((settings or {}).get("persons", {}).values())) \
            or ["person.user", "person.wife"]
        node["persons"] = persons
    if quant in ("last", "all") and not is_cond:
        fr = _presence_for(normalized)
        if fr is not None:
            node["for"] = fr
    if is_cond:
        if not any(c.get("type") == "presence_agg" for c in sub["conditions"]):
            sub["conditions"].append(node)
    else:
        # 트리거 위치: 오파싱 트리거/조건 제거(gold 는 presence 단일 트리거 + 조건 없음).
        sub["triggers"] = [node]
        sub["conditions"] = []
    return True


# ===========================================================================
# Phase 3c — 액션 파라미터(§3.1·§3.2) · 한정지속 복원(§4.7). sub 제자리 후처리.
# ===========================================================================
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


# §3.1 색 이름 → rgb_color 고정 팔레트.
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
# §3.1 색온도: 전구색/따뜻한 색=2700 · 주백색=4000 · 주광색/하얀 불=6500.
_KELVIN_PALETTE = [
    (re.compile(r"전구색|따뜻한\s*색|따뜻한색|따뜻하게|웜\s*화이트|온백색"), 2700),
    (re.compile(r"주백색|중백색|자연색"), 4000),
    (re.compile(r"주광색|하얀\s*불|하얀불|시원한\s*색|쿨\s*화이트|형광색"), 6500),
]


def _light_service_data(text: str, action: str) -> dict:
    """light 서비스 표현 → data(색·색온도·상대밝기·transition). 명시 표현 없으면 {}."""
    data: dict = {}
    is_off = action.endswith("turn_off")
    if not is_off:
        # 색온도(전구색/주백색/주광색)를 먼저 본다 — '주백색'이 rgb '백색' 부분매칭에
        #   잡히지 않도록(주백색=4000, 백색만=흰색). 그 다음 색이름 rgb.
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
        # 상대 밝기(step): "더 밝게"(+) / "어둡게"(-). 수치% 있으면 그 값, 없으면 ±20.
        step_sign = None
        if re.search(r"더\s*밝게|더밝게|밝게\s*좀|더\s*환하게", text):
            step_sign = 1
        elif re.search(r"어둡게", text) and not re.search(r"제일\s*어둡|가장\s*어둡|최소", text):
            step_sign = -1
        if step_sign is not None:
            pm = re.search(r"(\d+)\s*(?:퍼센트|프로|%|퍼)", text)
            data["brightness_step_pct"] = step_sign * (int(pm.group(1)) if pm else 20)
    # transition(끄기에도 허용): 'N초에 걸쳐' > '천천히'(10) > '서서히/부드럽게'(5).
    tm = re.search(r"(\d+)\s*초\s*(?:에\s*걸쳐|동안|만큼|간)", text)
    if tm:
        data["transition"] = int(tm.group(1))
    elif re.search(r"천천히", text):
        data["transition"] = 10
    elif re.search(r"서서히|부드럽게|살살|스르르|자연스럽게", text):
        data["transition"] = 5
    return data


def _apply_light_params(text: str, sub: dict) -> None:
    """light on/off 서비스 액션에 §3.1 data 를 얹는다(명시 파라미터 표현이 있을 때만)."""
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


_TOGGLE_RE = re.compile(r"반대\s*(?:로|상태)|토글")
_TOGGLABLE = {"turn_on", "turn_off", "open_cover", "close_cover", "lock", "unlock"}


def _apply_toggle(sub: dict) -> bool:
    """반대로/토글(§3.2) → <domain>.toggle(단일 도메인) / homeassistant.toggle(혼합)."""
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
    act = f"{doms[0]}.toggle" if len(doms) == 1 else "homeassistant.toggle"
    others = [a for a in sub["actions"] if a not in svc]
    sub["actions"] = [{"type": "service", "action": act,
                       "target": {"entity_id": ids}}] + others
    return True


# §4.7 한정 지속·복원: "N분만 켰다가 꺼" → [on, delay N, off](역순 '껐다가 켜' 포함).
_REVERT_RE = re.compile(
    r"(\d+)\s*(분|시간|초)\s*(?:만|정도|가량|쯤|동안)?\s*(?:좀\s*|딱\s*|그냥\s*|정도\s*)*"
    r"(?:[가-힣]+\s+)?"   # L1: 대상 명사(환풍기/조명) 사이 삽입 허용("5분만 환풍기 켜놨다")
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
    """한정 지속·복원 시퀀스를 기존 서비스 액션의 대상/도메인으로 [act1, delay, act2] 재구성."""
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
    # §4.1: 트리거가 없고 결과상 상태('무드등 켜져 있는데')만 있으면 대상 엔티티로
    #   진입에지 state 트리거 승격(대상=상태 주어인 한정지속 문형).
    if not sub["triggers"] and len(ids) == 1:
        am = re.search(r"(?:켜져|꺼져|열려|닫혀|잠겨|풀려)\s*있", normalized)
        if am:
            stv = _aspect_state(_QUOTE_SPAN_RE.sub(" ", normalized), ids[0])
            sub["triggers"] = [{"type": "state", "entity_id": ids[0], "to": stv}]
    return True


# §4.2 수치 에지: 범위이탈("벗어나면")·이중 에지("아래로 …거나 위로")·between-트리거.
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
    """수치 센서 엔티티를 device_class 키워드로 강제 해석(온도/습도/미세먼지/전력).

    '보일러/에어컨' 같은 제어기기 개념이 섞여 P._find_concept 가 climate 를 고르는
    문장(예 '…사이일 때 안방 보일러 꺼')에서도 센서를 정확히 집기 위해 도메인을 고정한다.
    """
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
    area = P._find_area(gz, text)
    cands = gz.resolve_concept({"domain": "sensor", "device_class": dc}, area, text)
    return cands[0]["id"] if cands else None


def _drop_sensor_service(sub: dict) -> None:
    """센서(sensor.*)를 대상으로 하는 오파싱 service 액션 제거(센서는 제어 불가)."""
    sub["actions"] = [a for a in sub["actions"]
                      if not (a.get("type") == "service"
                              and any(x.split(".")[0] == "sensor"
                                      for x in _target_ids(a)))]


def _mark_savable(result: dict, sub: dict) -> None:
    """오버레이가 트리거를 세워 모델이 완결(트리거+액션)되면 result 를 저장가능(ok)으로.
    앱 parse 가 트리거 미검출로 ok=False 로 표시한 문장을 후처리가 살렸을 때, 하이브리드
    L2 매처가 이 exact 결과를 덮어써 회귀내는 것을 막는다(§5 매처는 not-ok 만 흡수)."""
    if result is None or not sub.get("triggers") or not sub.get("actions"):
        return
    result["ok"] = True
    result["unmatched"] = []
    if not result.get("confidence") or result["confidence"] < 0.6:
        result["confidence"] = 0.7


def _augment_numeric_edge(normalized: str, sub: dict, gz, result=None) -> None:
    """§4.2 수치 에지 마무리 — 범위이탈/이중에지(두 트리거)·between-트리거(한 노드)."""
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
    # 트리거가 비고 numeric 조건도 없을 때만: between-트리거 · 단일 에지 fallback.
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
    # 단일 에지 fallback: 개념 미해석으로 트리거가 빈 'N 밑으로 떨어지면 / N 넘으면'.
    below_m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:도|와트|퍼센트|프로|%)?\s*(?:이하|미만|밑|아래)", normalized)
    above_m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:도|와트|퍼센트|프로|%)?\s*(?:이상|초과)"
        r"|(\d+(?:\.\d+)?)\s*(?:도|와트|퍼센트|프로|%)?\s*(?:을|를)?\s*넘", normalized)
    if bool(below_m) == bool(above_m):
        return   # 방향 미상/충돌 — 관여 안 함
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


# L1 날씨형 전이 → numeric_state(습도/온도). '습해지/눅눅해지/습하'=습도 above,
#   '더워지'=온도 above, '추워지'=온도 below. 임계값: N% / N도 뒤에 이상/이하/보다가 붙은
#   명시값 우선(액션존의 설정온도 'N도로'는 배제), 없으면 관례 기본(습도 above 70·추위 below 20).
_WEATHER_HUMID_RE = re.compile(r"습해지|눅눅|축축|꿉꿉|습하|후덥")
_WEATHER_HOT_RE = re.compile(r"더워지|더워|더우|무더|후텁")
_WEATHER_COLD_RE = re.compile(r"추워지|추워|추우|쌀쌀|서늘|썰렁")
_WEATHER_THRESH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|퍼센트|프로|도)?\s*(?:이상|이하|초과|미만|보다)")


def _weather_numeric(normalized: str, gz, default_area):
    """날씨형 전이 절 → numeric_state 노드(습도/온도) 또는 None. 결정적."""
    if gz is None:
        return None
    humid = _WEATHER_HUMID_RE.search(normalized)
    hot = _WEATHER_HOT_RE.search(normalized)
    cold = _WEATHER_COLD_RE.search(normalized)
    if not (humid or hot or cold):
        return None
    dc = "humidity" if humid else "temperature"
    area = P._find_area(gz, normalized) or default_area
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
            return None            # 더위 기본 임계 미관측 — 명시 없으면 관여 안 함
        node["above"] = explicit
    else:  # cold
        node["below"] = explicit if explicit is not None else 20.0
    return node


# §4.1 held: "N분/시간 넘게 …켜져/열려 있으면" → state 트리거 for 지속.
_HELD_FOR_RE = re.compile(r"(\d+)\s*(분|시간|초)\s*(?:넘게|이상|동안|째|내내|계속)")


def _augment_held_for(normalized: str, sub: dict) -> None:
    """지속 조건 'N분/시간 넘게 …있으면' 을 state 트리거의 for 로 반영(§4.1).
    오파싱된 numeric_state('두 시간'→above 2)는 결과상 state+for 로 교정한다."""
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
                # 인용 메시지('…꺼도 될…')의 극성어가 상태 계산을 오염시키지 않게 제거.
                stv = _aspect_state(_QUOTE_SPAN_RE.sub(" ", normalized), eid)
                sub["triggers"][i] = {"type": "state", "entity_id": eid,
                                      "to": stv, "for": dur}
                sub["conditions"] = [
                    c for c in sub["conditions"]
                    if not (c.get("type") == "time"
                            or (c.get("type") == "state"
                                and c.get("entity_id") == eid))]
                return


def _augment_actions_only(sentence: str, normalized: str, sub: dict,
                          is_repeat: bool) -> None:
    """액션측 후처리(§3.3·§3.4·§3.1·§3.2·§4.7) — sub 를 제자리 수정.
    우선순위: repeat → 인용 notify → 한정지속 복원 → 토글 → light 파라미터.
    프레즌스 처리 경로와 시간·달력 경로 양쪽에서 공유(중복 제거)."""
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
        rep = _build_count_repeat(sub, normalized)
        if rep is None:
            rep = _build_until_repeat(sub, normalized)
        sub["actions"] = [rep] if rep is not None else [{"type": "repeat"}]
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


def _augment_time_calendar(sentence: str, normalized: str, result: dict,
                           settings: Optional[dict] = None, gz=None) -> dict:
    """Phase 3a 후처리 — sun/time_pattern/sun_window/weekday/day_of_month/interval_anchor/
    repeat/notify 노드를 결정적으로 얹는다. result(model) 를 제자리 수정.
    Phase 3b: presence_agg(§1.3/§2.5) 를 먼저 처리하고, 처리되면 시간·달력 축은 건너뛴다."""
    model = result.get("model") or {}
    sub = _primary_subrule(model)
    if sub is None:
        return result
    sub.setdefault("triggers", [])
    sub.setdefault("conditions", [])
    sub.setdefault("actions", [])

    is_repeat = _is_repeat_action(normalized)

    # Phase 3b: 프레즌스 양화가 적용되면 시간·달력 신규노드는 건너뛴다(상호배타 문장군).
    presence_done = _augment_presence(normalized, sub, settings)
    if presence_done:
        _augment_actions_only(sentence, normalized, sub, is_repeat)
        return result

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

    # --- 트리거: 날씨형 전이(습해지/더워지/추워지) → numeric_state(습도/온도). sun 오검출보다
    #     먼저 판정해 '습해지'의 '해지' 오탐을 대체하고, 트리거 미검출 문장을 살린다. ---
    if not any(t.get("type") == "numeric_state" for t in sub["triggers"]) \
            and not any(c.get("type") == "numeric_state" for c in sub["conditions"]):
        wnode = _weather_numeric(normalized, gz, None)
        if wnode is not None:
            sub["triggers"] = [t for t in sub["triggers"]
                               if t.get("type") not in ("sun", "segment", "daily")]
            sub["triggers"].insert(0, wnode)
            _drop_sensor_service(sub)
            _mark_savable(result, sub)

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

    # --- 트리거: 수치 에지 마무리(§4.2) — 범위이탈/이중에지/between-트리거. ---
    _augment_numeric_edge(normalized, sub, gz, result)

    # --- 트리거: 지속(held) for 마무리(§4.1) — 'N분 넘게 …있으면'. ---
    _augment_held_for(normalized, sub)

    # --- 액션: repeat(§3.4) 우선, 아니면 인용 notify(§3.3). ---
    _augment_actions_only(sentence, normalized, sub, is_repeat)
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
    P.VERB_STEMS = list(P.VERB_STEMS) + ["잡히", "움직이", "나서", "비우", "새", "높", "낮",
                                         "들어가", "지나가", "찍"]  # L1 입실·통과·임계도달

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

    saved["_build_motion"] = P._Parser._build_motion
    P._Parser._build_motion = _build_motion_B               # L1 모션 방 전파

    saved["_find_action_boundary"] = P._Parser._find_action_boundary
    P._Parser._find_action_boundary = _find_action_boundary_B  # L1 후행 쉼표 액션 경계

    saved["_extract_topic"] = P._Parser._extract_topic
    P._Parser._extract_topic = _extract_topic_B             # L1 동사 관형형 '-는' 배제

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
        P._Parser._build_motion = saved["_build_motion"]
        P._Parser._find_action_boundary = saved["_find_action_boundary"]
        P._Parser._extract_topic = saved["_extract_topic"]
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
    result = _augment_time_calendar(sentence, normalized, result, settings, gz)
    _remap_erv_fan(result, normalized, gz)                  # L1 환기팬(습도/미세먼지)→전열교환기
    _augment_else_branch(sentence, normalized, result, gz, settings)  # L1 else 분기(§4.6)
    _augment_negation_not(sentence, normalized, result, gz)           # L1 수치 NOT 조건(§4.3)
    return result


# ===========================================================================
# L1 부정 NOT 래퍼(§4.3) — "N 넘지 않으면/안 넘으면/못 넘으면" = NOT[numeric_state above N].
#   시각(daily) 트리거가 이미 정확하고 수치 노드가 아직 없을 때만 조건을 얹는다(보수적).
#   수치 이분 상태를 반대로 뒤집는 게 아니라(경계 미포함 의미가 달라짐) not 래퍼로 표현한다.
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
    # 이미 수치 노드가 있으면(트리거/조건) 방향·부정 판정이 어긋날 수 있어 관여하지 않음.
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


def _remap_erv_fan(result: dict, sentence: str, gz) -> None:
    """'환기팬'이 습도/미세먼지 맥락에서 쓰이면 전열교환기(ERV)로 재매핑(환풍기≠ERV).
    습도/공기질 환기는 항상 전열교환기이므로 오검출된 욕실 환풍기 대상만 바꾼다. 결정적."""
    if gz is None or "환기팬" not in sentence:
        return
    if not re.search(r"습도|미세먼지|초미세|공기\s*질|공기질", sentence):
        return
    erv = next((e["entity_id"] for e in gz.entities if e["domain"] == "fan"
                and ("전열교환기" in (e.get("name") or "") or e["entity_id"].endswith("erv"))), None)
    bath = next((e["entity_id"] for e in gz.entities if e["domain"] == "fan"
                 and "환풍기" in (e.get("name") or "")), None)
    if not erv or not bath:
        return
    model = result.get("model") or {}
    subs = model.get("subrules") if isinstance(model.get("subrules"), list) else [model]
    for sub in subs:
        if not isinstance(sub, dict):
            continue
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
# L1 else 분기(§4.6) — "A면 X, 아니면 Y" / 대비쌍 후행절을 if/else 액션으로 조립.
#   파서가 명시 대비("아니면"류)를 다중 서브룰로 쪼갠 것을, 한 트리거 아래
#   {type:if, if:[조건], then:[액션1], else:[액션2]} 단일 서브룰로 합친다.
#   ★ 명시 대비 표현이 있을 때만 발동(회귀 방지). 대칭 전이형("켜지면 A 꺼지면 B",
#   "아니면" 없음)은 게이트에 걸리지 않아 서브룰 2개 그대로 유지한다.
# ===========================================================================
_ELSE_MARK_RE = re.compile(
    r"아니면|아니고|아닐\s*때|안\s*그러면|그렇지\s*않으면|그렇지\s*않다면|그\s*외에")


def _detect_if_condition(normalized: str, subs: list, trigs: list, gz, settings) -> list:
    """else 분기의 if 조건 노드(하나)를 검출. 못 찾으면 [](조립은 계속 — if 내부는 채점
    비교 대상이 아니므로 트리거 정확성이 exact 를 결정한다). 정직성 차원의 성의 검출."""
    # (1) 트리거가 아닌 서브룰 조건(numeric_state/state 등)을 그대로 if 조건으로.
    for s in subs:
        for c in s.get("conditions", []) or []:
            if isinstance(c, dict) and c.get("type") and c.get("type") != "time":
                return [dict(c)]
    # (2) presence 양화("둘 다 집에 있을 때/아무도 없으면").
    info = _presence_info(normalized)
    if info:
        q = {"empty": "none", "all": "all", "some": "any"}.get(info["concept"], "any")
        node = {"type": "presence_agg", "quant": q}
        if re.search(r"둘\s*다|우리\s*둘|부부|두\s*사람", normalized):
            persons = sorted(set((settings or {}).get("persons", {}).values())) \
                or ["person.user", "person.wife"]
            node["persons"] = persons
        return [node]
    # (3) 요일(개별/부정) — bare 평일/주말은 (4)에서 day_type 로.
    wd, wneg, wbare = _detect_weekdays(normalized)
    if wd and not wbare:
        return [{"type": "weekday", "days": wd, "negate": wneg}]
    # (4) day_type(평일/주말) 대비쌍.
    if "평일" in normalized:
        return [{"type": "day_type", "types": ["weekday"]}]
    if "주말" in normalized:
        return [{"type": "day_type", "types": ["weekend"]}]
    # (5) 밤창(sun_window) — 세그먼트보다 먼저(‘해 진 뒤’).
    if _NIGHTWIN_RE.search(normalized) or re.search(r"해\s*진\s*뒤", normalized):
        return [{"type": "sun_window", "after": "sunset", "before": "sunrise"}]
    # (6) 시간대 세그먼트.
    for w, seg in (("오전", "morning"), ("오후", "afternoon"), ("새벽", "dawn"),
                   ("아침", "morning"), ("저녁", "evening"), ("밤", "night"), ("낮", "afternoon")):
        if w in normalized:
            return [{"type": "time_segment", "segments": [seg]}]
    return []


# 극성(켜/끄) 판정 — 대비쌍 then/else 재구성용. 명령 동사 어간만(트리거 '켜지면' 등은
# 절 분리 후 스캔하므로 영향 최소).
_ON_VERB_RE = re.compile(r"켜|틀|열어|여[는나]|올려|가동|작동|점등|높여|데워|재생|방송|가습")
_OFF_VERB_RE = re.compile(r"꺼|끄|닫|내려|잠[그가긴]|소등|낮춰|멈춰|정지|중지")


def _clause_polarity(text: str):
    """절 표면형 → 'on'/'off'/None. 켜류 동사만 있으면 on, 끄류만 있으면 off."""
    on = _ON_VERB_RE.search(text)
    off = _OFF_VERB_RE.search(text)
    if on and not off:
        return "on"
    if off and not on:
        return "off"
    if on and off:  # 둘 다 — 뒤에 온 것(문말 서술)이 그 절의 명령.
        return "on" if on.start() > off.start() else "off"
    return None


def _polar_service(action: str, pol: str) -> str:
    """서비스명 + 극성 → 서비스명. cover 는 open/close, 그 외 turn_on/off."""
    domain = (action or "").split(".")[0] or "homeassistant"
    if domain == "cover":
        return "cover.open_cover" if pol == "on" else "cover.close_cover"
    return domain + (".turn_on" if pol == "on" else ".turn_off")


def _weekday_contrast_then_else(normalized: str, acts: list):
    """'평일엔 X, 주말엔 Y' 의 then(평일)·else(주말) 액션을 표면 동사 극성으로 재구성.
    파서가 대비를 한 액션으로 병합(then==else 오류)해도 base service 의 대상/도메인을
    on/off 로 전개해 복원한다. 극성 확정 불가·동일 극성이면 None(→ 기존 로직 유지)."""
    base = next((a for a in acts if isinstance(a, dict) and a.get("type") == "service"
                 and isinstance(a.get("target"), dict)
                 and a["target"].get("entity_id")), None)
    if base is None:
        return None
    p_idx = normalized.find("평일")
    w_idx = normalized.find("주말")
    if p_idx < 0 or w_idx < 0 or p_idx >= w_idx:
        return None
    then_pol = _clause_polarity(normalized[p_idx + 2:w_idx])  # 평일절
    else_pol = _clause_polarity(normalized[w_idx + 2:])       # 주말절
    if then_pol is None or else_pol is None or then_pol == else_pol:
        return None
    tgt = base["target"]
    then_act = {"type": "service", "action": _polar_service(base["action"], then_pol),
                "target": tgt}
    else_act = {"type": "service", "action": _polar_service(base["action"], else_pol),
                "target": tgt}
    return [then_act], [else_act]


def _augment_else_branch(sentence: str, normalized: str, result: dict, gz, settings) -> None:
    """다중 서브룰(명시 대비)을 한 트리거 + if/else 액션으로 조립."""
    if not isinstance(result, dict):
        return
    model = result.get("model")
    if not isinstance(model, dict):
        return
    # 다중 규칙은 subrules 리스트, 단일 규칙은 평탄 model(subrules 키 없음)로 반환된다.
    subs_raw = model.get("subrules")
    if isinstance(subs_raw, list) and subs_raw:
        subs, flat = subs_raw, False
    elif isinstance(model.get("triggers"), list) or isinstance(model.get("actions"), list):
        subs, flat = [model], True
    else:
        return
    # 단일 서브룰(또는 평탄 단일 규칙)이 평일/주말 대비쌍("평일엔 X, 주말엔 Y")이면 if/else 로.
    # 파서가 대비를 한 서브룰로 병합해 액션이 하나여도, if 내부는 채점 비교 대상이 아니므로
    # 트리거만 정확하면 exact. ★ '평일' 과 '주말' 이 동시에 있을 때만(명시 대비) 발동.
    if len(subs) == 1:
        if not ("평일" in normalized and "주말" in normalized):
            return
        # 배제("주말은 빼고" = 평일에만)는 대비쌍이 아니다 — weekday 조건이지 if/else 아님.
        if re.search(r"빼고|말고|제외", normalized):
            return
        s0 = subs[0]
        trigs = list(s0.get("triggers") or [])
        acts = [a for a in s0.get("actions", []) or []
                if isinstance(a, dict)
                and a.get("type") in ("service", "set_mode", "delay", "repeat")
                and not (a.get("type") == "service"
                         and a.get("action") == "homeassistant.turn_on")]
        if not trigs or not acts:
            return
        if_cond = _detect_if_condition(normalized, subs, trigs, gz, settings)
        # 파서가 대비를 한 액션으로 병합했으면(then==else) 표면 극성으로 then/else 복원.
        te = _weekday_contrast_then_else(normalized, acts)
        if te is not None:
            then_acts, else_acts = te
        else:
            then_acts, else_acts = [acts[0]], [acts[-1]]
        if_node = {"type": "if", "if": if_cond, "then": then_acts, "else": else_acts}
        if flat:
            model["conditions"] = []
            model["actions"] = [if_node]
        else:
            model["subrules"] = [{"triggers": trigs, "conditions": [],
                                  "actions": [if_node]}]
        return
    if not (_ELSE_MARK_RE.search(normalized)
            or ("평일" in normalized and "주말" in normalized
                and not re.search(r"빼고|말고|제외", normalized))):
        return
    # 트리거: 첫 트리거 보유 서브룰. 없으면 sun 보정(‘해 지면/뜨면’).
    trig_sub = next((s for s in subs if s.get("triggers")), None)
    trigs = list(trig_sub["triggers"]) if trig_sub else []
    if not trigs:
        sun_evt = "sunrise" if _SUNRISE_RE.search(normalized) else (
            "sunset" if _SUNSET_RE.search(normalized) else None)
        if sun_evt:
            node = {"type": "sun", "event": sun_evt}
            off = _sun_offset(normalized)
            if off:
                node["offset"] = off
            trigs = [node]
    if not trigs:
        return
    # then/else 액션(파서가 만든 실질 액션). 우선 빈-대상 오파싱(homeassistant.turn_on)을
    # 제외하고, 그러면 하나뿐일 때만 포함해서 채운다(if 내부는 채점 비교 대상이 아님).
    def _pick(include_ha):
        return [a for s in subs for a in s.get("actions", []) or []
                if isinstance(a, dict)
                and a.get("type") in ("service", "set_mode", "delay", "repeat")
                and (include_ha or not (a.get("type") == "service"
                                        and a.get("action") == "homeassistant.turn_on"))]
    svc_acts = _pick(False)
    if len(svc_acts) < 2:
        svc_acts = _pick(True)
    if len(svc_acts) < 2:
        return   # then/else 둘 다 필요 — 하나뿐이면 분기 조립 보류(안전)
    if_cond = _detect_if_condition(normalized, subs, trigs, gz, settings)
    if_node = {"type": "if", "if": if_cond,
               "then": [svc_acts[0]], "else": [svc_acts[-1]]}
    # 공통(if 분기 밖) 달력 조건: 매달 N일/격주는 서브룰 공통 조건으로 남는다.
    conds: list = []
    dom = _detect_day_of_month(normalized)
    if dom is not None:
        conds.append({"type": "day_of_month", "days": dom})
    iv = _detect_interval(normalized)
    if iv is not None:
        conds.append({"type": "interval_anchor", "unit": "week",
                      "interval": iv, "anchor": _INTERVAL_ANCHOR})
    model["subrules"] = [{"triggers": trigs, "conditions": conds, "actions": [if_node]}]
