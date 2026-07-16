"""§6.2 5단계 한국어 파서 — 문장 → RuleModel + chips (ParseResult).

의존성 0 (표준 라이브러리만). 형태소 분석기 금지.
파이프라인: 전처리 → 패턴 선추출(지속시간 P1/P2, 수치) → 절 분리(마지막 '면' 경계) →
절→노드 분류 → IR 방출.
"""
from __future__ import annotations

import re
from typing import Optional

from .gazetteer import (DAY_TYPE_WORDS, DEVICE_CONCEPTS, MOTION_CONCEPT,
                        MOTION_WORDS, SEASON_WORDS, SEGMENT_WORDS, Gazetteer)
from .normalize import (find_clock, find_percent, find_temperature,
                        josa_eul_reul, josa_i_ga, normalize_ws,
                        strip_particles_simple, to_duration_obj,
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
]
# 이벤트(→트리거) 키워드
EVENT_KEYWORDS = ["도착", "열리", "열림", "열려", "닫히", "닫힘", "감지", "눌리",
                  "눌림", "켜지", "꺼지", "생기", "울리", "왔", "들어오", "나가"]
# 명령형(→액션) 힌트
COMMAND_HINTS = ["켜", "꺼", "끄", "틀", "바꿔", "바꾸", "올려", "내려", "멈춰",
                 "닫아", "열어", "잠가", "풀어", "실행", "가동", "작동", "설정"]
# 절 연결어미
CONNECTIVES = ["는데", "면서", "다가", "고", "며"]
# 부정 표현
NEG_WORDS = ["없", "아니", "안 "]


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
    p1 = re.compile(
        r"(?P<locpre>[가-힣]+(?:에서|에는|에)\s+)?"
        r"(?P<n>\d+)\s*(?P<u>초|분|시간)\s*동안\s*(?P<loc>[가-힣]+(?:에서|에는|에)\s+)?"
        r"(?P<subj>[가-힣A-Za-z]+)\s*(?:이|가)?\s*(?P<neg>없|있)으?면")

    unit = {"초": 1, "분": 60, "시간": 3600}

    def _sub(m):
        gd = m.groupdict()
        secs = int(gd["n"]) * unit[gd["u"]]
        # 동안 뒤 처소격(loc)을 우선, 없으면 앞쪽 처소격(locpre) 사용.
        loc = (gd.get("loc") or gd.get("locpre") or "").strip()
        return repl(gd["subj"].strip(), loc, gd["neg"] == "없", secs)

    _sub_p2 = _sub
    _sub_p1 = _sub

    text = p2.sub(_sub_p2, text)
    text = p1.sub(_sub_p1, text)
    return text, frames


_SENTINEL_RE = re.compile(r"\x00F(\d+)\x00")


