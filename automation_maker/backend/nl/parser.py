"""§6.2 5단계 한국어 파서 — 문장 → RuleModel + chips (ParseResult).

의존성 0 (표준 라이브러리만). 형태소 분석기 금지.
파이프라인: 전처리 → 패턴 선추출(지속시간 P1/P2, 수치) → 절 분리(마지막 '면' 경계) →
절→노드 분류 → IR 방출.
"""
from __future__ import annotations

import re
from typing import Optional

from . import postpass, summary, surface
from .gazetteer import (DAY_TYPE_WORDS, DEVICE_CONCEPTS, MOTION_CONCEPT,
                        MOTION_WORDS, SEASON_WORDS, SEGMENT_WORDS, Gazetteer)
from .normalize import (find_clock, find_percent, find_temperature,
                        normalize_ws, strip_particles_simple, to_duration_obj,
                        token_boundary_ok)

# ---------------------------------------------------------------------------
# 어간/키워드 사전
# ---------------------------------------------------------------------------
# '면' 경계 검증용 동사 어간(직전 어절이 활용형일 때만 절 경계)
VERB_STEMS = [
    "있", "없", "되", "지나", "하", "열리", "닫히", "눌리", "도착", "감지",
    "바뀌", "올라가", "내려가", "넘", "떨어지", "켜지", "꺼지", "오", "가",
    "들어오", "나가", "유지", "풀리", "잠기", "울리", "닿", "생기", "뜨", "지",
    "오르", "내리", "낮아지", "높아지", "많아지", "적어지", "왔", "왔었",
    "아니",  # 모드 부정 조건('슬립모드가 아니면')의 '면' 경계 인식용(§6)
    # A5(라운드1): 절 경계('면') 인식용 어간 확장 — 잡히면/움직이면/나서면/비우면/새면.
    "잡히", "움직이", "나서", "비우", "새",
    # L1(#3): 수치 레벨 형용사(높으면/낮으면)·입실/통과(들어가면/지나가면)·임계도달(찍으면).
    "높", "낮", "들어가", "지나가", "찍",
]
# 이벤트(→트리거) 키워드
EVENT_KEYWORDS = ["도착", "열리", "열림", "열려", "닫히", "닫힘", "감지", "눌리",
                  "눌림", "켜지", "꺼지", "생기", "울리", "왔", "들어오", "나가"]
# 명령형(→액션) 힌트
# enabler(B1/B6): '잠그고/차단하고/소등/점등' 등 액션 연결어미가 절 경계로 인식되도록 확장.
COMMAND_HINTS = ["켜", "꺼", "끄", "틀", "바꿔", "바꾸", "올려", "내려", "멈춰",
                 "닫아", "열어", "잠가", "풀어", "실행", "가동", "작동", "설정",
                 "잠그", "차단", "소등", "점등"]
# 절 연결어미
CONNECTIVES = ["는데", "면서", "다가", "고", "며"]
# 부정 표현
NEG_WORDS = ["없", "아니", "안 "]

# ---------------------------------------------------------------------------
# 스코프/이벤트 정규식 (A3·A10 스코프, B1 zone 귀가/외출, B6 누수)
# ---------------------------------------------------------------------------
# A3/A10: 선두 스코프어(모든/집의 모든/전부/다). '다른/다시' 는 (?=\s) 로 배제한다.
_SCOPE_RE = re.compile(r"\s*(집의\s+모든|모든|전부|다)(?=\s)")
# B1: 귀가(enter) 표현. '도착/왔/들어오' 는 기존 _build_arrival(다인 로직)이 처리하므로
#     여기서는 그 외 귀가 표현(귀가/퇴근/집에 오·와·돌아)을 zone enter 로 라우팅한다.
_ZONE_ENTER_RE = re.compile(
    r"귀가|퇴근|집에?\s*(?:오|와|돌아|들어)|집에?\s*도착|도착하|들어오|왔")
# B1: 외출(leave) 표현. '나가/나서' 는 앞 음절이 한글이면(누나가·일어나서 등 오탐) 제외.
_ZONE_LEAVE_RE = re.compile(
    r"외출|(?<![가-힣])나가|집\s*비우|집을?\s*비우|(?<![가-힣])나서|집을?\s*나\b")
# B6: 누수/침수(moisture) 이벤트.
_LEAK_RE = re.compile(r"누수|물\s*새|물이\s*새|샘|침수")
# #17: 동사 관형형('-는/-은')은 주제어가 아니라 트리거 절이다. _extract_topic 이
#      'X 움직이는 …'의 '움직이는'을 주제로 오삼키지 않도록 배제.
_VERB_ADNOMINAL_RE = re.compile(
    r"(?:움직이|감지되|잡히|열리|닫히|켜지|꺼지|들어오|들어가|나가|지나가|풀리|잠기|울리"
    r"|생기|떨어지|올라가|내려가|오|되|나|가|넘|높아지|낮아지|없어지)는$")

# ---------------------------------------------------------------------------
# P2 상(aspect) 라우팅 헬퍼 (#8·#9·#10·#11·#14) — SPEC-SCHEMA-90 §4.1.
#   결과상('-어 있-')/지속상(-ㄹ 때, -는 동안)은 상태(조건), 전이형(켜지면/열리면/되면)은 트리거.
# ---------------------------------------------------------------------------
_RESULTATIVE_RE = re.compile(
    r"(?:켜져|꺼져|열려|닫혀|잠겨|풀려|채워|비워|걸려|놓여|담겨|덮여|서|앉아|누워|들어와)\s*있"
    r"|(?:켜진|꺼진|열린|닫힌|잠긴|풀린|열려있는|닫혀있는)\s*상태")
_DURATIVE_RE = re.compile(r"(?:을|ㄹ|일|는)\s*때|(?:는|은|인)\s*동안")
_UNLOCK_RE = re.compile(r"풀리|풀려|풀린")
_ROOM_ENTER_RE = re.compile(r"들어가|지나가|지나쳐")
_BETWEEN_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*도?\s*(?:에서|부터)\s*(-?\d+(?:\.\d+)?)\s*도?\s*(?:사이|까지)")
# L1: 수치 비교 표층 — 도달/찍/닿(임계 도달=above)도 포함.
_NUM_CMP_RE = r"이상|이하|초과|미만|넘|올라가|내려가|떨어지|밑|높아|낮아|높|낮|도달|닿|찍"
# 지속상 조건이 액션존 앞에 붙는 경우('… 감지되는 동안엔 조명 켜'/'… 닫혀 있을 때만 켜').
_DURATIVE_SPLIT_RE = re.compile(
    r"^(?P<cond>.*?(?:(?:을|ㄹ|일|는)\s*때(?:만|는|엔|에)?"
    r"|(?:는|은|인)\s*동안(?:엔|은|에)?))\s+(?P<act>.+)$")


def _is_state_aspect(clause: str) -> bool:
    """절이 결과상/지속상(상태) 절인가 → 트리거가 아니라 조건(승격 규칙 대상)."""
    return bool(_RESULTATIVE_RE.search(clause) or _DURATIVE_RE.search(clause))


def _between(clause: str):
    m = _BETWEEN_RE.search(clause)
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    return (min(a, b), max(a, b))


def _is_weather_clause(clause: str) -> bool:
    return bool(postpass._WEATHER_HUMID_RE.search(clause)
                or postpass._WEATHER_HOT_RE.search(clause)
                or postpass._WEATHER_COLD_RE.search(clause))


def _clause_has_numeric(clause: str) -> bool:
    """이 절이 numeric_state 트리거/조건을 만들 후보인가(_emit_numeric_aspect 게이트와 동일)."""
    weather = _is_weather_clause(clause)
    if not (weather or re.search(_NUM_CMP_RE, clause) or _between(clause)):
        return False
    if not weather and _find_concept(clause) is None and find_temperature(clause) is None:
        return False
    return True


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


def _room_enter_motion_area(gz: Gazetteer, clause: str):
    """방 입실/통과 트리거 절이면 그 방 area, 아니면 None(그 방 모션 센서로 라우팅용)."""
    area = _find_area(gz, clause)
    if area is None:
        return None
    if _ROOM_ENTER_RE.search(clause):
        return area
    if _ZONE_ENTER_RE.search(clause) or _ZONE_LEAVE_RE.search(clause) \
            or _LEAK_RE.search(clause):
        return None
    if re.search(r"움직임|모션|인기척|동작|움직이|누수|밸브|온도|습도|미세먼지"
                 r"|열리|열려|닫히|닫혀|감지|도착|왔|눌리|울리", clause):
        return None
    if re.search(r"(?<![가-힣])가(?:면|서|고)(?![가-힣])", clause):
        return area
    return None


def _split_durative_condition(clause: str):
    """#9: 액션 절이 '지속상 조건 + 액션'이면 (조건부, 액션부)로 분리. 아니면 (None, clause).
    '… 감지되는 동안엔 조명 켜' / '… 닫혀 있을 때만 켜'의 선행 지속상 절을 조건으로 뗀다."""
    m = _DURATIVE_SPLIT_RE.match(clause)
    if not m:
        return None, clause
    cond = m.group("cond").strip()
    act = m.group("act").strip()
    # 가드: 조건부에 상태/수치/모션 신호가 있고, 액션부에 실제 명령 동사가 있을 때만 분리.
    if not (_RESULTATIVE_RE.search(cond) or _between(cond)
            or re.search(r"감지|모션|움직|인기척|사람|높|낮|사이", cond)):
        return None, clause
    if not any(h in act for h in COMMAND_HINTS):
        return None, clause
    return cond, act


def _value_pct(clause: str):
    """B2: 밝기/세기 값 해석. %/절반/최대/약하게/단위없는 'N으로' → 0~100 정수. 없으면 None."""
    p = find_percent(clause)
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
        # '26도로/9시로' 같은 시각·온도 리터럴은 밝기값이 아니다.
        if not re.search(str(v) + r"\s*(?:도|시|분|초|시간)", clause):
            return v
    return None


def _value_dict(clause: str):
    """B2 값 해석을 {"value": n} 형태로(없으면 None). find_percent 대체(상위집합)."""
    v = _value_pct(clause)
    return {"value": v} if v is not None else None


