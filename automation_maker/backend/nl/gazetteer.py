"""표면형 사전(gazetteer): 문장 텍스트 → 방/엔티티/기기어/사람/모드/시간대/수치 스팬.

의존성 0 (표준 라이브러리만). 형태소 분석기 금지 — 사전 최장일치 + 조사 스트리핑.
"""
from __future__ import annotations

from typing import Optional

from .normalize import (choseong, find_clock, find_duration, find_percent,
                        find_temperature, token_boundary_ok)

# ---------------------------------------------------------------------------
# 내장 동의어 사전 (기기 개념). surface → concept.
# concept: {"domain", "device_class"?, "hint"?, "label"}
# ---------------------------------------------------------------------------
DEVICE_CONCEPTS: dict[str, dict] = {
    "조명": {"domain": "light", "label": "조명"},
    "불": {"domain": "light", "label": "조명"},
    "전등": {"domain": "light", "label": "조명"},
    "라이트": {"domain": "light", "label": "조명"},
    "에어컨": {"domain": "climate", "hint": "cool", "label": "에어컨"},
    "에어콘": {"domain": "climate", "hint": "cool", "label": "에어컨"},
    "냉방": {"domain": "climate", "hint": "cool", "label": "냉방"},
    "보일러": {"domain": "climate", "hint": "heat", "label": "보일러"},
    "난방": {"domain": "climate", "hint": "heat", "label": "난방"},
    "환풍기": {"domain": "fan", "label": "환풍기"},
    "환기팬": {"domain": "fan", "label": "환풍기"},
    "전열교환기": {"domain": "fan", "label": "전열교환기"},
    "커튼": {"domain": "cover", "label": "커튼"},
    "블라인드": {"domain": "cover", "label": "블라인드"},
    "티비": {"domain": "media_player", "label": "TV"},
    "텔레비전": {"domain": "media_player", "label": "TV"},
    "도어락": {"domain": "lock", "label": "도어락"},
    "콘센트": {"domain": "switch", "label": "콘센트"},
    "온도": {"domain": "sensor", "device_class": "temperature", "label": "온도"},
    "습도": {"domain": "sensor", "device_class": "humidity", "label": "습도"},
    "미세먼지": {"domain": "sensor", "device_class": "pm25", "label": "미세먼지"},
}
# 모션류 감지 개념
MOTION_WORDS = ["움직임", "모션", "인기척", "동작", "재실"]
MOTION_CONCEPT = {"domain": "binary_sensor",
                  "device_class": ["motion", "occupancy", "presence"], "label": "움직임"}

# 방 동의어: surface → 대상 area 이름
ROOM_SYNONYMS = {
    "화장실": "욕실", "침실": "안방", "큰방": "안방", "안방": "안방",
    "거실": "거실", "주방": "주방", "부엌": "주방", "현관": "현관",
    "베란다": "베란다", "발코니": "베란다", "작은방": "작은방", "욕실": "욕실",
}

# 시간대 단어
SEGMENT_WORDS = {"새벽": "dawn", "아침": "morning", "낮": "day",
                 "저녁": "evening", "밤": "night"}

# 요일 구분 / 계절 단어 (SPEC-V2 §3 day_type / season 노드)
DAY_TYPE_WORDS = {"주말": "weekend", "평일": "weekday",
                  "공휴일": "holiday", "휴일": "holiday"}
SEASON_WORDS = {"봄": "spring", "여름": "summer",
                "가을": "autumn", "겨울": "winter"}

# climate hint 우선 키워드
_HINT_KEYWORDS = {"cool": ("에어컨", "냉방", "쿨", "ac"),
                  "heat": ("보일러", "난방", "히터", "온돌")}