def _is_myeon_boundary(word: str) -> bool:
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
    def __init__(self, sentence: str, gz: Gazetteer, settings: dict, pins: dict):
        self.sentence = sentence
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
    def parse(self):
        text = normalize_ws(self.sentence)
        # 2) 지속시간 선추출
        text, frames = _duration_frames(text)

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
        """'면' 경계가 없을 때 "매일 [밤/…]? H시 …" 시각 트리거를 antecedent로 분리."""
        clk = find_clock(text)
        if clk and "매일" in text and not re.search(r"이후|이전|부터|까지|전에", text):
            ce = clk["span"][1]
            antecedent = text[:ce].strip()
            consequent = re.sub(r"^\s*(?:에|정각)\s*", "", text[ce:]).strip()
            return antecedent, consequent
        # 명령만 있는 문장 등 — 전체를 액션으로 시도
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

    # ---- 절이 이벤트(→트리거 후보)인가 ----
    def _clause_is_event(self, clause: str) -> bool:
        if any(k in clause for k in EVENT_KEYWORDS):
            return True
        if re.search(r"움직임|모션|인기척|동작", clause):
            return True
        if "사람" in clause and re.search(r"있|없|감지", clause):
            return True
        return False

    def _process_antecedent(self, clauses, frames):
        held = [c for c in clauses if _SENTINEL_RE.fullmatch(c.strip())]
        other = [c for c in clauses if not _SENTINEL_RE.fullmatch(c.strip())]

        for c in held:
            self._build_held(c, frames)

        event_clauses = [c for c in other if self._clause_is_event(c)]
        has_primary = bool(held) or bool(event_clauses)
        boundary_clause = other[-1] if other else None

        # 각 절의 달력/시간/수치 측면. 수치 비교는 다른 트리거가 없고 경계 절이면 트리거로 승격.
        for c in other:
            self._emit_calendar_aspect(c)
            self._emit_time_aspect(c)
            promote = (c is boundary_clause) and not has_primary and not self.triggers
            self._emit_numeric_aspect(c, as_trigger=promote)

        # 이벤트 절: held 가 있으면 전부 조건, 없으면 마지막 절이 트리거
        for i, c in enumerate(event_clauses):
            as_trigger = (not held) and (i == len(event_clauses) - 1)
            self._build_event_clause(c, as_trigger)

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
        clk = find_clock(clause)
        if clk:
            after = re.search(r"이후|부터|넘|지나", clause)
            before = re.search(r"이전|까지|전에", clause)
            # '되면'(전환)이나 '매일'은 daily 트리거, '이후/이전'은 time 조건으로 구분(§3).
            is_daily = ("매일" in self.sentence or re.search(r"되면", clause)) \
                and not after and not before
            if is_daily:
                self._build_daily_trigger(clk)
            else:
                self._build_time_condition(clause)
            return
        for seg in SEGMENT_WORDS:
            if seg in clause:
                # '새벽이 되면'처럼 전환 표현이면 트리거, '새벽시간에'처럼 위치면 조건
                if re.search(r"되면|되\b|시작하|넘어가|바뀌", clause):
                    self._build_segment_trigger(clause)
                else:
                    self._build_segment_condition(clause)
                return

    def _emit_numeric_aspect(self, clause, as_trigger=False):
        if not re.search(r"이상|이하|초과|미만|넘|올라가|내려가|떨어지", clause):
            return
        # 온도/습도 등 센서 개념이 있어야 numeric_state
        if not _find_concept(clause):
            return
        self._build_numeric(clause, as_trigger=as_trigger)

    def _build_event_clause(self, clause, as_trigger):
        if "도착" in clause or "왔" in clause or "들어오" in clause:
            self._build_arrival(clause, as_trigger)
        elif re.search(r"움직임|모션|인기척|동작|사람", clause):
            self._build_motion(clause, as_trigger)
        else:
            self._build_state_event(clause, as_trigger)

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
            area = _find_area(self.gz, fr.get("loc", "")) or self.default_area \
                or _find_area(self.gz, subj)
            concept = motion or MOTION_CONCEPT
            cands = self.gz.resolve_concept(concept, area, subj)
            slot = f"triggers[{len(self.triggers)}].entity_id"
            chip = self._chip(subj, "trigger", slot, cands)
            eid = chip.chosen
            node = {"type": "state_held", "entity_id": eid, "to": to, "for": dur}
            self.triggers.append(node)

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
        neg = ("닫" in clause or "꺼지" in clause) and "열" not in clause
        to = "off" if neg else "on"
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
            cands = self.gz.resolve_concept(concept, area, clause) if concept else []
            chip = self._chip(clause.strip(), role, slot, cands)
        eid = chip.chosen
        if as_trigger:
            self.triggers.append({"type": "state", "entity_id": eid, "to": to})
        else:
            self.conditions.append({"type": "state", "entity_id": eid, "state": to})

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
        above = below = None
        if re.search(r"이상|초과|넘|올라가|높", clause):
            above = num
        elif re.search(r"이하|미만|떨어지|낮|내려가", clause):
            below = num
        else:
            above = num
        if concept is None:
            concept = {"domain": "sensor", "device_class": "temperature"} if temp else None
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
    def _process_consequent(self, clauses):
        for clause in clauses:
            self._build_action(clause)

    def _build_action(self, clause):
        # 모드 전환("슬립 모드로 바꿔")
        for name, spec in self.gz.mode_surfaces.items():
            if name in clause:
                node = {"type": "service", "action": spec.get("action")}
                if spec.get("target"):
                    node["target"] = spec["target"]
                if spec.get("data"):
                    node["data"] = spec["data"]
                self.actions.append(node)
                self.chips.append(_Chip(name, "action",
                                        f"actions[{len(self.actions)-1}]",
                                        [{"id": name, "label": name, "sublabel": "모드 전환",
                                          "score": 1.0}]))
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

        # 명령 판정
        turn_off = bool(re.search(r"꺼|끄|멈춰|정지|닫아", clause))
        turn_on = bool(re.search(r"켜|틀|열어|가동|작동|실행|바꿔", clause)) and not turn_off
        # 값
        pct = find_percent(clause)
        # 프리셋(…로/으로) — 모드 외 climate fan_mode 등
        preset = None
        pm = re.search(r"([가-힣A-Za-z0-9]+)\s*(?:으로|로)\s*(?:틀|켜|바꿔|설정)", clause)
        if pm:
            cand = pm.group(1)
            if not find_percent(cand) and cand not in ("그것", "거기"):
                preset = cand

        # 대상들(체언 병렬: 와/과/하고/,)
        targets = self._split_targets(clause)
        if not targets and self.default_target:
            targets = [self.default_target.get("text", "")]

        made = False
        for t in targets:
            self._emit_service(t, clause, turn_on, turn_off, pct, preset)
            made = True
        if not made:
            self.unmatched.append(clause.strip())

    def _split_targets(self, clause: str):
        """액션 절에서 표적 명사들 추출(병렬 조사로 분리)."""
        # '모든 X'
        if "모든" in clause:
            return [clause[clause.find("모든"):]]
        # 조사로 분리
        body = clause
        for cmd in COMMAND_HINTS:
            body = re.sub(cmd + r".*$", "", body)
        # 병렬: 와/과/하고/이랑/랑/, 로 분리
        parts = re.split(r"(?:와|과|하고|이랑|랑|,)\s*", body)
        out = []
        for p in parts:
            p = strip_particles_simple(p.strip())
            if not p:
                continue
            # 방/기기/엔티티가 있는지
            if _find_concept(p) or self.gz.resolve_name(strip_particles_simple(p)) \
                    or _find_area(self.gz, p):
                out.append(p)
        return out

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
        if self.rule_area is None and e and e.get("area_id"):
            self.rule_area = e["area_id"]

    def _domain_service(self, domain, eid, turn_on, turn_off, pct, preset, clause):
        data = {}
        if domain == "light":
            action = "light.turn_off" if turn_off else "light.turn_on"
            if pct and not turn_off:
                data["brightness_pct"] = pct["value"]
        elif domain == "fan":
            action = "fan.turn_off" if turn_off else "fan.turn_on"
            if pct and not turn_off:
                data["percentage"] = pct["value"]
        elif domain == "climate":
            action = "climate.turn_off" if turn_off else "climate.turn_on"
        elif domain == "cover":
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

    def _category(self):
        cat_map = {"light": "lighting", "switch": "switch", "fan": "fan",
                   "cover": "cover", "climate": "climate", "media_player": "media",
                   "lock": "lock", "scene": "etc"}
        for a in self.actions:
            act = a.get("action") or ""
            dom = act.split(".", 1)[0]
            if dom in cat_map:
                return cat_map[dom]
        return "etc"

    _DAY_TYPE_LABELS = {"weekday": "평일", "weekend": "주말", "holiday": "공휴일"}
    _SEASON_LABELS = {"spring": "봄", "summer": "여름", "autumn": "가을", "winter": "겨울"}

    def _summary(self):
        parts = []
        # 트리거
        tdesc = []
        zone_persons = []
        for t in self.triggers:
            typ = t.get("type")
            if typ == "zone":
                zone_persons.append(self._nm(t.get("entity_id")))
                continue
            if typ == "state":
                nm = self._nm(t.get("entity_id"))
                verb = self._state_verb(t.get('entity_id'), t.get('to'))
                # 대상 이름에 이미 모션/움직임 의미가 있으면 중복 서술 제거
                if self._is_motion_name(nm):
                    verb = verb.replace("움직임이 ", "")
                tdesc.append(f"{nm}{josa_i_ga(nm)} {verb}")
            elif typ == "state_held":
                nm = self._nm(t.get("entity_id"))
                tdesc.append(f"{nm}{josa_i_ga(nm)} {self._dur(t['for'])} 동안 "
                             f"{'없으면' if t.get('to')=='off' else '있으면'}")
            elif typ == "group_held":
                tdesc.append(f"다른 곳 움직임이 {self._dur(t['for'])} 동안 없으면")
            elif typ == "numeric_state":
                nm = self._nm(t.get("entity_id"))
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
        if zone_persons:
            joined = ' 또는 '.join(zone_persons)
            tdesc.insert(0, f"{joined}{josa_i_ga(joined)} 집에 도착하면")
        if tdesc:
            parts.append(" 또는 ".join(tdesc))
        # 조건
        cdesc = []
        for c in self.conditions:
            typ = c.get("type")
            if typ == "time_segment":
                w = [k for k, v in SEGMENT_WORDS.items() if v == c["segments"][0]]
                cdesc.append(f"{w[0] if w else ''} 시간대")
            elif typ == "day_type":
                labels = [self._DAY_TYPE_LABELS.get(x, x) for x in c.get("types", [])]
                cdesc.append("/".join(labels))
            elif typ == "season":
                labels = [self._SEASON_LABELS.get(x, x) for x in c.get("seasons", [])]
                cdesc.append("/".join(labels))
            elif typ == "time":
                if c.get("after"):
                    cdesc.append(f"{c['after'][:5]} 이후")
                if c.get("before"):
                    cdesc.append(f"{c['before'][:5]} 이전")
            elif typ == "numeric_state":
                nm = self._nm(c.get("entity_id"))
                if c.get("above") is not None:
                    cdesc.append(f"{nm}{josa_i_ga(nm)} {c['above']} 이상")
                if c.get("below") is not None:
                    cdesc.append(f"{nm}{josa_i_ga(nm)} {c['below']} 이하")
            elif typ == "state":
                nm = self._nm(c.get("entity_id"))
                cdesc.append(f"{nm} 상태가 {c.get('state')}")
        # 액션
        adesc = []
        for a in self.actions:
            if a.get("type") == "delay":
                adesc.append(f"{self._dur(a.get('duration', {}))} 뒤에")
                continue
            act = a.get("action", "")
            tgt = a.get("target", {}).get("entity_id", [])
            nm = self._nm(tgt[0]) if tgt else act.split(".")[0]
            if act.endswith("turn_on") or act == "cover.open_cover":
                extra = ""
                if a.get("data", {}).get("brightness_pct"):
                    extra = f" {a['data']['brightness_pct']}% 밝기로"
                verb = "엽니다" if act == "cover.open_cover" else "켭니다"
                adesc.append(f"{nm}{josa_eul_reul(nm)}{extra} {verb}")
            elif act.endswith("turn_off") or act == "cover.close_cover":
                verb = "닫습니다" if act == "cover.close_cover" else "끕니다"
                adesc.append(f"{nm}{josa_eul_reul(nm)} {verb}")
            elif act == "climate.set_fan_mode":
                adesc.append(f"팬 모드를 {a['data'].get('fan_mode')}로 설정합니다")
            elif act and act.startswith("scene."):
                adesc.append("모드를 전환합니다")
            else:
                adesc.append(f"{nm} 동작을 실행합니다")
        cond_txt = (", " + " 그리고 ".join(cdesc)) if cdesc else ""
        return f"{' '.join(parts)}{cond_txt} → {', '.join(adesc)}." if parts else \
               f"{', '.join(adesc)}."

    @staticmethod
    def _is_motion_name(nm) -> bool:
        return bool(nm) and any(w in nm for w in ("모션", "움직임", "인기척", "동작", "재실"))

    def _state_verb(self, eid, to):
        e = self.gz.entity(eid)
        dc = e.get("device_class") if e else None
        if dc in ("door", "window", "opening", "garage_door"):
            return "열리면" if to == "on" else "닫히면"
        if dc in ("motion", "occupancy", "presence"):
            return "움직임이 감지되면" if to == "on" else "움직임이 없으면"
        return "켜지면" if to == "on" else "꺼지면"

    def _nm(self, eid):
        if not eid:
            return "대상"
        e = self.gz.entity(eid)
        if e:
            return e.get("name") or eid
        # person
        for surf, pid in self.gz.person_surfaces.items():
            if pid == eid:
                return surf
        return eid

    def _dur(self, d):
        if d.get("hours"):
            return f"{d['hours']}시간"
        if d.get("minutes"):
            return f"{d['minutes']}분"
        return f"{d.get('seconds',0)}초"


def parse(sentence: str, gazetteer: Gazetteer, settings: dict,
          pins: Optional[dict] = None) -> dict:
    """§6.2 진입점."""
    return _Parser(sentence, gazetteer, settings, pins or {}).parse()