def _time_range(clause: str):
    """B4: 'A시 부터 B시 까지/사이' → {type:time, after, before}. 시각 2개 + 범위표지."""
    if not re.search(r"부터", clause) or not re.search(r"까지|사이", clause):
        return None
    clks = []
    pos = 0
    while True:
        c = find_clock(clause[pos:])
        if not c:
            break
        clks.append(c["hhmm"])
        pos += c["span"][1]
    if len(clks) >= 2:
        return {"type": "time", "after": clks[0] + ":00", "before": clks[1] + ":00"}
    return None


def _duration_frames(text: str):
    """P1/P2 지속시간 패턴 선추출. 반환: (수정된 텍스트, frames)."""
    frames = []

    def repl(subj, loc, neg, seconds):
        idx = len(frames)
        frames.append({"subj": subj, "loc": loc, "neg": neg, "seconds": seconds,
                       "boundary": True})
        return f" \x00F{idx}\x00 "

    # loc(선택): '안방에' 같은 방 위치. subj 는 절 경계를 넘지 않도록 단일 체언으로 제한.
    # P2 먼저(더 김): (loc)(체언)이/가? (없|있)는 상태(로|가) N(초|분|시간) ... (되면|지나면|유지되면)
    p2 = re.compile(
        r"(?P<loc>[가-힣]+(?:에서|에는|에|은|는)\s+)?(?P<subj>[가-힣A-Za-z]+)\s*(?:이|가)?\s*"
        r"(?P<neg>없|있)는\s*상태\s*(?:로|가)?\s*(?P<n>\d+)\s*(?P<u>초|분|시간)\s*"
        r"(?:동안\s*)?(?:이|가)?\s*(?:되면|지나면|유지되면|지\s*나면)")
    # locpre: 지속 패턴 '앞'의 처소격 '[방]에(는)' 흡수 (예: "안방에 30분 동안 …").
    # A4: '동안' → '(?:동안|간)' 으로 확장해 "5분간 …" 도 지속시간 프레임으로 인식.
    p1 = re.compile(
        r"(?P<locpre>[가-힣]+(?:에서|에는|에)\s+)?"
        r"(?P<n>\d+)\s*(?P<u>초|분|시간)\s*(?:동안|간)\s*(?P<loc>[가-힣]+(?:에서|에는|에)\s+)?"
        r"(?P<subj>[가-힣A-Za-z]+)\s*(?:이|가)?\s*(?P<neg>없|있)으?면")
    # #6(P3): 주어 생략·주어선행·부정 모션 지속 held. p1(주어후행 없/있)이 못 잡는 부재형
    #   ('5분간 안 움직이면'·'모션이 10분 동안 없으면'·'1시간 넘게 감지 못 되면') → 모션 off 지속.
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
        # 동안 뒤 처소격(loc)을 우선, 없으면 앞쪽 처소격(locpre) 사용.
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


_SENTINEL_RE = re.compile(r"\x00F(\d+)\x00")


def _is_myeon_boundary(word: str) -> bool:
    # #5: 후행 쉼표 허용('높으면,' 처럼 쉼표가 붙어도 절 경계). 마침표·말줄임표는
    #     문장 흐름을 바꿀 수 있어 제외(회귀 방지).
    word = word.rstrip(",")
    if not word.endswith("면"):
        return False
    body = word[:-1]
    if body.endswith("으"):
        body = body[:-1]
    for stem in VERB_STEMS:
        if body.endswith(stem):
            return True
    if body.endswith("이"):  # copula (…이면 / …이상이면)
        return True
    return False


def _is_noun_surface(gz: Gazetteer, text: str) -> bool:
    t = strip_particles_simple(text.strip())
    if not t:
        return False
    if gz.resolve_name(t):
        return True
    if t in gz.room_surfaces or t in gz.person_surfaces or t in DEVICE_CONCEPTS:
        return True
    if t in MOTION_WORDS:
        return True
    return False


def _is_verby(tok: str) -> bool:
    if any(k in tok for k in EVENT_KEYWORDS):
        return True
    if any(k in tok for k in COMMAND_HINTS):
        return True
    for stem in ("있", "없", "되", "지나", "유지"):
        if stem in tok:
            return True
    if "이" in tok and ("면" in tok or "고" in tok):
        return True
    return False


def _split_clauses(gz: Gazetteer, text: str) -> list[str]:
    """연결어미(고/는데/며/면서)로 절 분리. 체언+병렬(하고/와/과)은 분리하지 않음."""
    tokens = text.split()
    clauses: list[str] = []
    cur: list[str] = []
    for tok in tokens:
        cur.append(tok)
        suf = None
        for c in CONNECTIVES:
            if tok.endswith(c) and len(tok) > len(c):
                suf = c
                break
        if suf:
            body = tok[: -len(suf)]
            # 명사 병렬(체언+하고/와/과) → 절 분리 아님
            if suf in ("고",) and body.endswith("하") and _is_noun_surface(gz, body[:-1]):
                continue
            if _is_noun_surface(gz, body):
                continue
            if _is_verby(tok):
                clauses.append(" ".join(cur))
                cur = []
    if cur:
        clauses.append(" ".join(cur))
    return clauses


# ---------------------------------------------------------------------------
# 표적/값 해석
# ---------------------------------------------------------------------------
def _find_area(gz: Gazetteer, text: str) -> Optional[str]:
    best = None
    best_len = 0
    for surf, aid in gz.room_surfaces.items():
        if surf in text and len(surf) > best_len:
            best, best_len = aid, len(surf)
    return best


def _find_concept(text: str) -> Optional[dict]:
    # 최장일치 기기어
    hit = None
    hit_len = 0
    for surf, concept in DEVICE_CONCEPTS.items():
        if surf in text and len(surf) > hit_len:
            hit, hit_len = concept, len(surf)
    for surf in MOTION_WORDS:
        if surf in text and len(surf) > hit_len:
            hit, hit_len = MOTION_CONCEPT, len(surf)
    return hit


def _find_persons(gz: Gazetteer, text: str) -> list[tuple[str, str]]:
    out = []
    for surf, pid in gz.person_surfaces.items():
        start = 0
        while True:
            idx = text.find(surf, start)
            if idx < 0:
                break
            # 한글 토큰 경계 검사: '누나'의 '나' 같은 부분매칭 거부, '나와/와이프가'는 허용
            if token_boundary_ok(text, idx, idx + len(surf), surf):
                out.append((idx, surf, pid))
                break
            start = idx + 1
    out.sort()
    return [(surf, pid) for _, surf, pid in out]


class _Chip:
    __slots__ = ("span", "text", "role", "slot_key", "status", "chosen", "candidates")

    def __init__(self, text, role, slot_key, candidates, span=None):
        self.text = text
        self.role = role
        self.slot_key = slot_key
        self.candidates = candidates
        self.span = span or [0, 0]
        if not candidates:
            self.status = "unresolved"
            self.chosen = None
        elif len(candidates) == 1 and candidates[0].get("score", 0) >= 0.8:
            self.status = "confirmed"
            self.chosen = candidates[0]["id"]
        elif candidates[0].get("score", 0) >= 0.8 and (len(candidates) == 1):
            self.status = "confirmed"
            self.chosen = candidates[0]["id"]
        else:
            self.status = "uncertain"
            self.chosen = candidates[0]["id"]

    def as_dict(self):
        return {"span": self.span, "text": self.text, "role": self.role,
                "slot_key": self.slot_key, "status": self.status,
                "chosen": self.chosen, "candidates": self.candidates}