class Gazetteer:
    def __init__(self, inventory: dict, settings: dict):
        self.inventory = inventory
        self.settings = settings or {}
        self.entities: list[dict] = inventory.get("entities", [])
        self.areas: list[dict] = inventory.get("areas", [])
        self.zones: list[dict] = inventory.get("zones", [])
        self._by_id = {e["entity_id"]: e for e in self.entities}

        # area 이름 → id
        self.area_name_to_id: dict[str, str] = {}
        for a in self.areas:
            self.area_name_to_id[a["name"]] = a["area_id"]
        # 방 표면형(동의어 포함) → area_id
        self.room_surfaces: dict[str, str] = {}
        for surface, canon in ROOM_SYNONYMS.items():
            aid = self.area_name_to_id.get(canon)
            if aid:
                self.room_surfaces[surface] = aid
        for a in self.areas:
            self.room_surfaces.setdefault(a["name"], a["area_id"])

        # 엔티티 이름 표면형(공백 제거 변형 포함) → entity_id 리스트
        self.entity_surfaces: dict[str, list[str]] = {}
        for e in self.entities:
            name = e.get("name") or e["entity_id"]
            for form in {name, name.replace(" ", "")}:
                self.entity_surfaces.setdefault(form, []).append(e["entity_id"])

        # 사람: settings.persons 오버레이 (표면형 → person entity_id)
        self.person_surfaces: dict[str, str] = {}
        for surf, pid in (self.settings.get("persons") or {}).items():
            if pid:
                self.person_surfaces[surf] = pid
        # 모드: settings.modes
        self.mode_surfaces: dict[str, dict] = {}
        for name, spec in (self.settings.get("modes") or {}).items():
            self.mode_surfaces[name] = spec
            self.mode_surfaces.setdefault(name.replace(" ", ""), spec)
        # 별칭: settings.aliases 오버레이(항상 우선)
        self.alias_surfaces: dict[str, str] = {}
        for al in (self.settings.get("aliases") or []):
            surf, eid = al.get("surface"), al.get("entity_id")
            if surf and eid:
                self.alias_surfaces[surf] = eid

        # 초성 인덱스(엔티티)
        self._cho_index = [(choseong(e.get("name") or ""), e) for e in self.entities]

    @classmethod
    def build(cls, inventory: dict, settings: dict) -> "Gazetteer":
        return cls(inventory, settings)

    # ------------------------------------------------------------------
    # 개념 → 엔티티 후보 해석 (area 맥락 부스팅 포함)
    # ------------------------------------------------------------------
    def resolve_concept(self, concept: dict, area_id: Optional[str] = None,
                        name_text: Optional[str] = None) -> list[dict]:
        domain = concept.get("domain")
        dc = concept.get("device_class")
        dc_set = set(dc) if isinstance(dc, list) else ({dc} if dc else None)
        hint = concept.get("hint")

        cands = []
        for e in self.entities:
            if domain and e["domain"] != domain:
                continue
            if dc_set is not None and e.get("device_class") not in dc_set:
                continue
            cands.append(e)

        # hint(climate 냉/난방) 우선 필터
        if hint and hint in _HINT_KEYWORDS:
            kws = _HINT_KEYWORDS[hint]
            pref = [e for e in cands if any(k in (e.get("name") or "").lower() or
                                            k in e["entity_id"].lower() for k in kws)]
            if pref:
                cands = pref

        # area 맥락: 해당 방에 매칭이 있으면 그 방으로 제한
        scored = []
        in_area = [e for e in cands if area_id and e.get("area_id") == area_id]
        pool = in_area if in_area else cands
        label = concept.get("label", "")
        for e in pool:
            score = 0.6
            reason = "기기 종류 일치"
            if area_id and e.get("area_id") == area_id:
                score += 0.2
                reason = f"{self._area_name(area_id)}의 {label}"
            # 이름 안에 개념 단어가 그대로 있으면 소폭 가산
            nm = e.get("name") or ""
            if label and label in nm:
                score += 0.05
            # '메인' 선호(대표 조명 등)
            main_bonus = 0.03 if ("메인" in nm or "main" in e["entity_id"]) else 0.0
            scored.append((score + main_bonus, e, reason))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [self._cand(e, min(sc, 0.99), reason) for sc, e, reason in scored]

    def resolve_name(self, text: str) -> list[dict]:
        """정확 이름/별칭 매칭."""
        t = text.strip()
        if t in self.alias_surfaces:
            e = self._by_id.get(self.alias_surfaces[t])
            if e:
                return [self._cand(e, 1.0, "별칭 일치")]
        ids = self.entity_surfaces.get(t) or self.entity_surfaces.get(t.replace(" ", ""))
        if ids:
            return [self._cand(self._by_id[i], 0.9, "이름 일치") for i in ids]
        return []

    def _cand(self, e: dict, score: float, reason: str) -> dict:
        sub = e.get("area_name") or "미배정"
        if e.get("device_name"):
            sub = f"{sub} · {e['device_name']}"
        return {"id": e["entity_id"], "label": e.get("name") or e["entity_id"],
                "sublabel": sub, "score": round(score, 3), "reason": reason}

    def _area_name(self, area_id: Optional[str]) -> Optional[str]:
        for a in self.areas:
            if a["area_id"] == area_id:
                return a["name"]
        return None

    def area_name(self, area_id):
        return self._area_name(area_id)

    def entity(self, entity_id: str) -> Optional[dict]:
        return self._by_id.get(entity_id)

    def entities_by_concept(self, concept: dict, area_id: Optional[str] = None,
                            except_area_id: Optional[str] = None) -> list[str]:
        """스코프 해석용: 개념에 맞는 전체 엔티티 id 목록."""
        domain = concept.get("domain")
        dc = concept.get("device_class")
        dc_set = set(dc) if isinstance(dc, list) else ({dc} if dc else None)
        out = []
        for e in self.entities:
            if domain and e["domain"] != domain:
                continue
            if dc_set is not None and e.get("device_class") not in dc_set:
                continue
            if area_id and e.get("area_id") != area_id:
                continue
            if except_area_id and e.get("area_id") == except_area_id:
                continue
            out.append(e["entity_id"])
        return out

    # ------------------------------------------------------------------
    # §6.1 match(): 문장 텍스트 → 스팬 목록 (프론트 칩/일반 API)
    # ------------------------------------------------------------------
    def match(self, text: str) -> list[dict]:
        spans: list[dict] = []
        occupied = [False] * len(text)

        def claim(s, e):
            if any(occupied[s:e]):
                return False
            for i in range(s, e):
                occupied[i] = True
            return True

        # 표면형 사전(최장일치): 별칭 > 엔티티 이름 > 방 > 사람 > 모드 > 기기어 > 시간대
        surfaces: list[tuple[str, str, object]] = []
        for surf, eid in self.alias_surfaces.items():
            surfaces.append((surf, "entity", [self._cand(self._by_id[eid], 1.0, "별칭 일치")]
                             if eid in self._by_id else []))
        for surf, ids in self.entity_surfaces.items():
            surfaces.append((surf, "entity",
                             [self._cand(self._by_id[i], 0.9, "이름 일치") for i in ids]))
        for surf, aid in self.room_surfaces.items():
            surfaces.append((surf, "area",
                             [{"id": aid, "label": self._area_name(aid), "score": 0.9}]))
        for surf, pid in self.person_surfaces.items():
            surfaces.append((surf, "person", [{"id": pid, "label": surf, "score": 1.0}]))
        for surf, spec in self.mode_surfaces.items():
            surfaces.append((surf, "mode", [{"id": surf, "label": surf, "score": 1.0, "spec": spec}]))
        for surf, concept in DEVICE_CONCEPTS.items():
            surfaces.append((surf, "device_word", concept))
        for surf in MOTION_WORDS:
            surfaces.append((surf, "device_word", MOTION_CONCEPT))
        for surf, seg in SEGMENT_WORDS.items():
            surfaces.append((surf, "segment", [{"id": seg, "label": surf, "score": 1.0}]))

        surfaces.sort(key=lambda t: len(t[0]), reverse=True)
        for surf, typ, payload in surfaces:
            start = 0
            while True:
                idx = text.find(surf, start)
                if idx < 0:
                    break
                s, e = idx, idx + len(surf)
                # 사람 표면형은 한글 토큰 경계를 지킬 때만 매칭 ('누나'의 '나' 거부)
                if typ == "person" and not token_boundary_ok(text, s, e, surf):
                    start = e
                    continue
                if claim(s, e):
                    sp = {"start": s, "end": e, "text": surf, "type": typ}
                    if typ == "device_word":
                        sp["concept"] = payload
                        sp["candidates"] = []
                    else:
                        sp["candidates"] = payload
                    spans.append(sp)
                start = e

        # 수치류(겹치지 않는 위치에서)
        for finder, typ in ((find_percent, "percent"), (find_temperature, "temperature"),
                            (find_clock, "clock"), (find_duration, "duration")):
            # 모든 발생 스캔
            pos = 0
            while True:
                sub = text[pos:]
                info = finder(sub)
                if not info:
                    break
                s = pos + info["span"][0]
                e = pos + info["span"][1]
                if claim(s, e):
                    spans.append({"start": s, "end": e, "text": text[s:e], "type": typ,
                                  "value": info, "candidates": []})
                pos = e

        spans.sort(key=lambda sp: sp["start"])
        return spans
