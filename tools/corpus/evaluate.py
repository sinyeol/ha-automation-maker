"""§3.4 — 현재 파서를 구동해 코퍼스 커버리지 측정 + 리포트 생성.

- build_inventory(): MockHAClient → merge_inventory + 모드 포함 settings + Gazetteer.
- evaluate(): 각 item 을 parse() (예외 캡처) → structural_match.compare → 누적.
- 출력: out/results.jsonl (item별), out/coverage_report.md (영역·템플릿·소스별 정확률, §7.7).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# --- 앱 import 루트 배선 (읽기 전용) --------------------------------------
_APP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from backend.ha_client import merge_inventory  # noqa: E402
from backend.mock_data import MockHAClient  # noqa: E402
from backend.nl.gazetteer import Gazetteer  # noqa: E402
from backend.nl.parser import parse  # noqa: E402

try:
    from . import structural_match as _sm
except ImportError:  # pragma: no cover
    import structural_match as _sm


# ---------------------------------------------------------------------------
# settings: app.py _default_settings() 의 미러(앱 미import — 앱 수정 금지 규약).
# DEV mock 인벤토리(person.wife·scene.sleep_mode)에 맞춘 기본값.
# ---------------------------------------------------------------------------
def default_settings() -> dict:
    return {
        "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                     "evening": "17:00", "night": "21:00"},
        "persons": {"나": "person.user", "와이프": "person.wife"},
        "modes": {"슬립 모드": {"initial": "off",
                              "on_action": {"action": "scene.turn_on",
                                            "target": {"entity_id": ["scene.sleep_mode"]}},
                              "off_action": None}},
        "near_home": {"zone_state": "home"},
        "aliases": [],
        "confirm_actions": ["lock", "valve"],
        "llm": {"backend": "off"},
    }


def _extract_zones(states: list) -> list:
    # app.extract_zones 미러(앱 import 회피). zone.* 상태를 별도 목록으로.
    out = []
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith("zone."):
            continue
        attrs = s.get("attributes", {}) or {}
        out.append({"entity_id": eid, "name": attrs.get("friendly_name") or eid})
    out.sort(key=lambda z: z["name"])
    return out


def build_inventory():
    """(inventory, Gazetteer, settings) 반환."""
    ha = MockHAClient()

    async def _fetch():
        return await ha.fetch_registries(), await ha.get_states()

    reg, states = asyncio.run(_fetch())
    inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
    inv["zones"] = _extract_zones(states)
    settings = default_settings()
    gz = Gazetteer.build(inv, settings)
    return inv, gz, settings


# ---------------------------------------------------------------------------
# 평가
# ---------------------------------------------------------------------------
def evaluate(corpus, gazetteer, settings, inventory) -> dict:
    by_verdict = {"exact": 0, "partial": 0, "fail": 0}
    by_area = {}
    by_template = {}
    by_source = {}
    items_out = []
    gold_invalid = 0

    for item in corpus:
        if item.get("gold_invalid"):
            gold_invalid += 1
            items_out.append({"item": item, "verdict": "gold_invalid",
                              "diff": [], "actual_ok": None, "confidence": None,
                              "exception": None})
            continue

        exc = None
        actual = None
        try:
            actual = parse(item["sentence"], gazetteer, settings)
        except Exception as e:  # noqa: BLE001 — 파서 예외는 fail 로 캡처
            exc = f"{type(e).__name__}: {e}"

        if exc is not None:
            cmp = {"verdict": "fail", "diff": [{"tag": "exception"}],
                   "trigger_match": False, "cond_match": False,
                   "action_match": False, "subrule_count_match": False}
            actual_ok = False
            conf = None
        else:
            cmp = _sm.compare(item["gold"], actual["model"])
            actual_ok = bool(actual.get("ok"))
            conf = actual.get("confidence")
            if not actual_ok and cmp["verdict"] == "exact":
                # ok=False(미해결 칩)면 exact 이라도 partial 로 낮춘다(저장 불가).
                cmp = dict(cmp, verdict="partial")

        v = cmp["verdict"]
        by_verdict[v] = by_verdict.get(v, 0) + 1
        area = item.get("area", "?")
        tid = item.get("template_id", "?")
        src = item.get("source", "?")
        by_area.setdefault(area, {"exact": 0, "partial": 0, "fail": 0})[v] += 1
        by_template.setdefault(tid, {"exact": 0, "partial": 0, "fail": 0})[v] += 1
        by_source.setdefault(src, {"exact": 0, "partial": 0, "fail": 0})[v] += 1

        items_out.append({"item": item, "verdict": v, "diff": cmp["diff"],
                          "actual_ok": actual_ok, "confidence": conf,
                          "exception": exc,
                          "trigger_match": cmp["trigger_match"],
                          "cond_match": cmp["cond_match"],
                          "action_match": cmp["action_match"]})

    evaluated = sum(by_verdict.values())
    return {"total": len(corpus), "evaluated": evaluated,
            "gold_invalid": gold_invalid, "by_verdict": by_verdict,
            "by_area": by_area, "by_template": by_template,
            "by_source": by_source, "items": items_out}


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
def write_results_jsonl(result: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in result["items"]:
            it = row["item"]
            rec = {"id": it.get("id"), "sentence": it.get("sentence"),
                   "source": it.get("source"), "area": it.get("area"),
                   "template_id": it.get("template_id"), "verdict": row["verdict"],
                   "actual_ok": row["actual_ok"], "confidence": row["confidence"],
                   "exception": row["exception"], "diff": row["diff"],
                   "gold": it.get("gold")}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _pct(d: dict) -> str:
    tot = sum(d.get(k, 0) for k in ("exact", "partial", "fail"))
    if not tot:
        return "-"
    return f"{100.0 * d.get('exact', 0) / tot:.1f}%"


def _acc_table(title, mapping) -> str:
    lines = [f"### {title}", "",
             "| 항목 | exact | partial | fail | 정확률(exact) |",
             "|---|---:|---:|---:|---:|"]
    for key in sorted(mapping):
        d = mapping[key]
        lines.append(f"| {key} | {d.get('exact',0)} | {d.get('partial',0)} "
                     f"| {d.get('fail',0)} | {_pct(d)} |")
    lines.append("")
    return "\n".join(lines)


def write_coverage_report(result: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bv = result["by_verdict"]
    lines = ["# 커버리지 리포트 (현재 파서 기준)", "",
             f"- 코퍼스 총 {result['total']}문장 "
             f"(평가 {result['evaluated']} · gold_invalid 제외 {result['gold_invalid']})",
             f"- exact {bv.get('exact',0)} · partial {bv.get('partial',0)} "
             f"· fail {bv.get('fail',0)} · 전체 정확률 {_pct(bv)}", ""]
    lines.append(_acc_table("영역별", result["by_area"]))
    lines.append(_acc_table("템플릿별", result["by_template"]))
    lines.append(_acc_table("소스별 (grammar/paraphrase 분리, §7.7)", result["by_source"]))
    lines.append("> §7.7 낙관편향: grammar 문장은 템플릿과 동형이라 커버리지가 낙관적이다. "
                 "실제 일반화 성능은 **paraphrase 소스 정확률**을 우선 본다.\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