class _Parser:
    def __init__(self, sentence: str, gz: Gazetteer, settings: dict, pins: dict,
                 normalized: Optional[str] = None):
        self.sentence = sentence
        # 표면정규화된 파싱용 텍스트. 원문(sentence)은 alias·칩 스팬·인용 메시지 기준으로
        # 보존한다(§1.1). 직접 _Parser 구성(테스트) 시 normalized 미지정이면 원문을 쓴다.
        self.normalized = normalized if normalized is not None else sentence
        self.gz = gz
        self.settings = settings or {}
        self.pins = pins or {}
        self.triggers: list[dict] = []
        self.conditions: list[dict] = []
        self.actions: list[dict] = []
        self.chips: list[_Chip] = []
        self.unmatched: list[str] = []
        self.warnings: list[str] = []
        self.condition_mode = "and"
        self.default_area: Optional[str] = None
        self.default_target: Optional[dict] = None  # concept for topic target
        self.rule_area: Optional[str] = None
        self.ante_text: str = ""  # 트리거/조건 존(액션 존 제외) — '다른' 판정용
        # 다중 서브룰 컨텍스트 상속(§2.1): 앞 서브룰의 방·액션 대상을 뒤 서브룰이 물려받는다.
        self.inh_area: Optional[str] = None
        self.inh_action_entity: Optional[str] = None
        # §2.1 조건 엔티티 재참조: 앞 서브룰에서 마지막으로 언급된 트리거/조건 센서(및 그 방).
        # 액션 대상 area(inh_area) 상속과 분리 — 생략된 "모션이 없으면"은 이 센서를 재사용한다.
        self.inh_trigger_entity: Optional[str] = None
        self.inh_trigger_area: Optional[str] = None

    # ---- 스팬 계산(칩용): 원문에서 부분문자열 위치 ----
    def _span_of(self, sub: str) -> list[int]:
        idx = self.sentence.find(sub)
        return [idx, idx + len(sub)] if idx >= 0 else [0, 0]

    def _assign_spans(self):
        """[0,0] 스팬 칩에 원문 내 실제 위치를 역탐색해 부여(§결함9).

        동일 표면형이 여러 번 나오면 이미 배정된 위치를 피해 순서대로 매칭한다.
        원문에서 특정할 수 없는 칩만 [0,0]으로 남긴다(프론트가 별도 처리).
        """
        used: dict[str, set[int]] = {}
        for chip in self.chips:
            if chip.text and chip.span and chip.span != [0, 0]:
                used.setdefault(chip.text, set()).add(chip.span[0])
        for chip in self.chips:
            if chip.span and chip.span != [0, 0]:
                continue
            sub = chip.text
            if not sub:
                continue
            seen = used.setdefault(sub, set())
            idx = self.sentence.find(sub)
            while idx >= 0 and idx in seen:
                idx = self.sentence.find(sub, idx + 1)
            if idx >= 0:
                seen.add(idx)
                chip.span = [idx, idx + len(sub)]

    # ---- 칩 생성(핀 우선 반영) ----
    def _chip(self, text, role, slot_key, cands, span=None):
        """엔티티 후보를 담는 칩을 만든다. pin된 슬롯은 후보 계산을 건너뛰고 확정 유지."""
        if slot_key in self.pins:
            pid = self.pins[slot_key]
            e = self.gz.entity(pid)
            cands = ([self.gz._cand(e, 1.0, "사용자 확정")] if e
                     else [{"id": pid, "label": pid, "sublabel": "", "score": 1.0}])
            chip = _Chip(text, role, slot_key, cands, span)
            chip.status = "confirmed"
            chip.chosen = pid
        else:
            chip = _Chip(text, role, slot_key, cands, span)
        self.chips.append(chip)
        return chip

    # ---- 토픽(선두 X은/는) ----
    def _extract_topic(self, antecedent: str) -> str:
        tokens = antecedent.split()
        for i in range(min(3, len(tokens))):
            tok = tokens[i]
            if tok.endswith("은") or tok.endswith("는"):
                if _VERB_ADNOMINAL_RE.search(tok):
                    continue  # #17: 동사 관형형(주제 아님) → 다음 후보 확인
                topic_txt = " ".join(tokens[: i + 1])
                base = topic_txt[:-1].strip()
                area = _find_area(self.gz, base)
                concept = _find_concept(base)
                name_cands = self.gz.resolve_name(strip_particles_simple(base))
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

    # ================================================================
    def _find_pivots(self, text: str, frames) -> list[int]:
        """동사-검증된 '면' 종결어미 + 경계 센티넬 위치를 모두 찾는다(§2.1 1단계)."""
        tokens = text.split()
        pivots = []
        for i, tok in enumerate(tokens):
            m = _SENTINEL_RE.search(tok)
            if m and frames[int(m.group(1))]["boundary"]:
                pivots.append(i)
            elif _is_myeon_boundary(tok):
                pivots.append(i)
        return pivots

    def parse(self):
        # §1.1: 파싱은 표면정규화된 텍스트로 하되 alias·스팬은 원문(self.sentence) 기준.
        text = normalize_ws(self.normalized)
        # 2) 지속시간 선추출
        text, frames = _duration_frames(text)

        # §2.1: 조건 종결어미('면'/경계 센티넬) pivot 을 모두 찾는다. 2개 이상이면 다중
        # 규칙쌍(subrules) 문법으로, 1개 이하면 기존 단일 규칙 경로로 처리한다(하위호환).
        pivots = self._find_pivots(text, frames)
        if len(pivots) >= 2:
            return self._parse_multi(text, frames, pivots)

        # 3) 마지막 '면' 경계 찾기 (센티넬 우선)
        tokens = text.split()
        boundary_idx = None
        for i in range(len(tokens) - 1, -1, -1):
            tok = tokens[i]
            m = _SENTINEL_RE.search(tok)
            if m and frames[int(m.group(1))]["boundary"]:
                boundary_idx = i
                break
            if _is_myeon_boundary(tok):
                boundary_idx = i
                break

        if boundary_idx is None:
            # '면' 경계가 없어도 "매일 … H시"는 daily 트리거로 살린다(§3).
            antecedent, consequent = self._split_daily_no_boundary(text)
        else:
            antecedent = " ".join(tokens[: boundary_idx + 1])
            consequent = " ".join(tokens[boundary_idx + 1:])

        # 토픽 추출
        antecedent = self._extract_topic(antecedent)
        # '다른' 판정용: 트리거/조건 존(antecedent) 텍스트만 보관 (액션 존 제외)
        self.ante_text = antecedent

        # 절 분리
        ante_clauses = self._split_by_sentinel(antecedent, frames)
        cons_clauses = _split_clauses(self.gz, consequent) if consequent else []

        # 4) 절 → 노드
        self._process_antecedent(ante_clauses, frames)
        self._process_consequent(cons_clauses)

        self._assign_spans()
        return self._emit(frames)

    def _split_daily_no_boundary(self, text: str):
        """'면' 경계가 없을 때 antecedent 를 분리(§3, B4 확장):
        (1) markerless 시각(밤 11시엔/매일 H시) → 시각 트리거, (2) 자정/정오 → 시각,
        (3) 문두 모드-시점(잘 때) → 모드 트리거, (4) 문두 시간대(저녁엔) → 시간대 트리거.
        """
        clk = find_clock(text)
        if clk and not re.search(r"이후|이전|부터|까지|전에|사이", text):
            ce = clk["span"][1]
            cons = re.sub(r"^\s*(?:에는|엔|에|정각)\s*", "", text[ce:]).strip()
            return text[:ce].strip(), cons
        # 자정/정오(시 없는) 문두 → 시각 antecedent.
        m0 = re.match(r"\s*(자정|정오)(?:에는|엔|에)?\s+", text)
        if m0:
            return text[:m0.end()].strip(), text[m0.end():].strip()
        # 문두 모드 표면형(잘 때/슬립 모드 …) → 모드 antecedent.
        for surf in sorted(self.gz.mode_surfaces, key=len, reverse=True):
            i = text.find(surf)
            if i >= 0 and text[:i].strip() == "":
                e = i + len(surf)
                return text[:e].strip(), text[e:].strip()
        # 문두 시간대(저녁엔/주말 아침엔 …) → 시간대 antecedent.
        m = re.match(r"\s*(?:주말|평일|공휴일|휴일|주중)?\s*(새벽|아침|낮|저녁|밤)"
                     r"(?:에는|엔|에|이)?\s+", text)
        if m:
            e = m.end()
            return text[:e].strip(), text[e:].strip()
        # 명령만 있는 문장 등 — 전체를 액션으로 시도.
        return "", text

    def _split_by_sentinel(self, antecedent: str, frames):
        """센티넬(지속시간 프레임)을 독립 절로, 나머지는 연결어미로 분리."""
        clauses = []
        pieces = re.split(r"(\x00F\d+\x00)", antecedent)
        for p in pieces:
            p = p.strip()
            if not p:
                continue
            if _SENTINEL_RE.fullmatch(p):
                clauses.append(p)
            else:
                clauses.extend(_split_clauses(self.gz, p))
        return clauses

    # ================================================================
    # 다중 규칙쌍(subrules) 경로 (§2.1)
    # ================================================================
    def _find_action_boundary(self, region: list[str]) -> int:
        """pivot 사이 구간에서 규칙 경계(명령동사+'-고/-며')의 인덱스를 찾는다.

        경계까지(포함) = 앞 서브룰 액션존, 그 뒤 = 다음 서브룰 조건존. 경계가 없으면
        구간 전체를 액션으로 본다(마지막 인덱스 반환).
        """
        if not region:
            return -1
        for j in range(len(region) - 1, -1, -1):
            # #16: 후행 구두점 허용('끄고,'처럼 쉼표가 붙어도 액션 경계).
            tok = region[j].rstrip(",.…")
            if not ((tok.endswith("고") or tok.endswith("며"))
                    and any(h in tok for h in COMMAND_HINTS)):
                continue
            # §결함3: 바로 앞 토큰이 주격 조사(이/가)로 끝나는 '주어'면, 이 '-고/-며'는 다음
            # 서브룰 '조건'의 서술어(예: "환풍기가 가동하고")이지 액션 경계가 아니다. 목적격
            # (을/를) 등 비주격일 때만 액션 연결로 본다("거실조명을 … 켜주고"는 액션 유지).
            if j > 0 and self._is_subject_token(region[j - 1]):
                continue
            return j
        return len(region) - 1

    def _is_subject_token(self, tok: str) -> bool:
        """토큰이 주격 조사(이/가)로 끝나는 체언(주어)인가 — 조사 제거 후 명사 표면형 확인."""
        if not (tok.endswith("이") or tok.endswith("가")):
            return False
        return _is_noun_surface(self.gz, tok[:-1])

    def _parse_multi(self, text: str, frames, pivots):
        tokens = text.split()
        n = len(pivots)
        cond_zones: list[list[str]] = [[] for _ in range(n)]
        act_zones: list[list[str]] = [[] for _ in range(n)]

        # 주제절(맨 앞 X는/은) 추출 → default_area/default_target (모든 서브룰 공용 후보)
        first_ante = " ".join(tokens[: pivots[0] + 1])
        first_ante = self._extract_topic(first_ante)
        cond_zones[0] = first_ante.split()

        for i in range(1, n):
            region = tokens[pivots[i - 1] + 1: pivots[i]]
            b = self._find_action_boundary(region)
            act_zones[i - 1] = region[: b + 1] if region else []
            after = region[b + 1:] if region else []
            cond_zones[i] = after + [tokens[pivots[i]]]
        act_zones[n - 1] = tokens[pivots[n - 1] + 1:]

        subrules = []
        summaries = []
        first_area: Optional[str] = None
        self.inh_area = self.default_area  # 주제절 방을 상속 시드로
        for i in range(n):
            self.triggers = []
            self.conditions = []
            self.actions = []
            self.condition_mode = "and"
            self.rule_area = None
            if i > 0:
                self.default_area = self.inh_area
            chip_start = len(self.chips)
            ante = " ".join(cond_zones[i])
            self.ante_text = ante
            ante_clauses = self._split_by_sentinel(ante, frames) if ante.strip() else []
            self._process_antecedent(ante_clauses, frames)
            cons = " ".join(act_zones[i]).strip()
            cons_clauses = _split_clauses(self.gz, cons) if cons else []
            self._process_consequent(cons_clauses)
            # 이 서브룰에서 만든 칩의 slot_key 를 subrule 인덱스로 접두(충돌 방지)
            for c in self.chips[chip_start:]:
                if c.slot_key:
                    c.slot_key = f"subrules[{i}].{c.slot_key}"
            summaries.append(self._summary())
            if self.rule_area:
                if first_area is None:
                    first_area = self.rule_area
                self.inh_area = self.rule_area
            # §2.1: 이 서브룰의 (주) 트리거/조건 센서를 저장 → 뒤 서브룰의 생략된 조건이 재참조.
            # 액션 대상 area(inh_area)와 분리해, 트리거 센서 자체를 물려준다.
            sensor_eid = next(
                (nd.get("entity_id")
                 for nd in list(self.triggers) + list(self.conditions)
                 if nd.get("type") in ("state", "state_held", "numeric_state")
                 and nd.get("entity_id")), None)
            if sensor_eid:
                self.inh_trigger_entity = sensor_eid
                se = self.gz.entity(sensor_eid)
                self.inh_trigger_area = se.get("area_id") if se else None
            subrules.append({"triggers": self.triggers,
                             "condition_mode": self.condition_mode,
                             "conditions": self.conditions,
                             "actions": self.actions})
        self._assign_spans()
        return self._emit_multi(subrules, summaries, first_area)

    def _emit_multi(self, subrules, summaries, area_id):
        model = {"alias": self.sentence.strip(), "description": "",
                 "mode": "single", "subrules": subrules}
        errors = self._light_validate_subrules(subrules)
        has_unresolved = any(c.status == "unresolved" for c in self.chips)
        ok = (not errors) and (not has_unresolved)
        scores = [c.candidates[0]["score"] for c in self.chips if c.candidates]
        base = sum(scores) / len(scores) if scores else 0.0
        if has_unresolved:
            base *= 0.4
        n_uncertain = sum(1 for c in self.chips if c.status == "uncertain")
        base *= (0.9 ** n_uncertain)
        confidence = round(min(base, 1.0), 3) if scores else 0.0
        category = self._category_of([a for sr in subrules for a in sr["actions"]])
        summary = " 그리고 ".join(s for s in summaries if s)
        for e in errors:
            self.warnings.append(e)
        return {"ok": ok, "model": model, "chips": [c.as_dict() for c in self.chips],
                "summary": summary, "area_id": area_id, "category": category,
                "unmatched": self.unmatched, "confidence": confidence,
                "warnings": self.warnings, "subrules_count": len(subrules)}

    def _light_validate_subrules(self, subrules) -> list[str]:
        errs = []
        for i, sr in enumerate(subrules):
            if not sr["triggers"]:
                errs.append(f"{i+1}번째 규칙의 실행 조건(트리거)을 찾지 못했어요.")
            if not sr["actions"]:
                errs.append(f"{i+1}번째 규칙의 실행할 동작을 찾지 못했어요.")
            for t in sr["triggers"]:
                if t.get("type") in ("state", "state_held", "numeric_state", "zone") \
                        and not t.get("entity_id") and "scope" not in t:
                    errs.append(f"{i+1}번째 규칙의 트리거 대상을 확정하지 못했어요.")
        return errs

    # ---- 절이 이벤트(→트리거 후보)인가 ----
    def _clause_is_event(self, clause: str) -> bool:
        # #11: 방 입실/통과(들어가/지나가/방+가)도 이벤트(그 방 모션).
        if _room_enter_motion_area(self.gz, clause) is not None:
            return True
        # B1/B6: 귀가·외출·누수 표현도 이벤트로 인식.
        if _ZONE_ENTER_RE.search(clause) or _ZONE_LEAVE_RE.search(clause):
            return True
        if _LEAK_RE.search(clause):
            return True
        if any(k in clause for k in EVENT_KEYWORDS):
            return True
        # #11(P2): 결과상('켜져 있으면')·잠금해제('풀리면')도 상태 이벤트 후보로 인정
        #   ('켜져'≠'켜지'라 EVENT_KEYWORDS 로는 안 잡힘). aspect 라우팅이 트리거/조건을 결정.
        if _RESULTATIVE_RE.search(clause) or _UNLOCK_RE.search(clause):
            return True
        if re.search(r"움직임|모션|인기척|동작|움직이", clause):
            return True
        if "사람" in clause and re.search(r"있|없|감지|오", clause):
            return True
        return False

    # ---- 모드 표면형 탐지(§6): 트리거 mode(to) / 조건 mode(state) ----
    def _detect_mode(self, clause: str):
        """절에서 모드 표면형을 찾아 (kind, canonical_name, on/off) 반환. 없으면 None.

        '슬립모드가 켜지면'→(trigger,이름,on), '꺼지면'→(trigger,이름,off),
        '슬립모드이고/면/일 때'→(condition,이름,on), '아니면/아니고'→(condition,이름,off).
        """
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
        # 조건 표지(일 때/에서/이면서/인 경우/중) → 모드 조건(다른 이벤트와 병존).
        # 기존 v3 기본값(모드 → condition)을 유지하되 표지를 우선 판정한다.
        if re.search(r"일\s*때|일때|에서|이면서|인\s*경우|인경우|중이|중일", tail):
            return ("condition", name, "off" if "아니" in tail else "on")
        # A2: 모드 해제 계열 표현은 off 트리거(켜지/꺼지 체크보다 앞).
        if re.search(r"해제|취소|종료|풀리|풀려|해지", tail):
            return ("trigger", name, "off")
        if "꺼지" in tail:
            return ("trigger", name, "off")
        if "켜지" in tail:
            return ("trigger", name, "on")
        if "아니" in tail:
            return ("condition", name, "off")
        # enabler: 모드 트리거 어미 확장 — 되면/들어가/진입/시작(전환) → 모드 트리거(on).
        if re.search(r"되면|들어가|진입|시작|전환", tail):
            return ("trigger", name, "on")
        # #7(충돌점): 오버레이는 bare '모드+면'을 트리거로 폴백하나, 앱은 복합절('슬립모드이고
        #   모션 작동하면')에서 모드=조건·이벤트=트리거 불변식(test_multiclause golden_b)을 지킨다.
        #   같은 절에 이벤트가 함께 있으면 강등 규칙이 절 단위로 동작하지 못하므로, 앱 불변식을
        #   우선해 기본값을 condition 으로 유지한다(APP-PORT-PLAN §5.1 리스크2 규칙).
        return ("condition", name, "on")

    def _emit_mode_trigger(self, name, state, clause):
        self.triggers.append({"type": "mode", "mode": name, "to": state})
        label = f"{name} 켜지면" if state == "on" else f"{name} 꺼지면"
        self.chips.append(_Chip(name, "trigger",
                                f"triggers[{len(self.triggers)-1}]",
                                [{"id": f"mode:{name}", "label": label,
                                  "sublabel": "모드 전환", "score": 1.0}],
                                self._span_of(name)))

    def _emit_mode_condition(self, name, state, clause):
        self.conditions.append({"type": "mode", "mode": name, "state": state})
        label = f"{name} 켜짐" if state == "on" else f"{name} 꺼짐"
        self.chips.append(_Chip(name, "condition",
                                f"conditions[{len(self.conditions)-1}]",
                                [{"id": f"mode:{name}", "label": label,
                                  "sublabel": "모드 조건", "score": 1.0}],
                                self._span_of(name)))

    def _process_antecedent(self, clauses, frames):
        """#8(P2): 상(aspect) 라우팅. 결과상/지속상 절은 조건, 전이형만 주 트리거 후보.
        트리거가 하나도 없을 때만 첫 상태 절을 진입에지 트리거로 승격(§4.1)."""
        held = [c for c in clauses if _SENTINEL_RE.fullmatch(c.strip())]
        other = [c for c in clauses if not _SENTINEL_RE.fullmatch(c.strip())]

        for c in held:
            self._build_held(c, frames)

        modes = [self._detect_mode(c) for c in other]
        event_clauses = [c for c, mi in zip(other, modes)
                         if self._clause_is_event(c) and not (mi and mi[0] == "trigger")]
        # 상태상(결과상/지속상) 절 vs 전이 절. 전이 절만 '주 트리거' 후보다.
        state_events = [c for c in event_clauses if _is_state_aspect(c)]
        trans_events = [c for c in event_clauses if c not in state_events]

        # #8 L1: 실제 이벤트(모션/상태/held/수치) 트리거가 있으면 문두 상태 모드('잘 때'·'슬립모드
        #   켜져 있고')는 트리거가 아니라 조건이다 → mode 'trigger'를 'condition'으로 강등.
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

        # 전이 이벤트: held 있으면 전부 조건, 없으면 마지막이 트리거.
        for i, c in enumerate(trans_events):
            as_trigger = (not held) and (i == len(trans_events) - 1)
            self._build_event_clause(c, as_trigger)

        # 상태상 절: 기본 조건. 트리거가 하나도 없으면 첫 상태 절만 진입에지 트리거로 승격(§4.1).
        for i, c in enumerate(state_events):
            promote = (not held) and (not self.triggers) and (i == 0)
            self._build_event_clause(c, promote)

        # #13 방 전파(catch-all): 대상-생략 액션이 방을 상속하도록, 트리거/조건에서 해석된 첫
        #   엔티티(방 있는 것)의 방을 기본 방으로 — 미설정일 때만.
        if self.default_area is None:
            for n in list(self.triggers) + list(self.conditions):
                eid = n.get("entity_id")
                if isinstance(eid, str) and "." in eid:
                    e = self.gz.entity(eid)
                    if e and e.get("area_id"):
                        self.default_area = e["area_id"]
                        break

    def _emit_calendar_aspect(self, clause):
        """주말/평일/공휴일 → day_type, 봄/여름/가을/겨울 → season 조건 (§3)."""
        dtypes = []
        for word, dt in DAY_TYPE_WORDS.items():
            if word in clause and dt not in dtypes:
                dtypes.append(dt)
        if dtypes:
            self.conditions.append({"type": "day_type", "types": dtypes})
            word = next(w for w in DAY_TYPE_WORDS if w in clause)
            self.chips.append(_Chip(word, "condition",
                                    f"conditions[{len(self.conditions)-1}]",
                                    [{"id": "day_type", "label": f"{word}에",
                                      "sublabel": "요일 구분", "score": 1.0}]))
        seasons = []
        for word, s in SEASON_WORDS.items():
            if word in clause and s not in seasons:
                seasons.append(s)
        if seasons:
            self.conditions.append({"type": "season", "seasons": seasons})
            word = next(w for w in SEASON_WORDS if w in clause)
            self.chips.append(_Chip(word, "condition",
                                    f"conditions[{len(self.conditions)-1}]",
                                    [{"id": "season", "label": f"{word}에",
                                      "sublabel": "계절 조건", "score": 1.0}]))

    def _emit_time_aspect(self, clause):
        # B4: 자정/정오(시 없는 표현) → daily 트리거(00:00 / 12:00).
        if not find_clock(clause):
            if "자정" in clause:
                self._build_daily_trigger({"hhmm": "00:00", "text": "자정", "span": (0, 2)})
                return
            if "정오" in clause:
                self._build_daily_trigger({"hhmm": "12:00", "text": "정오", "span": (0, 2)})
                return
        # B4: 시각 범위(A시부터 B시까지/사이) → time 조건(after A & before B).
        rng = _time_range(clause)
        if rng:
            self.conditions.append(rng)
            self.chips.append(_Chip(
                "시간범위", "condition", f"conditions[{len(self.conditions)-1}]",
                [{"id": "time_range", "label": f"{rng['after'][:5]}~{rng['before'][:5]}",
                  "sublabel": "시간 범위", "score": 1.0}]))
            return
        clk = find_clock(clause)
        if clk:
            after = re.search(r"이후|부터|넘|지나", clause)
            before = re.search(r"이전|까지|전에", clause)
            # markerless 시각(밤 11시엔/밤 9시가 되면/매일 …)은 daily 트리거,
            # '이후/이전/부터/까지' 범위표지가 있으면 time 조건(§3, B4).
            is_daily = (not after and not before)
            if is_daily:
                self._build_daily_trigger(clk)
            else:
                self._build_time_condition(clause)
            return
        for seg in SEGMENT_WORDS:
            if seg in clause:
                # A8: 전환 표현(세그먼트어+되면/시작/전환)이면 트리거로 승격. 다른 서술어의
                # '되면'(감지되면 등)에는 반응하지 않는다. 이벤트가 전혀 없으면 단독 시간대
                # 트리거로 승격.
                transition = re.search(
                    re.escape(seg) + r"(?:이|가)?\s*(?:되면|되\b|시작하|넘어가|바뀌|전환)",
                    clause)
                has_event = bool(re.search(
                    r"움직|모션|인기척|동작|감지|열리|닫히|도착|왔|들어|사람|누수|귀가|외출",
                    clause)) or bool(find_clock(clause))
                if transition or not has_event:
                    self._build_segment_trigger(clause)
                else:
                    self._build_segment_condition(clause)
                return

    def _emit_numeric_aspect(self, clause, as_trigger=False):
        # #14: 비교어에 도달|닿|찍(임계 도달) 추가 + between('N에서 M 사이').
        if not (re.search(_NUM_CMP_RE, clause) or _between(clause)):
            return
        # 센서 개념(온도/습도)이 없어도 'N도' 온도 리터럴이 있거나 앞 서브룰 센서를 물려받을
        # 수 있으면 진행(F1 + §2.1 재참조, '100을 넘으면 켜줘').
        if _find_concept(clause) is None and find_temperature(clause) is None \
                and not self.inh_trigger_entity:
            return
        self._build_numeric(clause, as_trigger=as_trigger)

    def _build_event_clause(self, clause, as_trigger):
        # #11: 방 입실/통과(들어가/지나가/방+가) → 그 방 모션(집 도착 zone·다인 도착보다 먼저).
        #   앱의 _build_arrival(다인 도착) 분기는 유지하고 앞에 이 분기만 삽입.
        if _room_enter_motion_area(self.gz, clause) is not None:
            self._build_motion(clause, as_trigger)
            return
        # B1: 외출(leave) 우선.
        if _ZONE_LEAVE_RE.search(clause):
            self._build_zone(clause, "leave", as_trigger)
            return
        # 도착/왔/들어오 → 기존 다인 도착 로직 유지(마지막 사람=트리거, 앞 사람=조건).
        if "도착" in clause or "왔" in clause or "들어오" in clause:
            self._build_arrival(clause, as_trigger)
            return
        # B1: 그 외 귀가 표현(귀가/퇴근/집에 오·와·돌아) → zone enter.
        if _ZONE_ENTER_RE.search(clause):
            self._build_zone(clause, "enter", as_trigger)
            return
        # B6: 누수/침수 → moisture 센서 이벤트.
        if _LEAK_RE.search(clause):
            self._build_leak(clause, as_trigger)
            return
        if re.search(r"움직임|모션|인기척|동작|사람|움직이", clause):
            self._build_motion(clause, as_trigger)
            return
        self._build_state_event(clause, as_trigger)

    def _build_zone(self, clause, event, as_trigger):
        """B1: 귀가/외출 zone 트리거(person 매칭, 미검출 시 기본 사용자)."""
        persons = _find_persons(self.gz, clause)
        if not persons:
            pid = (self.settings.get("persons") or {}).get("나") or "person.user"
            persons = [("나", pid)]
        if as_trigger:
            for surf, pid in persons:
                self.triggers.append({"type": "zone", "entity_id": pid,
                                      "zone": "zone.home", "event": event})
                self.chips.append(_Chip(
                    surf, "trigger", f"triggers[{len(self.triggers)-1}].entity_id",
                    [{"id": pid, "label": surf,
                      "sublabel": "집 도착" if event == "enter" else "외출",
                      "score": 1.0}]))
        else:
            state = (self.settings.get("near_home") or {}).get("zone_state", "home")
            for surf, pid in persons:
                self._person_state_condition(surf, pid, state)

    def _build_leak(self, clause, as_trigger):
        """B6: 누수/침수 → binary_sensor(moisture) 상태 이벤트."""
        concept = {"domain": "binary_sensor", "device_class": "moisture", "label": "누수"}
        area = _find_area(self.gz, clause) or self.default_area
        cands = self.gz.resolve_concept(concept, area, clause)
        slot = (f"triggers[{len(self.triggers)}].entity_id" if as_trigger
                else f"conditions[{len(self.conditions)}].entity_id")
        chip = self._chip("누수", "trigger" if as_trigger else "condition", slot, cands)
        if as_trigger:
            self.triggers.append({"type": "state", "entity_id": chip.chosen, "to": "on"})
        else:
            self.conditions.append({"type": "state", "entity_id": chip.chosen, "state": "on"})

    # ---- held(지속) → state_held / group_held ----
    def _build_held(self, sentinel, frames):
        idx = int(_SENTINEL_RE.search(sentinel).group(1))
        fr = frames[idx]
        to = "off" if fr["neg"] else "on"
        dur = to_duration_obj(fr["seconds"])
        subj = fr["subj"]
        # '다른 곳(은|에는)' → group_held (except_area). '다른' 판정은 트리거/조건 존에
        # 한정 — 액션 존의 "다른 조명을" 이 트리거를 뒤바꾸지 않도록 antecedent만 본다.
        is_group = "다른" in self.ante_text
        motion = _find_concept(subj)
        if motion is None and ("움직임" in subj or "모션" in subj or "인기척" in subj):
            motion = MOTION_CONCEPT
        if is_group:
            # except_area: 문맥에서 언급된 방(침실/안방 등)
            except_area = self._context_area_for_group()
            scope = {"device_class": MOTION_CONCEPT["device_class"], "domain": None,
                     "area_id": None, "except_area_id": except_area}
            node = {"type": "group_held", "scope": scope, "to": to, "for": dur}
            self.triggers.append(node)
            label = "다른 곳 움직임 없음" if to == "off" else "다른 곳 움직임"
            self.chips.append(_Chip(subj or "다른 곳", "trigger",
                                    f"triggers[{len(self.triggers)-1}].scope",
                                    [{"id": "scope:motion", "label": label,
                                      "sublabel": "모션 센서 전체", "score": 0.85}]))
        else:
            explicit_area = _find_area(self.gz, fr.get("loc", "")) \
                or _find_area(self.gz, subj)
            concept = motion or MOTION_CONCEPT
            slot = f"triggers[{len(self.triggers)}].entity_id"
            # §2.1: 방이 명시되지 않은 생략형 조건("모션이 없으면")은 앞 서브룰에서 언급된
            # 트리거/조건 센서 엔티티를 그대로 재참조한다(액션 대상 area 상속과 분리).
            reuse = self._inherited_sensor(concept) if explicit_area is None else None
            if reuse:
                e = self.gz.entity(reuse)
                nm = (e.get("name") if e else None) or subj
                self.chips.append(_Chip(nm, "trigger", slot,
                                        [{"id": reuse, "label": nm,
                                          "sublabel": "앞 규칙 센서", "score": 0.9}],
                                        self._span_of(subj)))
                eid = reuse
            else:
                area = explicit_area or self.default_area
                cands = self.gz.resolve_concept(concept, area, subj)
                chip = self._chip(subj, "trigger", slot, cands)
                eid = chip.chosen
            node = {"type": "state_held", "entity_id": eid, "to": to, "for": dur}
            self.triggers.append(node)

    def _inherited_sensor(self, concept) -> Optional[str]:
        """앞 서브룰의 트리거/조건 센서 중 개념(device_class)이 맞으면 그 entity_id 재참조(§2.1).

        생략된 조건 절이 앞 절에서 실제 등장한 센서를 다시 가리키게 한다. 개념이 맞지 않으면
        (예: 앞은 도어센서, 여기선 모션) 재참조하지 않고 None → area 기반 해석으로 폴백.
        """
        eid = self.inh_trigger_entity
        if not eid:
            return None
        e = self.gz.entity(eid)
        if not e:
            return None
        dc = concept.get("device_class")
        dc_set = set(dc) if isinstance(dc, list) else ({dc} if dc else None)
        if dc_set is not None and e.get("device_class") not in dc_set:
            return None
        return eid

    def _context_area_for_group(self):
        # 문장 안의 방 표면형 중 '다른 곳'이 아닌 실제 방 → except_area
        for surf, aid in sorted(self.gz.room_surfaces.items(), key=lambda x: -len(x[0])):
            if surf in self.sentence:
                return aid
        return None

    # ---- 이벤트 → 트리거/조건 ----
    def _build_event(self, clause, kind, as_trigger):
        # 도착(사람 위치)
        if "도착" in clause or "왔" in clause or "들어오" in clause:
            self._build_arrival(clause, as_trigger)
            return
        # 모션
        if kind == "motion":
            self._build_motion(clause, as_trigger)
            return
        # 문 열림/닫힘 등 상태 이벤트
        self._build_state_event(clause, as_trigger)

    def _build_arrival(self, clause, as_trigger):
        persons = _find_persons(self.gz, clause)
        or_persons = bool(re.search(r"또는|이나\b", clause))
        state = (self.settings.get("near_home") or {}).get("zone_state", "home")
        if not persons:
            self.unmatched.append(clause.strip())
            return
        if as_trigger:
            if or_persons:
                targets = persons
                cond_persons = []
            else:
                targets = [persons[-1]]
                cond_persons = persons[:-1]
            for surf, pid in targets:
                node = {"type": "zone", "entity_id": pid, "zone": "zone.home",
                        "event": "enter"}
                self.triggers.append(node)
                self.chips.append(_Chip(surf, "trigger",
                                        f"triggers[{len(self.triggers)-1}].entity_id",
                                        [{"id": pid, "label": surf, "sublabel": "집 도착",
                                          "score": 1.0}]))
            for surf, pid in cond_persons:
                self._person_state_condition(surf, pid, state)
        else:
            for surf, pid in persons:
                self._person_state_condition(surf, pid, state)

    def _person_state_condition(self, surf, pid, state):
        node = {"type": "state", "entity_id": pid, "state": state}
        self.conditions.append(node)
        self.chips.append(_Chip(surf, "condition",
                                f"conditions[{len(self.conditions)-1}].entity_id",
                                [{"id": pid, "label": surf, "sublabel": "집에 있음",
                                  "score": 1.0}]))

    def _build_motion(self, clause, as_trigger):
        neg = "없" in clause
        to = "off" if neg else "on"
        area = _find_area(self.gz, clause) or self.default_area
        # #12: 모션 방을 기본 방으로 전파(미설정일 때만) — 뒤따르는 대상-생략 기기어
        #      ('거실 인기척 있으면 무드 조명 켜')가 같은 방을 상속하게 한다.
        if area and self.default_area is None:
            self.default_area = area
        cands = self.gz.resolve_concept(MOTION_CONCEPT, area, clause)
        if as_trigger:
            slot = f"triggers[{len(self.triggers)}].entity_id"
            chip = self._chip("움직임", "trigger", slot, cands, self._span_of("움직임"))
            self.triggers.append({"type": "state", "entity_id": chip.chosen, "to": to})
        else:
            slot = f"conditions[{len(self.conditions)}].entity_id"
            chip = self._chip("움직임", "condition", slot, cands, self._span_of("움직임"))
            self.conditions.append({"type": "state", "entity_id": chip.chosen, "state": to})

    def _find_entity_in_clause(self, clause):
        """절 안에서 가장 긴 엔티티 이름 표면형을 찾는다."""
        best = None
        best_len = 0
        for surf, ids in self.gz.entity_surfaces.items():
            if surf in clause and len(surf) > best_len:
                best, best_len = (surf, ids), len(surf)
        return best

    def _build_state_event(self, clause, as_trigger):
        """#10(P2): 도메인·부정·결과상 극성을 반영한 상태 이벤트.

        - 상태값을 `_aspect_state` 로 계산: cover=open/closed, lock=unlocked/locked, 그 외 on/off.
        - '안/못' 부정과 결과상(꺼져/닫혀/잠겨)·잠금해제(풀리)를 정확히 반영, bare 문/창→door/window,
          앞 서브룰 센서 재참조, 도어센서+잠금표현→같은 방 lock 재매핑, 방 전파, 제어대상 상속.
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
            concept = _find_concept(clause)
            area = _find_area(self.gz, clause) or self.default_area
            # #10 L1: 명명 엔티티도 기기개념도 없는 개폐 이벤트의 bare '문/창(문)' → door/window 센서.
            if concept is None and re.search(r"열리|열려|열린|열|닫히|닫혀|닫힌|닫", clause):
                if re.search(r"창문|창(?!고)", clause):
                    concept = {"domain": "binary_sensor", "device_class": "window", "label": "창문"}
                elif "문" in clause:
                    concept = {"domain": "binary_sensor", "device_class": "door", "label": "문"}
            cands = self.gz.resolve_concept(concept, area, clause) if concept else []
            chip = self._chip(clause.strip(), role, slot, cands)
        eid = chip.chosen
        # §2.1: 개폐 대상 없는 뒤 서브룰 전이('닫히면'/'열리면')는 앞 서브룰 센서 재참조.
        if eid is None and self.inh_trigger_entity:
            eid = self.inh_trigger_entity
        # #10(P2): 도어센서에 잠금/해제 표현이 붙으면 같은 방의 lock 으로 재매핑.
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
        # #10/#13 방 전파: 해석된 엔티티의 방을 기본 방으로(미설정일 때만).
        if self.default_area is None and eid:
            e2 = self.gz.entity(eid)
            prop_area = (e2.get("area_id") if e2 else None) or _find_area(self.gz, clause)
            if prop_area:
                self.default_area = prop_area
        if as_trigger:
            self.triggers.append({"type": "state", "entity_id": eid, "to": st})
        else:
            self.conditions.append({"type": "state", "entity_id": eid, "state": st})
        # #10(P2): 제어 가능한 상태 대상은 뒤따르는 대상-생략 액션이 물려받게 한다.
        if eid and eid.split(".")[0] in (
                "light", "switch", "fan", "media_player", "climate", "cover", "lock"):
            self.inh_action_entity = eid

    # ---- numeric_state (조건 또는 트리거) ----
    def _build_numeric(self, clause, as_trigger=False):
        concept = _find_concept(clause)
        area = _find_area(self.gz, clause) or self.default_area
        temp = find_temperature(clause)
        num = None
        if temp:
            num = temp["value"]
        else:
            m = re.search(r"(\d+(?:\.\d+)?)", clause)
            if m:
                num = float(m.group(1))
        # 영하/마이너스 → 음수.
        if num is not None and re.search(r"영하|마이너스", clause):
            num = -abs(num)
        above = below = None
        btw = _between(clause)
        if btw:
            above, below = btw            # #14: '20도에서 24도 사이' → above 20 + below 24
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
        # #14/§2.1: 개념 없이 수치만 있는 뒤 서브룰('100을 넘으면 켜줘')은 앞 서브룰 센서 재참조.
        if eid is None and self.inh_trigger_entity:
            eid = self.inh_trigger_entity
        node = {"type": "numeric_state", "entity_id": eid}
        if above is not None:
            node["above"] = above
        if below is not None:
            node["below"] = below
        bucket.append(node)

    # ---- time 조건 ----
    def _build_time_condition(self, clause):
        clk = find_clock(clause)
        if not clk:
            return
        after = re.search(r"이후|부터|넘|지나", clause)
        before = re.search(r"이전|까지|전에", clause)
        node = {"type": "time"}
        hhmmss = clk["hhmm"] + ":00"
        if before and not after:
            node["before"] = hhmmss
        else:
            node["after"] = hhmmss
        self.conditions.append(node)
        self.chips.append(_Chip(clk["text"], "condition",
                                f"conditions[{len(self.conditions)-1}]",
                                [{"id": "time", "label": f"{clk['hhmm']} 기준",
                                  "sublabel": "시각 조건", "score": 1.0}]))

    def _build_daily_trigger(self, clk):
        self.triggers.append({"type": "daily", "at": clk["hhmm"]})
        self.chips.append(_Chip(clk["text"], "trigger",
                                f"triggers[{len(self.triggers)-1}]",
                                [{"id": "daily", "label": f"매일 {clk['hhmm']}",
                                  "sublabel": "매일 정시", "score": 1.0}],
                                self._span_of(clk["text"])))

    def _build_segment_trigger(self, clause):
        segs = [seg for word, seg in SEGMENT_WORDS.items() if word in clause]
        if not segs:
            return
        seg = segs[0]
        self.triggers.append({"type": "segment", "to": seg})
        word = [w for w, s in SEGMENT_WORDS.items() if s == seg][0]
        self.chips.append(_Chip(word, "trigger",
                                f"triggers[{len(self.triggers)-1}]",
                                [{"id": seg, "label": word + "이(가) 되면",
                                  "sublabel": "시간대 전환", "score": 1.0}],
                                self._span_of(word)))

    def _build_segment_condition(self, clause):
        segs = [seg for word, seg in SEGMENT_WORDS.items() if word in clause]
        if not segs:
            return
        seg = segs[0]
        self.conditions.append({"type": "time_segment", "segments": [seg]})
        word = [w for w, s in SEGMENT_WORDS.items() if s == seg][0]
        self.chips.append(_Chip(word, "condition",
                                f"conditions[{len(self.conditions)-1}]",
                                [{"id": seg, "label": word + " 시간대",
                                  "sublabel": "시간대 조건", "score": 1.0}]))

    # ---- 액션 절 처리 ----
    def _route_condition_segment(self, seg: str):
        """#9: 지속상 조건부 세그먼트를 수치/모션/상태 조건으로 방출(as_trigger=False).
        세그먼트에 방이 안 적혀 있으면 트리거 엔티티의 방을 임시 기본값으로 써 해석한다."""
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

    def _process_consequent(self, clauses):
        for clause in clauses:
            seg, rest = _split_durative_condition(clause)
            # #9: 지속상 조건은 트리거가 이미 있을 때만 떼어낸다(순수 명령문 보호).
            if seg is not None and self.triggers:
                self._route_condition_segment(seg)
                if rest.strip():
                    self._build_action(rest)
            else:
                self._build_action(clause)

    def _build_action(self, clause):
        # 모드 전환("슬립 모드로 바꿔/켜/꺼")
        for name, spec in self.gz.mode_surfaces.items():
            if name in clause:
                canon = self.gz.mode_canonical.get(name, name)
                # A9: off 표현에 취소|풀|해지|종료 추가.
                to = "off" if re.search(r"꺼|끄|해제|취소|풀|해지|종료|off", clause) else "on"
                # v3 신형 모드 스펙(initial/on_action/off_action) → set_mode 노드(side-effect는
                # 엔진 담당). 구형 스펙({action,target,data})은 하위호환으로 service 노드 유지.
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
                self.chips.append(_Chip(name, "action",
                                        f"actions[{len(self.actions)-1}]",
                                        [{"id": f"mode:{canon}", "label": label,
                                          "sublabel": "모드 전환", "score": 1.0}]))
                return

        # 지연: "N(초|분|시간) (뒤|후|있다가|이따가)에" → delay 액션을 먼저 삽입(§3).
        dm = re.search(r"(\d+)\s*(초|분|시간)\s*(?:뒤|후|있다가|이따가?)\s*에?", clause)
        if dm:
            unit = {"초": 1, "분": 60, "시간": 3600}
            secs = int(dm.group(1)) * unit[dm.group(2)]
            self.actions.append({"type": "delay", "duration": to_duration_obj(secs)})
            self.chips.append(_Chip(dm.group(0).strip(), "value",
                                    f"actions[{len(self.actions)-1}].duration",
                                    [{"id": "delay", "label": dm.group(0).strip(),
                                      "sublabel": "지연 후 실행", "score": 1.0}],
                                    self._span_of(dm.group(0).strip())))
            clause = (clause[:dm.start()] + " " + clause[dm.end():]).strip()

        # 명령 판정 (B6: 잠가/잠그/차단/소등 → off. lock 도메인은 _domain_service 에서 처리.)
        # #15①: '잠근'(잠근 채로 둬)도 off.
        turn_off = bool(re.search(r"꺼|끄|멈춰|정지|닫아|잠가|잠그|잠궈|잠근|차단|소등", clause))
        # B1: 잠금 풀어/해제 → unlock(=turn_on). '풀리'(모드 트리거)와 구분해 액션 존만.
        turn_on = bool(re.search(r"켜|틀|열어|가동|작동|실행|바꿔|풀어|풀고|해제|점등|밝게",
                                 clause)) and not turn_off
        # 값(B2: 절반/최대/약하게/단위없는 N으로 확장) — find_percent 상위집합.
        pct = _value_dict(clause)
        # 프리셋(…로/으로) — 모드 외 climate fan_mode 등
        preset = None
        pm = re.search(r"([가-힣A-Za-z0-9]+)\s*(?:으로|로)\s*(?:틀|켜|바꿔|설정)", clause)
        if pm:
            cand = pm.group(1)
            if not find_percent(cand) and cand not in ("그것", "거기"):
                preset = cand

        # B/스코프 확장: '전체/온 집/집 안/집 전체/모두 … 다' 전량 스코프(모든 외 표현).
        # 배제 스코프(빼고/제외/남기고)는 아래에서 처리하므로 여기선 제외.
        # #15④: '전부' 는 후행 한글이 없을 때만(전부다→전부 오매칭 방지).
        if (turn_on or turn_off) \
                and re.search(r"전체|전부(?![가-힣])|온\s*집|집\s*안|집안|집\s*전체|모두|온집", clause) \
                and not re.search(r"빼|제외|남기", clause):
            concept = _find_concept(clause) or {"domain": "light", "label": "조명"}
            ids = self.gz.entities_by_concept(concept)
            if ids:
                action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
                self.actions.append({"type": "service", "action": action,
                                     "target": {"entity_id": ids}})
                self.chips.append(_Chip(
                    "전체", "action", f"actions[{len(self.actions)-1}].target",
                    [{"id": f"all:{concept['domain']}",
                      "label": f"모든 {concept.get('label','')}", "sublabel": f"{len(ids)}개",
                      "score": 0.9}]))
                return

        # #15②: L1 후치 전량 스코프 '[기기] (싹) 다 꺼/켜' → 해당 도메인 전량. 방이 명시되면
        #   그 방 한정('거실 조명 다 꺼'→거실 라이트), 아니면 전역('불 싹 다 꺼'→집 전체).
        #   도메인 혼재(보일러/AC) 오검출 방지로 라이트(또는 개념 미검출)에만 안전 적용.
        if (turn_on or turn_off) \
                and re.search(r"싹|(?<![가-힣])다\s*(?:꺼|켜|끄|잠|닫|열|멈|내려|올려)", clause) \
                and not re.search(r"빼|제외|남기|말고", clause):
            concept = _find_concept(clause)
            if concept is None or concept.get("domain") == "light":
                concept = concept or {"domain": "light", "label": "조명"}
                area = _find_area(self.gz, clause)
                ids = self.gz.entities_by_concept(concept, area_id=area)
                if ids:
                    action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
                    self.actions.append({"type": "service", "action": action,
                                         "target": {"entity_id": ids}})
                    self.chips.append(_Chip(
                        "전체", "action", f"actions[{len(self.actions)-1}].target",
                        [{"id": f"all:{concept['domain']}",
                          "label": f"모든 {concept.get('label','')}", "sublabel": f"{len(ids)}개",
                          "score": 0.9}]))
                    return

        # #15③ B5 배제 스코프(다중): "A랑 B 빼고/제외하고/(만) 남기고/말고 (나머지) 다" → 해당
        #   도메인 전체에서 배제 방(들)·엔티티(들)를 뺀 목록. "나머지 스위치/콘센트"는 switch 도메인.
        #   요일 배제("주말 빼고")는 area/엔티티가 아니라 자연히 건너뛴다(weekday 후처리 담당).
        _ex_areas: list = []
        _ex_ids: list = []
        for em in re.finditer(
                r"((?:[가-힣]+\s*(?:이랑|랑|하고|과|와|,)\s*)*[가-힣]+)"
                r"\s*(?:만)?\s*(?:빼고|제외하고|남기고|말고)", clause):
            for w in re.split(r"\s*(?:이랑|랑|하고|과|와|,)\s*", em.group(1)):
                w = w.strip().rstrip("만")
                if not w:
                    continue
                a = _find_area(self.gz, w)
                if a:
                    if a not in _ex_areas:
                        _ex_areas.append(a)
                    continue
                nc = self.gz.resolve_name(strip_particles_simple(w))
                if nc and nc[0]["id"] not in _ex_ids:
                    _ex_ids.append(nc[0]["id"])
        if (_ex_areas or _ex_ids) and (turn_on or turn_off):
            if re.search(r"스위치|콘센트|전원", clause):
                concept = {"domain": "switch", "label": "스위치"}
            else:
                concept = _find_concept(clause) or {"domain": "light", "label": "조명"}
            all_ids = self.gz.entities_by_concept(concept)
            ids = [i for i in all_ids
                   if (self.gz.entity(i) or {}).get("area_id") not in _ex_areas
                   and i not in _ex_ids]
            if ids:
                action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
                self.actions.append({"type": "service", "action": action,
                                     "target": {"entity_id": ids}})
                self.chips.append(_Chip(
                    "제외", "action", f"actions[{len(self.actions)-1}].target",
                    [{"id": f"except:{concept['domain']}",
                      "label": f"제외 {concept.get('label','')}", "sublabel": f"{len(ids)}개",
                      "score": 0.9}]))
                return

        # A3/A10: 선두 스코프어(모든/집의 모든/전부/다) → 전량 스코프. 기기어가 있으면 그
        # 도메인 전체, 없으면 조명 전체(A10). "다른/다시" 는 _SCOPE_RE 의 (?=\s) 로 배제.
        sm = _SCOPE_RE.match(clause)
        if sm and (turn_on or turn_off):
            concept = _find_concept(clause)
            if concept is None:
                concept = {"domain": "light", "label": "조명"}  # A10
            ids = self.gz.entities_by_concept(concept)
            action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
            self.actions.append({"type": "service", "action": action,
                                 "target": {"entity_id": ids}})
            self.chips.append(_Chip(
                sm.group(0).strip() or "전체", "action",
                f"actions[{len(self.actions)-1}].target",
                [{"id": f"all:{concept['domain']}",
                  "label": f"모든 {concept.get('label','')}",
                  "sublabel": f"{len(ids)}개", "score": 0.9}]))
            return

        # 대상들(체언 병렬: 와/과/하고/,)
        targets, unresolved_targets = self._split_targets(clause)
        # §결함2: 대상이 명시됐지만(목적격) 어휘에 없으면 → 조용히 앞 서브룰 대상으로 상속하지
        # 말고 unresolved 로 내려 사용자 확정을 유도한다(상속은 대상 명사가 전혀 없을 때만).
        if not targets and unresolved_targets:
            for u in unresolved_targets:
                self._emit_unresolved_target(u)
            return
        if not targets and self.default_target:
            targets = [self.default_target.get("text", "")]
        # §2.1 컨텍스트 상속: 대상이 생략되면 앞 서브룰의 마지막 액션 대상을 물려받는다.
        if not targets and self.inh_action_entity and (turn_on or turn_off):
            self._emit_inherited_action(clause, turn_on, turn_off, pct)
            return

        made = False
        for t in targets:
            self._emit_service(t, clause, turn_on, turn_off, pct, preset)
            made = True
        if not made:
            self.unmatched.append(clause.strip())

    def _emit_inherited_action(self, clause, turn_on, turn_off, pct):
        """대상 생략 액션이 앞 서브룰의 액션 대상을 물려받아 서비스를 방출(§2.1)."""
        eid = self.inh_action_entity
        e = self.gz.entity(eid)
        domain = e["domain"] if e else eid.split(".", 1)[0]
        self._domain_service(domain, eid, turn_on, turn_off, pct, None, clause)
        nm = (e.get("name") if e else None) or eid
        self.chips.append(_Chip(nm, "action",
                                f"actions[{len(self.actions)-1}].target",
                                [{"id": eid, "label": nm, "sublabel": "앞 규칙 대상",
                                  "score": 0.8}]))

    def _emit_unresolved_target(self, text: str):
        """대상으로 명시됐으나 미해석된 토큰 → unresolved 칩 + unmatched(ok=False 유도, §결함2)."""
        slot = f"actions[{len(self.actions)}].target"
        self.chips.append(_Chip(text, "action", slot, []))
        self.unmatched.append(text)

    def _split_targets(self, clause: str):
        """액션 절에서 표적 명사들 추출(병렬 조사로 분리). 반환 (resolved, unresolved).

        - resolved: 어휘에 매칭된 대상 표면형.
        - unresolved: 대상 자리에 명시됐지만(목적격 을/를) 어휘에 없는 토큰(§결함2). 이게 있고
          resolved 가 비면 앞 서브룰 대상 상속을 막고 사용자 확정을 유도한다. '대상 토큰 없음'
          (병렬 분리 결과가 전부 빈/조사)과 '토큰 있으나 미해석'을 이렇게 구분한다.
        """
        # '모든 X'
        if "모든" in clause:
            return [clause[clause.find("모든"):]], []
        # 조사로 분리
        body = clause
        for cmd in COMMAND_HINTS:
            body = re.sub(cmd + r".*$", "", body)
        # 병렬: 와/과/하고/이랑/랑/, 로 분리
        parts = re.split(r"(?:와|과|하고|이랑|랑|,)\s*", body)
        out = []
        unresolved = []
        for raw in parts:
            raw = raw.strip()
            if not raw:
                continue
            p = strip_particles_simple(raw)
            if not p:
                continue
            # 방/기기/엔티티가 있는지
            if _find_concept(p) or self.gz.resolve_name(strip_particles_simple(p)) \
                    or _find_area(self.gz, p):
                out.append(p)
            elif self._looks_like_target(raw):
                # 목적격으로 명시된 대상인데 어휘에 없음 → 조용히 버리지 말 것(§결함2).
                unresolved.append(p)
        return out, unresolved

    @staticmethod
    def _looks_like_target(raw: str) -> bool:
        """토큰이 '대상 자리'의 명사인가 — 목적격 표지(을/를)가 있으면 명시된 대상으로 본다."""
        return raw.endswith("을") or raw.endswith("를")

    def _emit_service(self, target_text, clause, turn_on, turn_off, pct, preset):
        area = _find_area(self.gz, target_text) or self.default_area
        if self.default_target and self.default_target.get("area"):
            area = area or self.default_target["area"]
        # '모든 X' 스코프
        scope_all = "모든" in target_text
        concept = _find_concept(target_text)
        name_cands = self.gz.resolve_name(strip_particles_simple(target_text))

        if scope_all and concept:
            ids = self.gz.entities_by_concept(concept)
            action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
            self.actions.append({"type": "service", "action": action,
                                 "target": {"entity_id": ids}})
            self.chips.append(_Chip(target_text.strip(), "action",
                                    f"actions[{len(self.actions)-1}].target",
                                    [{"id": f"all:{concept['domain']}",
                                      "label": f"모든 {concept.get('label','')}",
                                      "sublabel": f"{len(ids)}개", "score": 0.9}]))
            return

        # '다른 X' (액션 존): 문맥 방을 제외한 해당 도메인 전체로 확장. 문맥 방을
        # 특정할 수 없으면 슬롯을 unresolved로 내려 사용자 확정을 유도(§결함4).
        if "다른" in target_text and concept:
            except_area = self.rule_area or self.default_area
            ids = (self.gz.entities_by_concept(concept, except_area_id=except_area)
                   if except_area else [])
            slot = f"actions[{len(self.actions)}].target"
            action = f"{concept['domain']}.turn_{'off' if turn_off else 'on'}"
            if ids:
                self.actions.append({"type": "service", "action": action,
                                     "target": {"entity_id": ids}})
                area_nm = self.gz.area_name(except_area) or "여기"
                self.chips.append(_Chip(target_text.strip(), "action",
                                        f"actions[{len(self.actions)-1}].target",
                                        [{"id": f"other:{concept['domain']}",
                                          "label": f"다른 {concept.get('label','')}",
                                          "sublabel": f"{area_nm} 제외 {len(ids)}개",
                                          "score": 0.85}]))
            else:
                self.actions.append({"type": "service", "action": action, "target": {}})
                self.chips.append(_Chip(target_text.strip(), "action", slot, []))
            return

        if name_cands:
            cands = name_cands
        elif concept:
            cands = self.gz.resolve_concept(concept, area, target_text)
        else:
            if self.default_target and self.default_target.get("concept"):
                cands = self.gz.resolve_concept(self.default_target["concept"],
                                                self.default_target.get("area") or area,
                                                target_text)
                concept = self.default_target["concept"]
            elif self.default_target and self.default_target.get("entity"):
                e = self.gz.entity(self.default_target["entity"])
                cands = [self.gz._cand(e, 0.95, "문장 주제")] if e else []
            else:
                cands = []

        slot = f"actions[{len(self.actions)}].target"
        chip = self._chip(target_text.strip() or "대상", "action", slot, cands)
        eid = chip.chosen
        if eid is None:
            self.actions.append({"type": "service", "action": "homeassistant.turn_on",
                                 "target": {}})
            return
        e = self.gz.entity(eid)
        domain = e["domain"] if e else eid.split(".", 1)[0]
        # 조명 "내려/낮춰/줄여"(밝기 낮추기)는 v2.0 미지원 → 미해석 처리(§결함1 주석).
        if domain == "light" and not turn_on and not turn_off and not pct \
                and re.search(r"내려|낮춰|줄여", clause):
            # 이미 만들어둔 액션 슬롯 칩(대상)이 있으면 제거하고 미해석으로.
            if self.chips and self.chips[-1].slot_key == slot:
                self.chips.pop()
            self.unmatched.append(target_text.strip() or clause.strip())
            return
        self._domain_service(domain, eid, turn_on, turn_off, pct, preset, clause)
        self.inh_action_entity = eid  # 다음 서브룰 대상 상속용(§2.1)
        if self.rule_area is None and e and e.get("area_id"):
            self.rule_area = e["area_id"]

    def _domain_service(self, domain, eid, turn_on, turn_off, pct, preset, clause):
        data = {}
        # B1/B6: '문 잠가/잠금 풀어' 처럼 도어센서에 잠금 동사가 오면 같은 방 lock 으로 재매핑.
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
            # B2: 값이 있고 켜/끄 명령이 아니면 set_percentage.
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
            tm = find_temperature(clause)
            if tm is not None and not turn_off and not turn_on \
                    and re.search(r"맞춰|맞추|맞게|설정|해줘|해\b|로\s*해", clause):
                self.actions.append({"type": "service",
                                     "action": "climate.set_temperature",
                                     "target": {"entity_id": [eid]},
                                     "data": {"temperature": int(tm["value"])}})
                return
            action = "climate.turn_off" if turn_off else "climate.turn_on"
        elif domain == "cover":
            # B2: 값이 있으면 위치 설정(절반만 열어 → position 50).
            if pct is not None:
                self.actions.append({"type": "service",
                                     "action": "cover.set_cover_position",
                                     "target": {"entity_id": [eid]},
                                     "data": {"position": pct["value"]}})
                return
            # 커튼/블라인드 동사: 내려/닫아/쳐/접어=close, 올려/열어/걷어/펼쳐=open.
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
                self.actions.append({"type": "service",
                                     "action": "media_player.volume_set",
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
        # climate 프리셋(쿨파워 등) → set_fan_mode 추가
        if domain == "climate" and preset:
            self.actions.append({"type": "service", "action": "climate.set_fan_mode",
                                 "target": {"entity_id": [eid]},
                                 "data": {"fan_mode": preset}})
            self.chips.append(_Chip(preset, "value",
                                    f"actions[{len(self.actions)-1}].data.fan_mode",
                                    [{"id": preset, "label": preset, "sublabel": "팬 모드",
                                      "score": 0.8}]))

    # ================================================================
    def _emit(self, frames):
        # condition_mode: 사람 OR 트리거가 여러 개면 그대로(HA OR). condition은 and 유지.
        model = {
            "alias": self.sentence.strip(),
            "description": "",
            "mode": "single",
            "triggers": self.triggers,
            "condition_mode": self.condition_mode,
            "conditions": self.conditions,
            "actions": self.actions,
        }
        # 내부 경량 검증 → ok
        errors = self._light_validate(model)
        has_unresolved = any(c.status == "unresolved" for c in self.chips)
        ok = (not errors) and (not has_unresolved)
        # confidence
        scores = [c.candidates[0]["score"] for c in self.chips if c.candidates]
        base = sum(scores) / len(scores) if scores else 0.0
        if has_unresolved:
            base *= 0.4
        n_uncertain = sum(1 for c in self.chips if c.status == "uncertain")
        base *= (0.9 ** n_uncertain)
        confidence = round(min(base, 1.0), 3) if scores else 0.0

        category = self._category()
        summary = self._summary()
        if errors:
            for e in errors:
                self.warnings.append(e)
        return {
            "ok": ok,
            "model": model,
            "chips": [c.as_dict() for c in self.chips],
            "summary": summary,
            "area_id": self.rule_area,
            "category": category,
            "unmatched": self.unmatched,
            "confidence": confidence,
            "warnings": self.warnings,
        }

    def _light_validate(self, model) -> list[str]:
        errs = []
        if not model["triggers"]:
            errs.append("실행 조건(트리거)을 찾지 못했어요.")
        if not model["actions"]:
            errs.append("실행할 동작을 찾지 못했어요.")
        for t in model["triggers"]:
            if t.get("type") in ("state", "state_held", "numeric_state", "zone") \
                    and not t.get("entity_id") and "scope" not in t:
                errs.append("트리거 대상을 확정하지 못했어요.")
        return errs

    _CAT_MAP = {"light": "lighting", "switch": "switch", "fan": "fan",
                "cover": "cover", "climate": "climate", "media_player": "media",
                "lock": "lock", "scene": "etc"}

    def _category(self):
        return self._category_of(self.actions)

    def _category_of(self, actions):
        for a in actions:
            act = a.get("action") or ""
            dom = act.split(".", 1)[0]
            if dom in self._CAT_MAP:
                return self._CAT_MAP[dom]
        return "etc"

    def _summary(self):
        # §S7: 요약 서술(트리거/조건/액션 → 한국어)은 파서·후처리 공용 모듈(summary)로
        # 단일화한다. 후처리(postpass)가 신규 노드(sun/weekday/presence/if 등)를 얹은 뒤
        # 같은 함수로 재생성하므로 확인 카드에 미서술 노드가 남지 않는다(APP-PORT-PLAN §1.3 S7).
        return summary.summarize_subrule(self.triggers, self.conditions,
                                         self.actions, self.gz)


def parse(sentence: str, gazetteer: Gazetteer, settings: dict,
          pins: Optional[dict] = None, now_fn=None) -> dict:
    """§6.2 진입점. 파이프라인: 표면정규화 → 금지문 게이트 → 규칙 파서 → 모델 후처리(§1.1).

    now_fn 은 신규 노드(sun/calendar) 결정성용으로 postpass 에 전달(S1 미사용). 원문 sentence
    는 alias·칩 스팬·인용 메시지 기준으로 보존하고, 파싱 텍스트만 normalized 를 쓴다.
    """
    normalized = surface.normalize_surface(sentence)
    # 금지문(자동화 요청 아님)은 파서 진입 전 차단 — 정반대 액션 생성 방지(안전 게이트).
    if surface.is_prohibition(normalized):
        return surface.prohibition_result(sentence)
    res = _Parser(sentence, gazetteer, settings, pins or {}, normalized=normalized).parse()
    return postpass.apply(res, sentence, normalized, gazetteer, settings, now_fn)
