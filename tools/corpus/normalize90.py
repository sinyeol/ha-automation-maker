"""SPEC-ACCURACY-90 §3 버킷1 — 한국어 표면형 정규화(L0 전처리).

익숙한 의미를 **비표준 표면형**으로 쓴 문장을, 기존 파서(automation_maker/backend/nl)가
이미 아는 **정규형**으로 결정적 변환한다. 순수 additive: 정규형인 입력은 건드리지 않는다
(회귀 0 계약). 앱 파일은 수정하지 않고 오버레이(parser_overlay.parse_patched)가 진입부에서
호출한다. 앱 포팅(Phase 7)이 쉽도록 독립 모듈로 분리.

변환 5축(SPEC §3 버킷1):
1. 한글 수사 → 숫자  (일곱 시 반→7시 반, 스물여덟 도→28도, 백오십→150, 세 번→3번)
2. 절경계 어미 통일   (-거든/-자마자/-는 순간/-거들랑/-면은 → 기존 파서가 아는 -면)
3. 존칭 제거 + 보충법 (-시- 제거: 하시면→하면, 나가시면→나가면 / 주무시→자, 계시→있)
4. 완화·보조 요소 제거 (좀/그냥/제발/근데/응/아 맞다/-버려/-놔/-둬)
5. 후치 조건 재배열   ("불 꺼줘, 문 열리면" → "문 열리면 불 꺼줘")

전부 결정적(입력이 같으면 출력이 같음). Date/random 미사용.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 0) 절 경계('면') 판정용 어간(파서와 독립된 보수적 사본).
#    후치 재배열에서 "마지막 절이 조건절인가"를 표면만으로 판정하는 데 쓴다.
# ---------------------------------------------------------------------------
_BOUNDARY_STEMS = (
    "있", "없", "되", "지나", "하", "열리", "닫히", "눌리", "도착", "감지",
    "바뀌", "올라가", "내려가", "넘", "떨어지", "켜지", "꺼지", "오", "가",
    "들어오", "나가", "유지", "풀리", "잠기", "울리", "닿", "생기", "뜨", "지",
    "오르", "내리", "낮아지", "높아지", "많아지", "적어지", "왔",
    "아니", "잡히", "움직이", "나서", "비우", "새", "느껴지", "없어지",
)


def _is_myeon_boundary(word: str) -> bool:
    """단어가 동사-검증된 '-면' 종결(절 경계)인가. 파서 _is_myeon_boundary 와 동형."""
    if not word.endswith("면"):
        return False
    body = word[:-1]
    if body.endswith("으"):
        body = body[:-1]
    if any(body.endswith(s) for s in _BOUNDARY_STEMS):
        return True
    if body.endswith("이"):  # copula (…이면)
        return True
    return False


# ---------------------------------------------------------------------------
# 1) 한글 수사 → 숫자
# ---------------------------------------------------------------------------
_NATIVE_TENS = {"열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40,
                "쉰": 50, "예순": 60, "일흔": 70, "여든": 80, "아흔": 90}
_NATIVE_ONES = {"하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3,
                "넷": 4, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7,
                "여덟": 8, "아홉": 9}
_SINO_ONES = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5,
              "육": 6, "칠": 7, "팔": 8, "구": 9}
_SINO_MULT = {"십": 10, "백": 100, "천": 1000}

# 수사 뒤에 오면 '수량 문맥'으로 확정하는 단위/counter.
_UNIT_WORDS = ("시간", "시", "분", "초", "도", "번", "개", "명", "잔", "대",
               "층", "프로", "퍼센트", "퍼")
# 수사 뒤 비교/경계 표현(단위 없이도 수량으로 확정: "백오십 넘으면").
_CMP_PREFIXES = ("넘", "이상", "이하", "미만", "초과", "이후", "이내")


def _native_prefix(s: str):
    """s 앞부분을 토박이수(0~99)로 소비. (값, 소비길이) 또는 (None,0)."""
    val = 0
    i = 0
    for w in sorted(_NATIVE_TENS, key=len, reverse=True):
        if s.startswith(w):
            val += _NATIVE_TENS[w]
            i = len(w)
            break
    for w in sorted(_NATIVE_ONES, key=len, reverse=True):
        if s[i:].startswith(w):
            val += _NATIVE_ONES[w]
            i += len(w)
            return val, i
    return (val, i) if i else (None, 0)


def _sino_prefix(s: str):
    """s 앞부분을 한자수로 소비. (값, 소비길이) 또는 (None,0)."""
    total = 0
    cur = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in _SINO_ONES:
            cur = _SINO_ONES[ch]
        elif ch in _SINO_MULT:
            total += (cur or 1) * _SINO_MULT[ch]
            cur = 0
        else:
            break
        i += 1
    if i == 0:
        return None, 0
    return total + cur, i


def _num_prefix(token: str):
    """토큰 앞부분의 수사를 숫자로. (값, 나머지) 또는 (None, token).

    토박이수를 먼저 시도(더 길게 소비되면 채택), 실패 시 한자수. 단일음절 '이'(지시어
    '이 조명'과 충돌)는 수사로 보지 않는다.
    """
    nv, ni = _native_prefix(token)
    sv, si = _sino_prefix(token)
    # 더 길게 소비하는 해석 우선(동률이면 토박이수).
    if ni >= si and ni > 0:
        return nv, token[ni:]
    if si > 0:
        if token[:si] == "이":  # 지시어 '이' 오인 방지
            return None, token
        return sv, token[si:]
    return None, token


def _convert_numerals(text: str) -> str:
    """수량 문맥의 한글 수사를 아라비아 숫자로. 정규형(이미 숫자)은 무변."""
    toks = text.split()
    out = []
    for idx, tok in enumerate(toks):
        val, rest = _num_prefix(tok)
        if val is None:
            out.append(tok)
            continue
        # (a) 토큰 안에 단위가 붙은 형태: "스물여덟도"/"열한시".
        if rest and rest.lstrip("을를이가은는").startswith(_UNIT_WORDS):
            out.append(str(val) + rest)
            continue
        # (b) 맨수사 토큰: 다음 토큰이 단위/비교로 시작하면 확정.
        if rest == "":
            nxt = toks[idx + 1] if idx + 1 < len(toks) else ""
            nxt_core = nxt.lstrip("을를이가은는")
            if nxt_core.startswith(_UNIT_WORDS) or nxt_core.startswith(_CMP_PREFIXES):
                out.append(str(val))
                continue
        out.append(tok)  # 문맥 불확실 → 보존
    return " ".join(out)


# ---------------------------------------------------------------------------
# 2) 절경계 어미 통일 → '-면'
# ---------------------------------------------------------------------------
# 어미를 떼고 남은 어간이 문/기기 '상태 전이'의 자동사여야 하는데, 화자가 타동사로 쓴 경우
# 정규 자동사 어간으로 보충한다(문 '열'자마자 → 문이 '열리'면, 티비 '켜'자마자 → '켜지'면).
# 절 경계(트리거)에서만 적용 — 명령형(켜줘/열어)은 어미를 떼지 않으므로 영향 없음.
_INTRANS_STEM_FIX = {"열": "열리", "닫": "닫히", "잠그": "잠기", "잠구": "잠기",
                     "켜": "켜지", "꺼": "꺼지"}


def _fix_intrans(stem: str) -> str:
    return _INTRANS_STEM_FIX.get(stem, stem)


def _normalize_endings(text: str) -> str:
    toks = text.split()
    out = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        # (a) 'X는/은 순간(에)' 2-토큰 → 'X면'.
        core = re.sub(r"[,.…]+$", "", nxt)
        if core in ("순간", "순간에", "찰나", "찰나에") and (tok.endswith("는") or tok.endswith("은")):
            stem = _fix_intrans(tok[:-1])
            trail = nxt[len(core):]  # 구두점 등 보존
            out.append(stem + "면" + trail)
            i += 2
            continue
        # (b) 토큰말 어미 치환.
        m = re.match(r"^(.*?)(자마자|거들랑|거든|면은)([,.…]*)$", tok)
        if m and m.group(1):
            out.append(_fix_intrans(m.group(1)) + "면" + m.group(3))
            i += 1
            continue
        out.append(tok)
        i += 1
    return " ".join(out)


# ---------------------------------------------------------------------------
# 3) 존칭 제거 + 보충법(suppletion) 사전
# ---------------------------------------------------------------------------
# 보충법: 존칭 어간이 별도 어휘인 동사(먼저 치환).
_SUPPLETIVE = (
    ("주무시", "자"), ("잠드시", "자"), ("계시", "있"),
    ("드시", "먹"), ("잡수시", "먹"), ("돌아가시", "가"),
)
# 존칭 접미사 요청형(액션 말미) → 평서 명령. 명령 어간은 이미 보존됨.
_HONORIFIC_ENDINGS = ("주시겠어요", "주시겠어", "주십시오", "주세요", "주실래요",
                      "주실래", "드리세요", "주시길", "주시죠")
# 존칭 삽입 '-시-' 를 제거할 때, '시' 다음에 올 수 있는 어미 첫 글자.
_SI_FOLLOW = "면는자어아고겠었더시길"


def _strip_honorific(text: str) -> str:
    for hon, base in _SUPPLETIVE:
        text = text.replace(hon, base)
    # 존칭 '-시-' 삽입 제거: 앞이 한글 어간, 뒤가 어미 첫 글자일 때만(시각 'N시'·'시간'·'시작' 배제).
    text = re.sub(r"(?<=[가-힣])시(?=[" + _SI_FOLLOW + r"])",
                  lambda m: "" if not _is_clock_si(text, m.start()) else "시", text)
    # 요청형 존칭 어미 정리(선택적 — 명령 어간은 이미 있음).
    for hon in _HONORIFIC_ENDINGS:
        text = text.replace(hon, "줘")
    return text


def _is_clock_si(text: str, pos: int) -> bool:
    """text[pos]=='시' 가 시각('9시')의 '시'인가 — 앞 글자가 숫자면 참(제거 금지)."""
    return pos > 0 and text[pos - 1].isdigit()


# ---------------------------------------------------------------------------
# 4) 완화·보조 요소 제거
# ---------------------------------------------------------------------------
# 다중어 완화구(구 단위 제거 — '아 맞다' 처럼 실질어 '아'/'맞다'가 붙어 다니는 것만).
_FILLER_PHRASES = ("아 맞다", "아 참", "그 뭐냐", "뭐랄까")
# 단독 완화어(토큰 전체가 이것일 때만 제거 — 부분매칭 금지). '아/맞다/참/음' 등 단독 실질어
# 가능성이 있는 것은 넣지 않는다(완화구에서만 제거) → 보수적으로 회귀 방지.
_FILLER_SOLO = {"좀", "그냥", "제발", "좀만", "응", "근데", "말이야", "말이지"}


def _strip_fillers(text: str) -> str:
    for ph in _FILLER_PHRASES:
        text = text.replace(ph, " ")
    toks = text.split()
    out = [t for t in toks if _strip_josa_tail(t) not in _FILLER_SOLO]
    return " ".join(out)


def _strip_josa_tail(tok: str) -> str:
    return re.sub(r"[,.…]+$", "", tok)


# ---------------------------------------------------------------------------
# 5) 후치 조건 재배열
#    액션이 앞, 조건절(-면)이 뒤 → 조건절을 앞으로. 정상 어순(액션이 문말)은 무변.
# ---------------------------------------------------------------------------
_ACTION_HINTS = ("켜", "꺼", "끄", "틀", "열", "닫", "잠", "풀", "돌려", "깜빡",
                 "낮춰", "올려", "내려", "멈춰", "알려", "보내", "울려", "실행",
                 "가동", "작동", "설정", "바꿔", "쳐", "걷", "펼", "젖", "소등",
                 "점등", "방송", "재생")


def _is_action_tok(tok: str) -> bool:
    core = _strip_josa_tail(tok)
    # '-면' 절 경계(열리면/켜지면 …)는 트리거지 액션이 아니다. 액션 힌트 글자가 겹쳐도 제외.
    if _is_myeon_boundary(core):
        return False
    return any(h in core for h in _ACTION_HINTS)


def _reorder_postposed(text: str) -> str:
    """후치 조건절을 앞으로 재배열. 마지막 실질 토큰이 '-면' 경계일 때만 발동."""
    raw = text.strip()
    if not raw:
        return text
    body = re.sub(r"[.?!…]+$", "", raw).strip()
    toks = body.split()
    if len(toks) < 2:
        return text
    # 마지막 실질 토큰이 조건 경계('-면')가 아니면(=정상 어순) 손대지 않는다.
    last_core = _strip_josa_tail(toks[-1])
    if not _is_myeon_boundary(last_core):
        return text
    # 조건 경계가 2개 이상이면 다중절 문법(파서 담당) → 재배열 금지.
    if sum(1 for t in toks if _is_myeon_boundary(_strip_josa_tail(t))) != 1:
        return text

    # (A) 구두점(,.…)으로 조건절이 분리된 경우: 마지막 구분자에서 자른다.
    seps = [m.start() for m in re.finditer(r"[,.…]", body)]
    if seps:
        cut = seps[-1]
        head = body[:cut].strip(" ,.…")
        cond = body[cut + 1:].strip(" ,.…")
    else:
        # (B) 구분자 없음: 마지막 액션 토큰 뒤부터가 조건절.
        act_idx = max((j for j, t in enumerate(toks) if _is_action_tok(t)), default=-1)
        if act_idx < 0 or act_idx == len(toks) - 1:
            return text
        head = " ".join(toks[:act_idx + 1]).strip()
        cond = " ".join(toks[act_idx + 1:]).strip()

    if not head or not cond:
        return text
    # 조건절에 액션 동사가 있으면(진짜 조건이 아님) 재배열 금지.
    if any(_is_action_tok(t) for t in cond.split()):
        return text
    # 액션부에 액션 동사가 없으면(대상만 있는 파편) 재배열 금지.
    if not any(_is_action_tok(t) for t in head.split()):
        return text
    return cond + " " + head


# ---------------------------------------------------------------------------
# 파이프라인
# ---------------------------------------------------------------------------
def normalize_surface(sentence: str, gz=None) -> str:
    """비표준 표면형 → 정규형. 정규형 입력은 그대로 반환(회귀 0)."""
    if not sentence:
        return sentence
    t = sentence
    t = _convert_numerals(t)      # 1) 수사 → 숫자 (먼저 — 시각 'N시'가 존칭 '시' 오제거 방지)
    t = _strip_honorific(t)       # 3) 존칭 제거 + 보충법 (어미 정규화 전에 -시- 삭제)
    t = _normalize_endings(t)     # 2) 절경계 어미 → -면
    t = _strip_fillers(t)         # 4) 완화·보조 요소 제거
    t = _reorder_postposed(t)     # 5) 후치 조건 재배열
    t = re.sub(r"\s+", " ", t).strip()
    return t
