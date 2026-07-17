"""L2 템플릿 매처 — 하이브리드 파서의 예시-라이브러리 레이어(SPEC-ACCURACY §2.6 이식).

입력 문장을 delexicalize(구체 어휘 → 태그열)해서, pattern_library 의 covered/partial 템플릿
골격과 대조하고, 매칭되면 그 템플릿의 gold 골격에 입력 슬롯을 바인딩해 구체 RuleModel 후보를
만든다. 규칙 파서(L1)가 못 읽는 **새 어순** 문장을 이미 아는 골드 문형으로 흡수하는 것이 목적.

앱 내부에서 자족한다: gazetteer(match/resolve_concept/entity)와 normalize(find_duration)만
읽기 전용으로 쓰고, gold 구체화(concretize_gold)는 이 모듈에 내장한다(도구 generate 의존 금지).
A6 조명 접미사 어휘("무드등"/"메인등"/"등")는 delexicalize 오버레이로 이 모듈에 인라인한다.

핵심 파이프라인:
  1) delexicalize: gazetteer.match + normalize.find_duration + A6 오버레이 어휘로 스팬 태깅 →
     인접 A(area)+D(device)/A+MOT 병합, 기능어는 극성/구조 심볼로 정규화 → 순서 있는 토큰 스트림.
  2) 인덱스: covered/partial 템플릿의 (태그골격 스트림 · 내용슬롯열 · gold) 사전계산.
  3) 2단 매칭: (a) 스트림 완전일치[극성 기능어 포함] → slot_fill,
     (b) 순서보존 유사도(LCS) τ=0.72 → struct_replace.
  4) 오탐 3게이트: 마진 τ_margin=0.05 · gap 템플릿 제외(인덱스 단계) · 구조태그 부분집합.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import yaml

from .gazetteer import MOTION_CONCEPT
from .normalize import find_duration

# ---------------------------------------------------------------------------
# A6 오버레이 어휘 — 조명 접미사(무드등/메인등/등). pattern_library 가 A6 반영 파서로
# covered 산정되었으므로, 매처 delexicalize 도 동일 어휘로 태깅해야 스트림이 정렬된다.
# gazetteer 전역은 건드리지 않고 이 매처 안에서만 얹는다(label 이름매칭으로 정확 엔티티 선택).
# ---------------------------------------------------------------------------
_A6_CONCEPTS: dict[str, dict] = {
    "무드등": {"domain": "light", "label": "무드등"},
    "메인등": {"domain": "light", "label": "메인등"},
    "등": {"domain": "light", "label": "조명"},
}

_LIBRARY_PATH = os.path.join(os.path.dirname(__file__), "pattern_library.yaml")


def load_pattern_library(path: Optional[str] = None) -> list:
    """앱 동봉 pattern_library.yaml 로드(없으면 빈 리스트 → 매처 무동작)."""
    p = path or _LIBRARY_PATH
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
    except (OSError, ValueError, yaml.YAMLError):
        return []
    return [t for t in data if isinstance(t, dict) and t.get("template")]


# ---------------------------------------------------------------------------
# gold 구체화(플레이스홀더 → 값) — tools/corpus/generate.concretize_gold 자족 이식.
# ---------------------------------------------------------------------------
_FULL_PH_RE = re.compile(r"^\{(\w+)\.(\w+)\}$")
_SENSOR_DOMAINS = {"binary_sensor", "sensor"}


def _domain_ids(inventory: dict, domain: str) -> list:
    return sorted(e["entity_id"] for e in (inventory or {}).get("entities", [])
                  if e.get("domain") == domain)


def _resolve_ph(slot: str, attr: str, combo: dict, inventory: dict):
    filler = combo.get(slot)
    if attr == "expand":  # {scope.expand}
        domain = filler.get("domain") if filler else None
        if not domain:
            # 제어 기기 슬롯(device_*)을 1순위, 그 외 비센서 엔티티 슬롯을 2순위로 도메인 유추.
            for k, f in combo.items():
                eid = f.get("entity")
                if eid and "." in eid and str(k).startswith("device_"):
                    domain = eid.split(".", 1)[0]
                    break
            if not domain:
                for k, f in combo.items():
                    eid = f.get("entity")
                    if eid and "." in eid and eid.split(".", 1)[0] not in _SENSOR_DOMAINS:
                        domain = eid.split(".", 1)[0]
                        break
        return _domain_ids(inventory, domain) if domain else []
    if filler is None:
        return None
    return filler.get(attr)


def concretize_gold(gold, combo: dict, inventory: dict):
    """gold 구조를 재귀 순회하며 ``{slot.attr}`` 를 실제 값으로 치환.

    YAML 1.1 이 ``on``/``off`` 를 bool 로 파싱하므로 RuleModel 규약(문자열 "on"/"off")으로 되돌린다.
    """
    if isinstance(gold, bool):
        return "on" if gold else "off"
    if isinstance(gold, dict):
        return {k: concretize_gold(v, combo, inventory) for k, v in gold.items()}
    if isinstance(gold, list):
        return [concretize_gold(v, combo, inventory) for v in gold]
    if isinstance(gold, str):
        m = _FULL_PH_RE.match(gold.strip())
        if m:
            return _resolve_ph(m.group(1), m.group(2), combo, inventory)

        def sub(mm):
            v = _resolve_ph(mm.group(1), mm.group(2), combo, inventory)
            return str(v) if v is not None else mm.group(0)
        return re.sub(r"\{(\w+)\.(\w+)\}", sub, gold)
    return gold


# ---------------------------------------------------------------------------
# 태그/심볼 어휘
# ---------------------------------------------------------------------------
# 내용 태그(슬롯 바인딩 대상): D=기기, MOT=모션, M=모드, SEG=시간대, PCT=%, DUR=지속,
#   TMP=온도, CLK=시각, PER=사람, NUM=맨숫자.
_CONTENT_TAGS = {"D", "MOT", "M", "SEG", "PCT", "DUR", "TMP", "CLK", "PER", "NUM"}

# gazetteer.match span type → 내용 태그
_TYPE_TAG = {"entity": "D", "device_word": "D", "person": "PER", "area": "A",
             "mode": "M", "segment": "SEG", "percent": "PCT",
             "temperature": "TMP", "clock": "CLK", "duration": "DUR"}

# 템플릿 슬롯명 → 내용 태그
_SLOT_TAG = {"device_light": "D", "device_fan": "D", "device_climate": "D",
             "device_media": "D", "device": "D", "motion": "MOT", "mode": "M",
             "segment": "SEG", "percent": "PCT", "duration": "DUR",
             "temperature": "TMP", "clock": "CLK", "person": "PER"}

# 기기 슬롯 이름 → 의도한 도메인. delexicalize 는 모든 기기를 태그 "D" 로 뭉개므로 스트림
# 완전일치만으로는 light/fan/climate 골드가 구분되지 않는다(예: light 문장이 covered 정렬상
# 먼저 오는 fan.turn_on 골드로 흡수). 채택 전 이 표로 슬롯 도메인 ↔ 입력 기기 도메인 일치를
# 강제해 오도메인 흡수를 막는다. "device"(도메인 무관 slot)는 None → 도메인 검사 생략.
_SLOT_DOMAIN = {"device_light": "light", "device_fan": "fan",
                "device_climate": "climate", "device_media": "media_player"}

# 조사 토큰(템플릿 {가}/{을} 등) — 스트림에서 무시.
_JOSA_TOKENS = {"이", "가", "을", "를", "은", "는", "와", "과", "로", "으로",
                "에", "의", "도", "만", "랑"}

_MOTION_WORDS = ("움직임", "모션", "인기척", "재실", "동작")


def _sym(word: str) -> Optional[str]:
    """기능어 한 어절 → 극성/구조 심볼(정규화). 없으면 None(무시).

    검사 순서가 극성을 가른다: 켜지/꺼지(트리거) > 없으(held) > 감지/작동(event) >
    조건(이고/일때) > 액션(켜/꺼). 입력 문장과 템플릿 리터럴에 **같은 함수**를 써 정렬한다.
    """
    if not word:
        return None
    if word in ("모든", "전부", "집의", "다"):        # 스코프(A3/A10)
        return "SCOPE"
    if re.search(r"해제|취소|종료|풀리|풀려|해지", word):
        return "TRIGOFF"
    if "켜지" in word:
        return "TRIGON"
    if "꺼지" in word:
        return "TRIGOFF"
    if "없으" in word or ("없" in word and word.endswith("면")):
        return "HELDOFF"
    if re.search(r"감지|작동|뜨면|들어오|열리|울리|눌리", word):
        return "EVTON"
    if re.search(r"이고|일\s*때|일때|이면서|인때|이면$", word):
        return "COND"
    if "아니" in word:
        return "CONDNEG"
    if re.search(r"켜|틀|가동|실행|바꿔|열어", word):
        return "ACTON"
    if re.search(r"꺼|끄|멈춰|정지|닫아", word):
        return "ACTOFF"
    return None


# ---------------------------------------------------------------------------
# 입력 문장 delexicalize
# ---------------------------------------------------------------------------
class _Span:
    __slots__ = ("s", "e", "tag", "payload")

    def __init__(self, s, e, tag, payload):
        self.s, self.e, self.tag, self.payload = s, e, tag, payload


class TemplateMatcher:
    """pattern_library 를 로드해 문장을 covered 골드 문형으로 매핑한다.

    match(sentence) → {model, matched_id, score, mode:"slot_fill"|"struct_replace"} | None
    """

    def __init__(self, pattern_library: list, gazetteer, inventory: dict,
                 tau: float = 0.72, tau_margin: float = 0.05):
        self.gz = gazetteer
        self.inventory = inventory or {}
        self.tau = tau
        self.tau_margin = tau_margin
        # A6 오버레이 어휘(매처 delexicalize 용) — 최장일치 우선.
        self._extra_devices = sorted(_A6_CONCEPTS.items(), key=lambda kv: -len(kv[0]))
        self._index = self._build_index(pattern_library)

    # ---- 인덱스: covered/partial 템플릿만(§2.6 게이트2: gap 제외) ----
    def _build_index(self, library: list) -> list:
        idx = []
        for tpl in library or []:
            if tpl.get("status") not in ("covered", "partial"):
                continue
            template = tpl.get("template", "")
            gold = tpl.get("gold", {})
            if not template or not gold:
                continue
            stream, content_slots, scope_slot = self._template_stream(template)
            content_tags = tuple(t for t in stream if t in _CONTENT_TAGS)
            idx.append({
                "id": tpl.get("id"),
                "status": tpl.get("status"),
                "stream": stream,
                "content_slots": content_slots,   # [(name, tag), ...] 순서
                "content_tags": content_tags,
                "content_tag_set": frozenset(content_tags),
                "scope_slot": scope_slot,
                "gold": gold,
                "area": tpl.get("area", ""),
            })
        # covered 우선(동률 완전일치 시 안정적 선택), 그다음 원래 순서.
        idx.sort(key=lambda r: 0 if r["status"] == "covered" else 1)
        return idx

    # ---- 템플릿 문자열 → (스트림, 내용슬롯열, scope슬롯유무) ----
    def _template_stream(self, template: str):
        stream: list = []
        content_slots: list = []
        scope_slot = False
        pos = 0
        for m in re.finditer(r"\{([^}]*)\}|<([^>]*)>", template):
            lit = template[pos:m.start()]
            self._emit_literal_tokens(lit, stream)
            pos = m.end()
            if m.group(1) is not None:      # {slot} 또는 {josa}
                name = m.group(1).strip()
                if name in _JOSA_TOKENS:
                    continue
                if name == "scope":
                    scope_slot = True
                    stream.append("SCOPE")
                    continue
                tag = _SLOT_TAG.get(name)
                if tag:
                    content_slots.append((name, tag))
                    stream.append(tag)
                # 알 수 없는 슬롯은 무시
            else:                            # <on>/<off> 확장 규칙
                rule = (m.group(2) or "").strip()
                if rule == "on":
                    stream.append("ACTON")
                elif rule == "off":
                    stream.append("ACTOFF")
        self._emit_literal_tokens(template[pos:], stream)
        return stream, content_slots, scope_slot

    def _emit_literal_tokens(self, lit: str, stream: list):
        for w in lit.split():
            # bare 모션 리터럴(재참조) → MOT 참조 토큰(슬롯 아님, 위치만 차지)
            if any(mw in w for mw in _MOTION_WORDS):
                stream.append("MOT")
                continue
            sym = _sym(w)
            if sym:
                stream.append(sym)

    def _entity_tag(self, eid: str) -> str:
        """엔티티 id → 내용 태그. 모션/재실 센서는 MOT, 사람은 PER, 나머지 기기는 D."""
        e = self.gz.entity(eid)
        if not e:
            return "D"
        dom = e.get("domain")
        if dom == "binary_sensor" and e.get("device_class") in ("motion", "occupancy",
                                                                "presence"):
            return "MOT"
        if dom == "person":
            return "PER"
        return "D"

    # ---- 입력 문장 → 스팬 목록(내용 스팬 태깅 + 병합) ----
    def _collect_spans(self, sentence: str) -> list:
        spans: list = []
        occupied = [False] * len(sentence)

        def claim(s, e):
            if any(occupied[s:e]):
                return False
            for i in range(s, e):
                occupied[i] = True
            return True

        # 0) 모드 선점(최장일치): 모드 표면형이 엔티티 이름(scene "슬립 모드")과 겹치므로
        #    구조상 모드로 확정하기 위해 gazetteer.match 보다 먼저 claim 한다.
        for surf in sorted(self.gz.mode_surfaces, key=len, reverse=True):
            start = 0
            while True:
                idx = sentence.find(surf, start)
                if idx < 0:
                    break
                s, e = idx, idx + len(surf)
                if claim(s, e):
                    canon = self.gz.mode_canonical.get(surf, surf)
                    spans.append(_Span(s, e, "M",
                                       {"type": "mode", "text": surf,
                                        "candidates": [{"id": canon}]}))
                start = e

        # 1) gazetteer.match (별칭/이름/방/사람/기기어/시간대/수치). 모션 센서 이름은 MOT 로.
        for sp in self.gz.match(sentence):
            typ = sp.get("type")
            tag = _TYPE_TAG.get(typ)
            if not tag:
                continue
            s, e = sp["start"], sp["end"]
            if not claim(s, e):
                continue
            if typ == "entity":
                cands = sp.get("candidates") or []
                if cands:
                    tag = self._entity_tag(cands[0]["id"])
            elif typ == "device_word":
                concept = sp.get("concept") or {}
                if concept.get("domain") == "binary_sensor":
                    tag = "MOT"
            payload = {"type": typ, "text": sp.get("text"),
                       "concept": sp.get("concept"), "value": sp.get("value"),
                       "candidates": sp.get("candidates")}
            spans.append(_Span(s, e, tag, payload))

        # 2) A6 오버레이 기기어(무드등/메인등/등) — 미점유 영역에서 최장일치.
        for surf, concept in self._extra_devices:
            start = 0
            while True:
                idx = sentence.find(surf, start)
                if idx < 0:
                    break
                s, e = idx, idx + len(surf)
                if claim(s, e):
                    spans.append(_Span(s, e, "D",
                                       {"type": "device_word", "text": surf,
                                        "concept": concept, "candidates": []}))
                start = e

        # 3) 남은 지속시간(find_duration) — 미점유만.
        pos = 0
        while True:
            info = find_duration(sentence[pos:])
            if not info:
                break
            s = pos + info["span"][0]
            e = pos + info["span"][1]
            if claim(s, e):
                spans.append(_Span(s, e, "DUR",
                                   {"type": "duration", "value": info}))
            pos = e

        spans.sort(key=lambda sp: sp.s)
        return self._merge_area_device(spans, sentence)

    def _merge_area_device(self, spans: list, sentence: str) -> list:
        """인접 A(area) + D/MOT 병합: 방 스팬 뒤에 곧바로 기기/모션이 오면 방을 흡수해
        하나의 기기/모션 슬롯으로 만든다("거실 조명" = 단일 D). area 는 컨텍스트로 보관."""
        out: list = []
        i = 0
        while i < len(spans):
            cur = spans[i]
            if cur.tag == "A":
                nxt = spans[i + 1] if i + 1 < len(spans) else None
                between = sentence[cur.e:nxt.s] if nxt else ""
                if nxt and nxt.tag in ("D", "MOT") and between.strip() == "":
                    nxt.payload = dict(nxt.payload or {})
                    nxt.payload["area_id"] = cur.payload.get("candidates", [{}])[0].get("id") \
                        if cur.payload.get("candidates") else self._area_of(cur, sentence)
                    out.append(nxt)
                    i += 2
                    continue
                # 단독 방 → 스트림에서 무시(A 는 내용 태그 아님)
                i += 1
                continue
            out.append(cur)
            i += 1
        return out

    def _area_of(self, area_span: _Span, sentence: str):
        cands = area_span.payload.get("candidates") or []
        return cands[0]["id"] if cands else None

    def delexicalize(self, sentence: str):
        """문장 → (스트림, 내용스팬열). 스트림 = 내용태그 + 기능심볼(순서 보존)."""
        spans = self._collect_spans(sentence)
        stream: list = []
        content_spans: list = []
        pos = 0
        for sp in spans:
            self._emit_literal_tokens(sentence[pos:sp.s], stream)
            stream.append(sp.tag)
            if sp.tag in _CONTENT_TAGS:
                content_spans.append(sp)
            pos = sp.e
        self._emit_literal_tokens(sentence[pos:], stream)
        return stream, content_spans

    # ---- 슬롯 바인딩 → gold 구체화 ----
    def _filler_for(self, sp: _Span):
        """내용 스팬 → concretize_gold 가 소비할 filler dict."""
        tag = sp.tag
        pl = sp.payload or {}
        if tag == "D":
            eid, domain, area = self._resolve_device(sp)
            return {"entity": eid, "domain": domain, "area": area}
        if tag == "MOT":
            eid, _dom, area = self._resolve_device(sp, motion=True)
            return {"entity": eid, "area": area}
        if tag == "M":
            cands = pl.get("candidates") or []
            val = cands[0]["id"] if cands else pl.get("text")
            canon = self.gz.mode_canonical.get(val, val)
            return {"value": canon}
        if tag == "SEG":
            cands = pl.get("candidates") or []
            return {"value": cands[0]["id"] if cands else None}
        if tag == "PCT":
            v = pl.get("value") or {}
            return {"value": v.get("value")}
        if tag == "DUR":
            v = pl.get("value") or {}
            secs = v.get("seconds")
            if secs is None and v.get("unit"):
                secs = v.get("value", 0) * {"초": 1, "분": 60, "시간": 3600}[v["unit"]]
            h = (secs or 0) // 3600
            mn = ((secs or 0) % 3600) // 60
            s = (secs or 0) % 60
            return {"dur": {"hours": h, "minutes": mn, "seconds": s}}
        if tag == "PER":
            cands = pl.get("candidates") or []
            return {"entity": cands[0]["id"] if cands else None}
        return {"surface": pl.get("text")}

    def _resolve_device(self, sp: _Span, motion: bool = False):
        pl = sp.payload or {}
        # 이름/별칭 매칭 스팬은 후보 id 직접 사용.
        if pl.get("type") == "entity":
            cands = pl.get("candidates") or []
            if cands:
                e = self.gz.entity(cands[0]["id"])
                return cands[0]["id"], (e["domain"] if e else None), \
                    (e.get("area_id") if e else None)
        concept = pl.get("concept") or (MOTION_CONCEPT if motion else None)
        area = pl.get("area_id")
        text = pl.get("text", "")
        if concept:
            cands = self.gz.resolve_concept(concept, area, text)
            if cands:
                e = self.gz.entity(cands[0]["id"])
                return cands[0]["id"], (concept.get("domain")
                                        or (e["domain"] if e else None)), \
                    (e.get("area_id") if e else area)
        return None, (concept.get("domain") if concept else None), area

    def _domain_ok(self, tpl: dict, content_spans: list) -> bool:
        """템플릿의 도메인 특정 기기 슬롯(device_light/fan/climate/media)에 바인딩될 입력
        D-스팬의 실제 도메인이 슬롯 도메인과 일치하는지 검사(오도메인 흡수 차단).

        _bind_and_concretize 와 동일한 태그정렬(by_tag 순서 + used 카운터)을 재현해, k번째
        D-슬롯에 실제로 들어갈 k번째 D-스팬의 도메인만 본다. 슬롯 도메인이 None(generic
        "device")이거나 입력 도메인을 확정 못 하면 통과(관용) — 확정된 불일치만 거부한다.
        """
        by_tag: dict = {}
        for sp in content_spans:
            by_tag.setdefault(sp.tag, []).append(sp)
        used: dict = {}
        for (name, tag) in tpl["content_slots"]:
            k = used.get(tag, 0)
            used[tag] = k + 1
            want = _SLOT_DOMAIN.get(name)
            if tag == "D" and want:
                q = by_tag.get(tag, [])
                if k < len(q):
                    _eid, dom, _area = self._resolve_device(q[k])
                    if dom and dom != want:
                        return False
        return True

    def _bind_and_concretize(self, tpl: dict, content_spans: list, sentence: str):
        """템플릿 내용슬롯 ↔ 입력 내용스팬 태그정렬 바인딩 → gold 구체화."""
        combo: dict = {}
        by_tag: dict = {}
        for sp in content_spans:
            by_tag.setdefault(sp.tag, []).append(sp)
        used = {k: 0 for k in by_tag}
        for (name, tag) in tpl["content_slots"]:
            q = by_tag.get(tag, [])
            k = used.get(tag, 0)
            if k < len(q):
                combo[name] = self._filler_for(q[k])
                used[tag] = k + 1
            else:
                combo[name] = {}
        if tpl["scope_slot"]:
            combo["scope"] = {"expand": True}
        concretized = concretize_gold(tpl["gold"], combo, self.inventory)
        model = {"alias": sentence.strip(), "description": "", "mode": "single"}
        model.update(concretized)
        return model

    # ---- 유사도(순서보존 LCS) ----
    @staticmethod
    def _lcs(a: list, b: list) -> int:
        if not a or not b:
            return 0
        prev = [0] * (len(b) + 1)
        for x in a:
            cur = [0] * (len(b) + 1)
            for j, y in enumerate(b, 1):
                cur[j] = prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1])
            prev = cur
        return prev[-1]

    def _sim(self, a: list, b: list) -> float:
        if not a and not b:
            return 1.0
        denom = max(len(a), len(b))
        return self._lcs(a, b) / denom if denom else 0.0

    @staticmethod
    def _func_multiset(stream: list) -> tuple:
        """스트림의 기능심볼(극성/구조) 멀티셋 — 내용 태그 제외, 정렬 튜플."""
        return tuple(sorted(t for t in stream if t not in _CONTENT_TAGS))

    # ---- 진입점 ----
    def match(self, sentence: str):
        stream, content_spans = self.delexicalize(sentence)
        if not stream:
            return None
        in_tag_set = frozenset(t for t in stream if t in _CONTENT_TAGS)
        in_func = self._func_multiset(stream)

        # ---- 1단: 스트림 완전일치(극성 기능어 포함) → slot_fill ----
        # 도메인 게이트: 기기 슬롯 도메인 ↔ 입력 기기 도메인 일치 후보만(오도메인 흡수 차단).
        exact = [r for r in self._index
                 if r["stream"] == stream and self._domain_ok(r, content_spans)]
        if exact:
            best = exact[0]  # 인덱스가 covered 우선 정렬 → 결정적
            model = self._bind_and_concretize(best, content_spans, sentence)
            return {"model": model, "matched_id": best["id"], "score": 1.0,
                    "mode": "slot_fill"}

        # ---- 2단: 순서보존 유사도 τ, 오탐 3게이트 → struct_replace ----
        # 극성 게이트: 기능심볼 멀티셋이 같아야 한다(ACTON↔ACTOFF·TRIGON↔TRIGOFF 교차 차단).
        scored = []
        for r in self._index:
            if self._func_multiset(r["stream"]) != in_func:
                continue
            # 게이트3: 입력 구조태그 ⊆ 템플릿 구조태그(과잉 구조 차단)
            if not in_tag_set.issubset(r["content_tag_set"]):
                continue
            # 게이트4: 기기 슬롯 도메인 ↔ 입력 기기 도메인 일치(오도메인 흡수 차단)
            if not self._domain_ok(r, content_spans):
                continue
            sc = self._sim(stream, r["stream"])
            scored.append((sc, r))
        if not scored:
            return None
        scored.sort(key=lambda t: (-t[0], 0 if t[1]["status"] == "covered" else 1))
        best_sc, best = scored[0]
        if best_sc < self.tau:
            return None
        # 게이트1: 마진(2등과 τ_margin 이상 차이 — 골드가 다른 모호 매칭 차단)
        if len(scored) > 1:
            runner_sc, runner = scored[1]
            if best_sc - runner_sc < self.tau_margin and runner["gold"] != best["gold"]:
                return None
        model = self._bind_and_concretize(best, content_spans, sentence)
        return {"model": model, "matched_id": best["id"], "score": round(best_sc, 3),
                "mode": "struct_replace"}
