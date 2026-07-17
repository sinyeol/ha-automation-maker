"""수치/단위/시각/조사 정규화 유틸 (표준 라이브러리만).

파서와 게이저티어가 공유하는 저수준 한국어 처리:
- 공백 정규화
- 조사(particle) 사전 검증형 최장일치 스트리핑
- 퍼센트 / 온도 / 지속시간 / 시각 파싱
- 초성 추출(초성 검색용)
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 조사 목록 (길이 내림차순으로 최장일치). 스트리핑은 사전 검증형 —
# 자른 결과가 사전에 있을 때만 확정한다(strip_particles 참조).
# ---------------------------------------------------------------------------
PARTICLES = [
    # 3자
    "이라도", "에서는", "에게서", "한테서", "으로는", "이랑은",
    # 2자
    "에서", "에게", "한테", "으로", "이랑", "하고", "부터", "까지", "보다",
    "처럼", "만큼", "라고", "로서", "로써", "에다", "이나", "이란", "이든",
    "든지", "이든", "라는", "이면", "라면", "은는", "께서",
    # 1자
    "은", "는", "이", "가", "을", "를", "에", "의", "와", "과", "로", "도",
    "만", "랑", "나", "야", "아", "께",
]
PARTICLES = sorted(set(PARTICLES), key=len, reverse=True)

# 명사 병렬을 만드는 조사(체언+X): 절 분리가 아니라 병렬로 처리해야 함
COORD_PARTICLES = ["하고", "이랑", "와", "과", "랑"]

_WS_RE = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    """앞뒤 공백 제거 + 내부 연속 공백을 하나로."""
    return _WS_RE.sub(" ", text).strip()


def strip_particles(token: str, is_word) -> str:
    """토큰 끝의 조사를 사전 검증형 최장일치로 제거.

    is_word(candidate) 가 True 를 돌려주는 가장 긴 절단 결과를 채택한다.
    아무 절단도 사전에 없으면 원문에서 알려진 조사만 1회 제거(폴백).
    """
    if not token:
        return token
    # 사전 검증형: 자른 결과가 사전에 있으면 그 형태를 채택
    for p in PARTICLES:
        if len(token) > len(p) and token.endswith(p):
            cand = token[: -len(p)]
            if is_word(cand):
                return cand
    if is_word(token):
        return token
    # 폴백: 사전에 없더라도 알려진 조사 1회 제거(가장 긴 것)
    for p in PARTICLES:
        if len(token) > len(p) and token.endswith(p):
            return token[: -len(p)]
    return token


def strip_particles_simple(token: str) -> str:
    """사전 없이 조사 최장일치 1회 제거(느슨)."""
    for p in PARTICLES:
        if len(token) > len(p) and token.endswith(p):
            return token[: -len(p)]
    return token


# ---------------------------------------------------------------------------
# 수치/단위
# ---------------------------------------------------------------------------
PERCENT_RE = re.compile(r"(\d+)\s*(?:%|퍼센트|프로)")
TEMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*도(?![시분])")
# 지속시간 스칼라. '동안'/'간' 접미사(예: "5분간"/"5분 동안")는 뒤따르는 잉여 텍스트로
# 취급돼 스칼라 추출에 영향이 없다(A4 프레임 인식은 parser._duration_frames 담당).
DURATION_RE = re.compile(r"(\d+)\s*(초|분|시간)")
# 음수 온도 표지(영하/마이너스). find_temperature(signed=True) 에서만 부호 반영.
NEG_TEMP_RE = re.compile(r"영하|마이너스")
# 시각: (오전|오후|아침|저녁|밤|새벽|낮)? 12시 (30분|반)?
CLOCK_RE = re.compile(
    r"(오전|오후|아침|저녁|밤|새벽|낮|정오|자정)?\s*(\d{1,2})\s*시\s*(?:(\d{1,2})\s*분|(반))?"
)

_UNIT_SECONDS = {"초": 1, "분": 60, "시간": 3600}


def find_percent(text: str):
    m = PERCENT_RE.search(text)
    if not m:
        return None
    return {"value": int(m.group(1)), "span": (m.start(), m.end())}


def find_temperature(text: str, *, signed: bool = False):
    """온도 리터럴('26도') → {'value','span'}. 없으면 None.

    signed=True 이면 '영하'/'마이너스' 표지가 문장에 있을 때 부호를 음수로 반영한다
    ('영하 5도' → -5.0). 기본(False) 은 기존 동작(양수 절댓값)과 동일해 회귀 없음.
    """
    m = TEMP_RE.search(text)
    if not m:
        return None
    val = float(m.group(1))
    if signed and NEG_TEMP_RE.search(text):
        val = -abs(val)
    return {"value": val, "span": (m.start(), m.end())}


def find_duration(text: str):
    m = DURATION_RE.search(text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return {"value": n, "unit": unit, "seconds": n * _UNIT_SECONDS[unit],
            "span": (m.start(), m.end())}


def to_duration_obj(seconds: int) -> dict:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return {"hours": h, "minutes": m, "seconds": s}


# ---------------------------------------------------------------------------
# 값(밝기/세기/위치/볼륨) 정규화 (B2)
#   %/절반/최대/최소·은은·약하게/단위없는 'N으로' → 0~100 정수.
#   순수 텍스트→값 변환이므로 gazetteer/parser 상태에 의존하지 않는다.
# ---------------------------------------------------------------------------
_VALUE_HALF_RE = re.compile(r"절반|반쯤|반만|반으로|반\s*밝기|반\s*정도")
_VALUE_MAX_RE = re.compile(r"최대|제일\s*밝|가장\s*밝|풀\s*파워|풀파워|풀\s*밝|최고\s*밝|환하게")
_VALUE_MIN_RE = re.compile(r"최소|제일\s*어둡|가장\s*어둡|아주\s*약|은은|살짝|약하게|희미")
_VALUE_BARE_RE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:으로|로)\s*(?:켜|해|설정|맞춰|낮춰|올려|줄여)")


def value_pct(text: str):
    """B2 값 해석. %/절반→50/최대→100/최소·은은·약하게→20/단위없는 'N으로' → 정수. 없으면 None.

    '26도로/9시로' 같은 시각·온도 리터럴은 밝기값으로 오인하지 않는다.
    """
    p = find_percent(text)
    if p:
        return p["value"]
    if _VALUE_HALF_RE.search(text):
        return 50
    if _VALUE_MAX_RE.search(text):
        return 100
    if _VALUE_MIN_RE.search(text):
        return 20
    m = _VALUE_BARE_RE.search(text)
    if m:
        v = int(m.group(1))
        if not re.search(str(v) + r"\s*(?:도|시|분|초|시간)", text):
            return v
    return None


def value_dict(text: str):
    """value_pct 를 {"value": n} 형태로(없으면 None). find_percent 의 상위집합(B2)."""
    v = value_pct(text)
    return {"value": v} if v is not None else None


def find_clock(text: str):
    """시각 표현 → {'hh','mm','span','ampm'}. 24시간제 HH:MM."""
    m = CLOCK_RE.search(text)
    if not m:
        return None
    ampm, hh, mm, half = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    minute = 30 if half else (int(mm) if mm else 0)
    hour = hh
    # 12시는 관례상 특수: 밤/자정/오전/새벽 12시 = 00:00, 낮/오후/정오 12시 = 12:00.
    if hh == 12:
        if ampm in ("오전", "아침", "새벽", "밤", "자정"):
            hour = 0
        else:  # 오후/낮/정오/무표기 → 정오
            hour = 12
    elif ampm in ("오후", "저녁", "밤") and hh < 12:
        hour = hh + 12
    elif ampm == "정오":
        hour = 12
    elif ampm == "자정":
        hour = 0
    elif ampm == "낮" and hh <= 6:
        hour = hh + 12  # '낮 2시' → 14시
    hour = hour % 24
    return {"hh": hour, "mm": minute, "text": m.group(0), "span": (m.start(), m.end()),
            "hhmm": f"{hour:02d}:{minute:02d}"}


# ---------------------------------------------------------------------------
# 한글 음절 / 조사(을·를 / 이·가) 판정
# ---------------------------------------------------------------------------
def is_hangul_syllable(ch: str) -> bool:
    """완성형 한글 음절(가~힣)인가."""
    return bool(ch) and "가" <= ch <= "힣"


def _has_final_consonant(ch: str) -> bool:
    """마지막 글자에 받침(종성)이 있는가."""
    return is_hangul_syllable(ch) and (ord(ch) - 0xAC00) % 28 != 0


def josa_eul_reul(word: str) -> str:
    """받침 유무로 을/를 선택. 비한글로 끝나면 병기('을(를)')."""
    if not word:
        return "을(를)"
    ch = word[-1]
    if not is_hangul_syllable(ch):
        return "을(를)"
    return "을" if _has_final_consonant(ch) else "를"


def josa_i_ga(word: str) -> str:
    """받침 유무로 이/가 선택. 비한글로 끝나면 병기('이(가)')."""
    if not word:
        return "이(가)"
    ch = word[-1]
    if not is_hangul_syllable(ch):
        return "이(가)"
    return "이" if _has_final_consonant(ch) else "가"


def token_boundary_ok(text: str, start: int, end: int, surface: str) -> bool:
    """부분문자열 매칭이 한글 토큰 경계를 지키는지 검사.

    - 매칭 앞 글자가 한글 음절이면 거부 ('누나'의 '나').
    - 매칭 뒤에 이어지는 한글이 순수 조사(strip 가능)가 아니면 거부 ('나가면'의 '나').
      뒤가 조사(나와/나랑/와이프가)면 허용.
    """
    if start > 0 and is_hangul_syllable(text[start - 1]):
        return False
    j = end
    while j < len(text) and is_hangul_syllable(text[j]):
        j += 1
    rest = text[end:j]
    if rest and strip_particles_simple(surface + rest) != surface:
        return False
    return True


# ---------------------------------------------------------------------------
# 초성
# ---------------------------------------------------------------------------
_CHO = ["ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ", "ㅅ", "ㅆ",
        "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]


def choseong(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            out.append(_CHO[(code - 0xAC00) // 588])
        else:
            out.append(ch)
    return "".join(out)
