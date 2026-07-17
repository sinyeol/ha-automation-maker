"""§3.5 — 실패/부분 문장을 추상 템플릿으로 역치환·클러스터해 갭/패턴 라이브러리 생성.

- abstract_to_template: delexicalization — gazetteer 최장일치 + normalize find_* 로
  엔티티/방/모드/시간/값 스팬을 {DEVICE}{ROOM}{MODE}{TIME}{PERCENT}{TEMP}{DUR}{NUM} 로
  역치환, 조사는 {J} 로 정규화.
- mine_gaps: fail·partial 아이템을 추상문자열로 클러스터(exact → 근접 병합) 빈도순.
- 출력: out/gap_library.yaml(추가 후보 패턴) + out/pattern_library.yaml(시드 템플릿 커버 상태).
"""
from __future__ import annotations

import os
import re
import sys

import yaml

_APP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from backend.nl.normalize import (find_clock, find_duration,  # noqa: E402
                                  find_percent, find_temperature)

# span type(gazetteer.match) → 자리표시자
_TYPE_PLACEHOLDER = {
    "entity": "{DEVICE}", "person": "{DEVICE}", "device_word": "{DEVICE}",
    "area": "{ROOM}", "mode": "{MODE}", "segment": "{SEG}",
    "percent": "{PERCENT}", "temperature": "{TEMP}", "clock": "{TIME}",
    "duration": "{DUR}",
}

# 자리표시자 뒤에 붙는 조사 → {J} 로 정규화
_JOSA_AFTER = ("으로는", "에서는", "이라도", "에서", "에게", "한테", "으로", "이랑",
               "하고", "부터", "까지", "이고", "이면", "라면", "은", "는", "이",
               "가", "을", "를", "에", "의", "와", "과", "로", "도", "만", "고",
               "면", "라", "야")

_PLACEHOLDER = re.compile(r"\{[A-Z]+\}")


def abstract_to_template(item: dict, gazetteer) -> str:
    """문장 → 추상 템플릿 문자열(delexicalization)."""
    text = item.get("sentence", "")
    spans = []
    # 1) gazetteer 최장일치(비겹침) 스팬
    try:
        for sp in gazetteer.match(text):
            ph = _TYPE_PLACEHOLDER.get(sp.get("type"))
            if ph:
                spans.append((sp["start"], sp["end"], ph))
    except Exception:  # noqa: BLE001 — 방어적: match 실패해도 계속
        pass
    # 2) normalize find_* 로 남은 수치/시간(겹치지 않는 것만)
    occupied = [False] * len(text)
    for s, e, _ in spans:
        for i in range(s, min(e, len(text))):
            occupied[i] = True

    def _scan(finder, ph):
        pos = 0
        while True:
            info = finder(text[pos:])
            if not info:
                break
            s = pos + info["span"][0]
            e = pos + info["span"][1]
            if not any(occupied[s:e]):
                spans.append((s, e, ph))
                for i in range(s, e):
                    occupied[i] = True
            pos = e

    _scan(find_percent, "{PERCENT}")
    _scan(find_temperature, "{TEMP}")
    _scan(find_clock, "{TIME}")
    _scan(find_duration, "{DUR}")

    # 3) 남은 맨숫자 → {NUM}
    for m in re.finditer(r"\d+", text):
        if not any(occupied[m.start():m.end()]):
            spans.append((m.start(), m.end(), "{NUM}"))

    # 스팬 적용(뒤에서부터)
    spans.sort()
    out = text
    for s, e, ph in sorted(spans, key=lambda x: -x[0]):
        out = out[:s] + ph + out[e:]

    # 4) 조사 정규화: 자리표시자 바로 뒤 한글 조사 → {J}
    def _josa(m):
        placeholder = m.group(1)
        tail = m.group(2)
        if not tail:
            return placeholder
        for j in sorted(_JOSA_AFTER, key=len, reverse=True):
            if tail.startswith(j):
                return placeholder + "{J}" + tail[len(j):]
        return placeholder + tail

    out = re.sub(r"(\{[A-Z]+\})([가-힣]+)?", _josa, out)
    return re.sub(r"\s+", " ", out).strip()


# ---------------------------------------------------------------------------
# 클러스터
# ---------------------------------------------------------------------------
def _sim(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def mine_gaps(eval_result: dict, gazetteer, near_threshold: float = 0.85) -> list[dict]:
    """fail·partial 아이템 → 추상 템플릿 클러스터(빈도순)."""
    clusters = []  # {pattern, count, area, examples, sample_diff, verdicts:{}}
    for row in eval_result.get("items", []):
        if row["verdict"] not in ("fail", "partial"):
            continue
        item = row["item"]
        pat = abstract_to_template(item, gazetteer)
        # exact 매칭 → 없으면 근접 병합
        target = None
        for c in clusters:
            if c["pattern"] == pat or _sim(c["pattern"], pat) >= near_threshold:
                target = c
                break
        if target is None:
            target = {"pattern": pat, "count": 0, "area": item.get("area", "?"),
                      "examples": [], "sample_diff": row["diff"],
                      "verdicts": {"fail": 0, "partial": 0}}
            clusters.append(target)
        target["count"] += 1
        target["verdicts"][row["verdict"]] += 1
        if len(target["examples"]) < 5:
            target["examples"].append(item.get("sentence", ""))
        if not target["sample_diff"] and row["diff"]:
            target["sample_diff"] = row["diff"]
    clusters.sort(key=lambda c: c["count"], reverse=True)
    for c in clusters:
        c["verdict_mix"] = c.pop("verdicts")
    return clusters


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
def _diff_tags(diff) -> list[str]:
    return sorted({d.get("tag", "?") for d in (diff or [])})


def write_gap_library(clusters: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = [{"pattern": c["pattern"], "count": c["count"], "area": c["area"],
             "verdict_mix": c["verdict_mix"], "error_tags": _diff_tags(c["sample_diff"]),
             "examples": c["examples"]}
            for c in clusters]
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _cover_status(counts: dict) -> str:
    tot = sum(counts.get(k, 0) for k in ("exact", "partial", "fail"))
    if not tot:
        return "unknown"
    exact = counts.get("exact", 0)
    if exact == tot:
        return "covered"
    if exact >= tot * 0.5 or counts.get("partial", 0) > counts.get("fail", 0):
        return "partial"
    return "gap"


def write_pattern_library(templates: list[dict], eval_result: dict,
                          path: str) -> None:
    """시드 템플릿 전체 + 커버 상태 + 예시(하이브리드의 데이터 자산)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    by_template = eval_result.get("by_template", {})
    # 템플릿별 예시 문장 수집
    examples = {}
    for row in eval_result.get("items", []):
        tid = row["item"].get("template_id")
        examples.setdefault(tid, [])
        if len(examples[tid]) < 3:
            examples[tid].append(row["item"].get("sentence", ""))
    data = []
    for tpl in templates:
        tid = tpl.get("id")
        counts = by_template.get(tid, {})
        data.append({"id": tid, "area": tpl.get("area", ""),
                     "template": tpl.get("template", ""),
                     "tags": list(tpl.get("tags", [])),
                     "status": _cover_status(counts),
                     "counts": counts or {"exact": 0, "partial": 0, "fail": 0},
                     "examples": examples.get(tid, []),
                     "gold": tpl.get("gold", {})})
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
