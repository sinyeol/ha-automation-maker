"""SPEC-ACCURACY-90 §3 버킷1/버킷6 — 한국어 표면형 정규화(L0 전처리) + 금지문 게이트.

Phase 7 이식(APP-PORT-PLAN §1.1): 오프라인 `normalize90.py`(표면정규화)와
`parser_overlay` 의 금지문 방어를 앱 일급 모듈로 편입한다. `parser.parse()` 진입부에서
  1) normalize_surface(sentence)  — 비표준 표면형 → 파서가 아는 정규형(순수 additive)
  2) is_prohibition(normalized)   — 금지문이면 prohibition_result 로 조기 반환
을 호출한다. 전부 결정적(Date/random 미사용). 정규형 입력은 건드리지 않는다(회귀 0 계약).

표면정규화 5축(SPEC §3 버킷1):
1. 한글 수사 → 숫자  (일곱 시 반→7시 반, 스물여덟 도→28도, 백오십→150, 세 번→3번)
2. 절경계 어미 통일   (-거든/-자마자/-는 순간/-거들랑/-면은 → 기존 파서가 아는 -면)
3. 존칭 제거 + 보충법 (-시- 제거: 하시면→하면, 나가시면→나가면 / 주무시→자, 계시→있)
4. 완화·보조 요소 제거 (좀/그냥/제발/근데/응/아 맞다/-버려/-놔/-둬)
5. 후치 조건 재배열   ("불 꺼줘, 문 열리면" → "문 열리면 불 꺼줘")

금지문 방어(SPEC §3 버킷6 / SPEC-SCHEMA-90 §4.4): "-지 마/못 -게/안 -게 해/-면 안 돼" 같은
금지문은 자동화 생성 요청이 아니다. 정반대 액션(가스밸브 열기 등) 생성이 최악의 안전사고이므로
파서를 태우기 전에 감지해 모델을 만들지 않는다(ok=False, subrules=[]). 인용부 안 표현은 무시.
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
        # (a2) L1: 이동 동사 '-ㄹ 때'(들어갈/지나갈/나갈 때) 2-토큰 → '-면'(입실·통과 시점 트리거).
        #      명령형과 무관, 트리거 경계로만 쓰인다('거실 지나갈 때 불 켜' → 지나가면).
        if core == "때" and _strip_josa_tail(tok) in _MOVE_LEX:
            out.append(_MOVE_LEXICON[_strip_josa_tail(tok)] + nxt[len("때"):])
            i += 2
            continue
        # (b) 토큰말 어미 치환.
        m = re.match(r"^(.*?)(자마자|거들랑|거든|면은)([,.…]*)$", tok)
        if m and m.group(1):
            out.append(_fix_intrans(m.group(1)) + "면" + m.group(3))
            i += 1
            continue
        # (b2) L1: 타동 개폐/이동 동사의 '-면'(열면/닫으면/닫면)은 문·창 상태 전이(트리거)이므로
        #      자동사 '-리/-히면'으로. 명령형(열어/닫아)은 '-면'이 아니라 영향 없음.
        core_tok = _strip_josa_tail(tok)
        if core_tok in _OPENCLOSE_MYEON:
            trail = tok[len(core_tok):]
            out.append(_OPENCLOSE_MYEON[core_tok] + trail)
            i += 1
            continue
        out.append(tok)
        i += 1
    return " ".join(out)


# L1: 타동 개폐 '-면' → 자동사 개폐 '-면'(문/창 상태 전이 트리거).
_OPENCLOSE_MYEON = {"열면": "열리면", "닫으면": "닫히면", "닫면": "닫히면"}
# L1: 이동 동사 '-ㄹ 때'(들어갈/지나갈/나갈 때) → '-면'. 값은 '-면' 형.
_MOVE_LEXICON = {"들어갈": "들어가면", "지나갈": "지나가면", "나갈": "나가면",
                 "들어올": "들어오면", "나올": "나오면"}
_MOVE_LEX = set(_MOVE_LEXICON)


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


# 결과상 상태형(꺼져/열려/닫혀 있-)은 명령(꺼/열어/닫아)과 표면이 겹쳐도 액션이 아니라 상태다.
# 후치 재배열에서 조건절의 '꺼져 있으면'을 액션으로 오인해 재배열을 막던 것을 방지한다.
_RESULT_STATE_TOKS = ("꺼져", "켜져", "닫혀", "열려", "잠겨", "풀려", "채워", "걸려", "놓여")


def _is_action_tok(tok: str) -> bool:
    core = _strip_josa_tail(tok)
    # '-면' 절 경계(열리면/켜지면 …)는 트리거지 액션이 아니다. 액션 힌트 글자가 겹쳐도 제외.
    if _is_myeon_boundary(core):
        return False
    # 결과상 상태형(꺼져/열려 있-)은 조건이지 액션이 아니다('돌려/올려' 등 진짜 명령은 유지).
    if any(core.startswith(r) for r in _RESULT_STATE_TOKS):
        return False
    return any(h in core for h in _ACTION_HINTS)


def _reorder_postposed_multi(text: str) -> str:
    """다중 후치절 재배열: 'ACTION, 조건1, 조건2(, …)' → '조건1 조건2 … ACTION'.

    쉼표로 3조각 이상이고, 첫 조각만 명령(액션 동사 포함)이며 나머지가 전부 조건형
    (액션 동사 없음 + 마지막 조각이 조건 종결 -면/빼고/말고/-만/-엔/-마다/-때)일 때만 발동.
    "거실 조명 켜줘, 사람 감지되면, 밤에만" · "보일러 꺼줘, 8시 되면, 주말은 빼고" 류.
    """
    raw = text.strip()
    if not raw:
        return text
    body = re.sub(r"[.?!…]+$", "", raw).strip()
    # 쉼표 또는 '마침표+공백'(문장 분리)으로 조각낸다("TV 꺼놔라. 와이프 회사 가면").
    parts = [p.strip() for p in re.split(r"\s*,\s*|\.\s+", body) if p.strip()]
    if len(parts) < 2:
        return text
    first, rest = parts[0], parts[1:]
    if not any(_is_action_tok(t) for t in first.split()):
        return text
    for r in rest:
        if any(_is_action_tok(t) for t in r.split()):
            return text
    if not re.search(r"(?:면|빼고|말고|제외|만|엔|마다|때)$", parts[-1]):
        return text
    return " ".join(rest) + " " + first


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
# 표면정규화 파이프라인
# ---------------------------------------------------------------------------
# L1: 임계 부기 괄호 '…면(N … 이상/이하)' → 'N … 이상/이하 …면'(트리거 절로 편입).
#   괄호가 경계 '면'에 붙어 절 분리를 깨고 임계 수치가 액션으로 새는 것을 막는다.
_THRESH_PAREN_RE = re.compile(
    r"([가-힣]+면)\s*\(\s*(\d[^()]*?(?:이상|이하|초과|미만|넘)[^()]*?)\s*\)")


def _relocate_threshold_paren(text: str) -> str:
    return _THRESH_PAREN_RE.sub(lambda m: m.group(2).strip() + " " + m.group(1), text)


def normalize_surface(sentence: str, gz=None) -> str:
    """비표준 표면형 → 정규형. 정규형 입력은 그대로 반환(회귀 0)."""
    if not sentence:
        return sentence
    t = sentence
    t = _relocate_threshold_paren(t)  # 0) 임계 괄호 재배치(경계 복원)
    t = _convert_numerals(t)      # 1) 수사 → 숫자 (먼저 — 시각 'N시'가 존칭 '시' 오제거 방지)
    t = _strip_honorific(t)       # 3) 존칭 제거 + 보충법 (어미 정규화 전에 -시- 삭제)
    t = _normalize_endings(t)     # 2) 절경계 어미 → -면
    t = _strip_fillers(t)         # 4) 완화·보조 요소 제거
    t = _reorder_postposed_multi(t)  # 5a) 다중 후치절(ACTION, 조건1, 조건2)
    t = _reorder_postposed(t)     # 5) 후치 조건 재배열
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ===========================================================================
# 금지문(prohibition) 오독 방어 — SPEC-ACCURACY-90 §3 버킷6 / SPEC-SCHEMA-90 §4.4
#   "-지 마/-지 말/못 -게/안 -게 해/-면 안 돼" 같은 금지문은 자동화 생성 요청이 아니다.
#   정반대 액션(가스밸브 열기 등) 생성이 최악의 안전사고이므로, 파서를 태우기 전에 감지해
#   모델을 만들지 않고(ok=False, subrules=[]) 미해결로 반환한다. 인용부(따옴표) 안의 금지
#   표현은 무시한다(예: 알림 메시지 '…하지 마세요'). normalize 로 완화어를 제거한 표면에서 본다.
# ===========================================================================
_QUOTE_SPAN_RE = re.compile(r"['\"“”‘’『』「」][^'\"“”‘’『』「」]*['\"“”‘’『』「」]")
_PROHIBIT_RE = re.compile(
    r"지도?\s*(?:좀\s*)?마(?![가-힣])"                 # -지 마 / -지도 마 (마세요·마루 등 제외)
    r"|지도?\s*(?:좀\s*)?마\s*라"                       # -지 마라
    r"|지도?\s*(?:좀\s*)?말(?=[아라자고지것세]|\s|[,.!?]|$)"  # -지 말아/말고/말라/말지/말자/말 것
    r"|못\s*[가-힣]+게"                                  # 못 -게 해 (못 켜게)
    r"|안\s+[가-힣]+게\s*(?:좀\s*)?(?:해|하|만들)"       # 안 -게 해 (안 켜지게 해). 공백 필수(결함3): '안전/편안'의 '안'을 부정부사로 오인 방지(parser.py:121 정합)
    r"|(?:으면|면)\s*(?:절대\s*)?안\s*(?:돼|되|된다)")   # -면 (절대) 안 돼


def is_prohibition(normalized: str) -> bool:
    """normalize 를 거친 표면형이 금지문(요청 아님)인가. 인용부 안 표현은 무시."""
    if not normalized:
        return False
    dequoted = _QUOTE_SPAN_RE.sub(" ", normalized)
    return bool(_PROHIBIT_RE.search(dequoted))


def prohibition_result(sentence: str) -> dict:
    """금지문 감지 시 반환하는 미해결 결과(정반대 액션 절대 미생성). 앱 parse 반환형과 호환."""
    return {"ok": False,
            "model": {"alias": sentence.strip(), "description": "",
                      "mode": "single", "subrules": []},
            "chips": [], "summary": "",
            "area_id": None, "category": None,
            "unmatched": [sentence.strip()], "confidence": 0.0,
            "warnings": ["금지문(자동화 생성 요청 아님)으로 판단해 동작을 만들지 않았습니다."]}
