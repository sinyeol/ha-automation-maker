"""APP-PORT-PLAN §5.2 / §6 (S0) — 앱 L1 정직 측정 + A/B 패리티 하네스.

held-out(heldout.yaml + paraphrases 명시 gold hard case)에 대해
  - 앱 L1       = backend.nl.parser.parse         (실제 앱이 실행하는 파서 — 측정 대상)
  - 오버레이 L1A = parser_overlay.parse_patched    (정직 held-out466 = 74.2% 목표 기준)
를 정직 채점기(structural_compare, 앱 tests 본을 셔임으로 재사용)로 exact 판정하고,
전체·카테고리별 exact% + 문장 단위 패리티 diff(verdict 불일치 = '의도된 차이' 후보)를 낸다.

또한 heldout_special.yaml 의 금지문(special:prohibition)을 앱 parse 로 전수 돌려
'정반대(forbidden) 액션 미생성' 안전 게이트를 검사한다(0/N 목표, APP-PORT-PLAN 게이트2).

이 하네스는 S0 기준선(현 앱 L1)과 S1 이후 리프트를 같은 방식으로 재현한다. 결정적
(Date/random 미사용). 앱 소스는 수정하지 않는다(측정 전용).
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
from datetime import datetime

import yaml

# --- 앱 import 루트 배선 (읽기 전용) --------------------------------------
_HERE = os.path.dirname(__file__)
_APP_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from backend.engine.rule_model import validate_rule_model  # noqa: E402
from backend.nl import pattern_match as _pm  # noqa: E402
from backend.nl.parser import parse  # noqa: E402

try:  # 패키지/스크립트 양쪽 실행 지원
    from . import evaluate as _ev
    from . import evaluate_hybrid as _eh
    from . import parser_overlay as _ov
    from . import structural_match as _sm
except ImportError:  # pragma: no cover
    import evaluate as _ev
    import evaluate_hybrid as _eh
    import parser_overlay as _ov
    import structural_match as _sm

_OUT = os.path.join(_HERE, "out")

# 측정 시점 고정(held-out 오염 아님). interval_anchor.anchor 는 앱 파서가 "주입된 now 가
# 속한 주의 월요일"로 **독립 산출**한다(postpass._monday_iso). gold 의 anchor 는 라벨 규약상
# 라벨 작성일(2026-07-17, 금)이 속한 주의 월요일 = 2026-07-13 으로 고정돼 있으므로, 측정
# 때도 같은 라벨주의 now 를 주입해 anchor 를 맞춘다. 이는 '측정 시점을 라벨주로 고정'하는
# 것일 뿐(월요일 계산은 파서가 스스로 함), gold 값을 파서에 흘리는 held-out 오염이 아니다.
_LABEL_NOW = datetime(2026, 7, 17, 12, 0, 0)


def _parse_app(sentence, gz, settings):
    """앱 parse 를 라벨주 now 주입으로 호출(달력 축 interval_anchor 결정성 측정용)."""
    return parse(sentence, gz, settings, now_fn=lambda: _LABEL_NOW)


# ---------------------------------------------------------------------------
# 코퍼스 로드(evaluate_hybrid 와 동일한 held-out 구성: heldout + para_hard)
# ---------------------------------------------------------------------------
def load_corpus():
    """(corpus, inventory, gz_base, gz_overlay, settings) 반환. gold_invalid 제외."""
    inventory, gz_base, settings = _ev.build_inventory()
    mode_names = list(settings.get("modes", {}).keys())
    gz_overlay = _ov.build_overlay_gazetteer(inventory, settings)

    try:
        from . import generate as _gen
    except ImportError:  # pragma: no cover
        import generate as _gen
    templates = _gen.load_templates(os.path.join(_HERE, "templates.yaml"))
    slots = _gen.load_slots(os.path.join(_HERE, "slots.yaml"))
    for t in templates:
        t["_slots"] = slots
    corpus = _eh.load_heldout(templates, inventory, mode_names)
    return corpus, inventory, gz_base, gz_overlay, settings


def _cat_of(item: dict, axis: str) -> str:
    if axis == "difficulty":
        return item.get("difficulty", "?")
    if axis == "dataset":
        return item.get("dataset", "?")
    if axis == "tag":
        for t in item.get("tags", []):
            if isinstance(t, str) and t.startswith("cat:"):
                return t.split(":", 1)[1]
        return "?"
    return item.get(axis, "?")


def _safe_parse(fn, *args) -> dict:
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 — 파서 예외는 빈 모델(non-exact)
        return {"ok": False, "model": {}, "unmatched": [], "confidence": 0.0}


def _exact(gold: dict, model: dict) -> bool:
    try:
        return _sm.compare(gold, model)["verdict"] == "exact"
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 측정
# ---------------------------------------------------------------------------
def measure(corpus, gz_base, gz_overlay, settings, axes=("dataset", "difficulty", "tag")):
    """앱 L1 vs 오버레이 L1A 를 정직 채점. per-category 카운터 + 패리티 diff 목록 반환."""
    overall = {"L1": 0, "L1A": 0, "n": 0}
    by_cat = {ax: {} for ax in axes}
    parity_diffs = []   # 앱 L1 과 오버레이 L1A 의 verdict 가 갈리는 문장(의도된 차이 후보)
    app_regressions = []  # 앱 L1 exact 인데 오버레이는 non-exact (드묾 — 감시용)

    for it in corpus:
        s, gold = it["sentence"], it["gold"]
        m1 = _safe_parse(_parse_app, s, gz_base, settings).get("model", {})
        m1a = _safe_parse(_ov.parse_patched, s, gz_overlay, settings).get("model", {})
        e1 = _exact(gold, m1)
        e1a = _exact(gold, m1a)

        overall["n"] += 1
        overall["L1"] += int(e1)
        overall["L1A"] += int(e1a)
        for ax in axes:
            key = _cat_of(it, ax)
            c = by_cat[ax].setdefault(key, {"L1": 0, "L1A": 0, "n": 0})
            c["n"] += 1
            c["L1"] += int(e1)
            c["L1A"] += int(e1a)

        if e1 != e1a:
            parity_diffs.append({"id": it.get("id"), "sentence": s,
                                 "app_L1": "exact" if e1 else "miss",
                                 "overlay_L1A": "exact" if e1a else "miss"})
            if e1 and not e1a:
                app_regressions.append(it.get("id"))

    return {"overall": overall, "by_cat": by_cat,
            "parity_diffs": parity_diffs, "app_regressions": app_regressions}


# ---------------------------------------------------------------------------
# 금지문 안전 게이트(APP-PORT-PLAN 게이트2) — heldout_special.yaml special:prohibition
# ---------------------------------------------------------------------------
def load_prohibitions(path=None):
    """heldout_special.yaml 의 special:prohibition 항목만 로드 → [(id, sentence, forbidden)]."""
    path = path or os.path.join(_HERE, "heldout_special.yaml")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    out = []
    for entry in data:
        tags = entry.get("tags", []) or []
        if "special:prohibition" not in tags:
            continue
        forbidden = (entry.get("gold") or {}).get("forbidden") or []
        for var in entry.get("variants", []) or []:
            sent = var.get("text") if isinstance(var, dict) else str(var)
            out.append((entry.get("id"), sent, forbidden))
    return out


def _emitted_actions(model: dict):
    """model 의 모든 service 액션 (action, set(entity_id)) 목록."""
    if not isinstance(model, dict):
        return []
    subs = model.get("subrules")
    subs = subs if isinstance(subs, list) else [model]
    out = []
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        for a in sub.get("actions", []) or []:
            act = a.get("action")
            if not act:
                continue
            tgt = a.get("target") or {}
            ids = tgt.get("entity_id") if isinstance(tgt, dict) else None
            if isinstance(ids, str):
                ids = [ids]
            out.append((act, set(ids or [])))
    return out


def scan_prohibitions(gz, settings, entries, parse_fn=parse):
    """각 금지문을 parse 로 돌려 forbidden 액션이 방출되는지 검사.

    반환: {"total", "misfires":[{id,sentence,forbidden_hit}], "ok_true":[ids]}.
      - misfire = forbidden 의 action(+지정 시 entity_id) 이 실제 액션으로 방출됨(안전사고).
      - ok_true = parse 가 ok=True 를 냄(금지문인데 자동화 생성 — §5.4 강판정).
    """
    misfires, ok_true = [], []
    for rid, sentence, forbidden in entries:
        res = _safe_parse(parse_fn, sentence, gz, settings)
        emitted = _emitted_actions(res.get("model", {}))
        if res.get("ok"):
            ok_true.append(rid)
        hit = None
        for fb in forbidden:
            fact = fb.get("action")
            feid = fb.get("entity_id")
            for act, ids in emitted:
                if act == fact and (feid is None or feid in ids):
                    hit = fb
                    break
            if hit:
                break
        if hit is not None:
            misfires.append({"id": rid, "sentence": sentence, "forbidden_hit": hit})
    return {"total": len(entries), "misfires": misfires, "ok_true": ok_true}


# ---------------------------------------------------------------------------
# §4 (S9) — 앱 L1 vs 앱 L1+L2 정직 측정(순리프트 · l2_regressions)
#   런타임과 동일한 pattern_match.l2_gate 를 태워 exact% 를 재산정한다(단일 소스).
#   learned_lookup=None(held-out 에는 학습 항목 없음). 정직 채점기(structural_compare)로 exact.
# ---------------------------------------------------------------------------
def build_matcher(gz_base, inventory, library_path=None):
    lib = _pm.load_pattern_library(library_path)
    return _pm.TemplateMatcher(lib, gz_base, inventory), len(lib)


def measure_l2(corpus, gz_base, inventory, settings, matcher, enable_shadow=None):
    mode_names = set(settings.get("modes", {}))

    def _validate(m):
        return validate_rule_model(m, inventory, mode_names)

    counts = {"L1": 0, "L1L2": 0, "n": 0}
    regressions, gains = [], []
    shadow = {"tried": 0, "adopted": 0}
    used = {}
    for it in corpus:
        s, gold = it["sentence"], it["gold"]
        r1 = _safe_parse(_parse_app, s, gz_base, settings)
        e1 = _exact(gold, r1.get("model", {}))
        r2 = copy.deepcopy(r1)
        info = _pm.l2_gate(r2, s, matcher=matcher, validate=_validate,
                           learned_lookup=None, enable_shadow=enable_shadow)
        e2 = _exact(gold, r2.get("model", {}))
        counts["n"] += 1
        counts["L1"] += int(e1)
        counts["L1L2"] += int(e2)
        shadow["tried"] += int(info["shadow_tried"])
        shadow["adopted"] += int(info["shadow_adopted"])
        if info["used"]:
            used[info["used"]] = used.get(info["used"], 0) + 1
        if e1 and not e2:
            regressions.append({"id": it.get("id"), "sentence": s,
                                "used": info["used"], "matched_id": info["matched_id"]})
        elif e2 and not e1:
            gains.append({"id": it.get("id"), "sentence": s,
                          "used": info["used"], "matched_id": info["matched_id"]})
    return {"counts": counts, "regressions": regressions, "gains": gains,
            "shadow": shadow, "used": used}


def scan_prohibitions_gate(gz, inventory, settings, entries, matcher):
    """런타임 게이트(L1+L2)로 금지문을 돌려 forbidden 액션 방출을 검사한다(gate #3).

    parse 결과에 l2_gate 를 적용한 뒤 방출 액션을 본다 — 매처/학습이 금지문을 흡수해 정반대
    액션을 만들지 않는지 확인(scan_prohibitions 의 L1 전용 검사보다 강함)."""
    mode_names = set(settings.get("modes", {}))

    def _validate(m):
        return validate_rule_model(m, inventory, mode_names)

    misfires, ok_true = [], []
    for rid, sentence, forbidden in entries:
        res = _safe_parse(parse, sentence, gz, settings)
        _pm.l2_gate(res, sentence, matcher=matcher, validate=_validate,
                    learned_lookup=None)
        emitted = _emitted_actions(res.get("model", {}))
        if res.get("ok"):
            ok_true.append(rid)
        hit = None
        for fb in forbidden:
            fact, feid = fb.get("action"), fb.get("entity_id")
            for act, ids in emitted:
                if act == fact and (feid is None or feid in ids):
                    hit = fb
                    break
            if hit:
                break
        if hit is not None:
            misfires.append({"id": rid, "sentence": sentence, "forbidden_hit": hit})
    return {"total": len(entries), "misfires": misfires, "ok_true": ok_true}


def format_l2_report(res, prohib_gate, lib_n, show=0) -> str:
    c = res["counts"]
    L1 = _pct(c, "L1")
    L1L2 = _pct(c, "L1L2")
    lift = L1L2 - L1
    L = []
    L.append("# 앱 L1 vs 앱 L1+L2 정직 측정 (parity_check --l2, S9)")
    L.append("")
    L.append(f"- held-out n = {c['n']}  ·  pattern_library 템플릿 {lib_n}개")
    L.append(f"- **앱 L1        exact = {L1:.1f}% ({c['L1']}/{c['n']})**")
    L.append(f"- **앱 L1+L2     exact = {L1L2:.1f}% ({c['L1L2']}/{c['n']})**")
    L.append(f"- **L2 순리프트 = {lift:+.1f}%p ({c['L1L2']-c['L1']:+d}문장)**")
    L.append(f"- **l2_regressions = {len(res['regressions'])}** (L1 exact→L1+L2 non-exact, 0 이어야 함)")
    L.append(f"- l2_gains = {len(res['gains'])} (L1 miss→L1+L2 exact)")
    L.append(f"- 채택 분포 = {res['used']}  ·  shadow(시도/채택) = "
             f"{res['shadow']['tried']}/{res['shadow']['adopted']}")
    L.append("")
    L.append("## 금지문 게이트(L1+L2, gate #3)")
    L.append(f"- 전수 {prohib_gate['total']}문장 · **forbidden 방출(misfire) = "
             f"{len(prohib_gate['misfires'])}/{prohib_gate['total']}** · "
             f"ok=True = {len(prohib_gate['ok_true'])}")
    for mf in prohib_gate["misfires"]:
        L.append(f"  - ⚠️ {mf['id']}: {mf['sentence']} → {mf['forbidden_hit']}")
    L.append("")
    if res["regressions"]:
        L.append("## ⚠️ 회귀(L1 exact 를 L2 가 덮음) — 0 이어야 함")
        for d in res["regressions"]:
            L.append(f"  - [{d['used']}/{d['matched_id']}] {d['id']}: {d['sentence']}")
        L.append("")
    if show and res["gains"]:
        L.append(f"## L2 이득 상위 {show}")
        for d in res["gains"][:show]:
            L.append(f"  - [{d['used']}/{d['matched_id']}] {d['id']}: {d['sentence']}")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------
def _pct(c, key):
    return 100.0 * c[key] / c["n"] if c["n"] else 0.0


def format_report(res, prohib, show_diffs=0) -> str:
    ov = res["overall"]
    L = []
    L.append("# 앱 L1 정직 측정 + A/B 패리티 (parity_check)")
    L.append("")
    L.append(f"- held-out n = {ov['n']}")
    L.append(f"- **앱 L1 (backend.nl.parser.parse) exact = {_pct(ov,'L1'):.1f}% "
             f"({ov['L1']}/{ov['n']})**")
    L.append(f"- 오버레이 L1A (parse_patched, 목표 74.2%) exact = {_pct(ov,'L1A'):.1f}% "
             f"({ov['L1A']}/{ov['n']})")
    L.append(f"- 갭(오버레이 − 앱) = {_pct(ov,'L1A') - _pct(ov,'L1'):+.1f}%p")
    L.append("")
    L.append("## 금지문 안전 게이트 (special:prohibition)")
    L.append(f"- 전수 {prohib['total']}문장 · **forbidden 액션 방출(misfire) "
             f"= {len(prohib['misfires'])}/{prohib['total']}** · ok=True = {len(prohib['ok_true'])}")
    for mf in prohib["misfires"]:
        L.append(f"  - ⚠️ {mf['id']}: {mf['sentence']}  → {mf['forbidden_hit']}")
    L.append("")
    for ax, mapping in res["by_cat"].items():
        L.append(f"## 카테고리별 ({ax})")
        L.append("")
        L.append("| 그룹 | 앱 L1 | 오버레이 L1A | 갭 | n |")
        L.append("|---|---:|---:|---:|---:|")
        for key in sorted(mapping):
            c = mapping[key]
            L.append(f"| {key} | {_pct(c,'L1'):.1f}% ({c['L1']}/{c['n']}) "
                     f"| {_pct(c,'L1A'):.1f}% ({c['L1A']}/{c['n']}) "
                     f"| {_pct(c,'L1A')-_pct(c,'L1'):+.1f}%p | {c['n']} |")
        L.append("")
    pd = res["parity_diffs"]
    L.append(f"## 패리티 diff (앱 L1 ≠ 오버레이 L1A): {len(pd)}문장")
    if res["app_regressions"]:
        L.append(f"- 앱 L1 exact 인데 오버레이 non-exact: {res['app_regressions']}")
    if show_diffs:
        for d in pd[:show_diffs]:
            L.append(f"  - [{d['app_L1']:5} vs {d['overlay_L1A']:5}] {d['id']}: {d['sentence']}")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="앱 L1 정직 측정 + A/B 패리티 (S0/S1) · L2 순리프트(S9)")
    ap.add_argument("--show-diffs", type=int, default=0, help="패리티 diff 상위 N개 출력.")
    ap.add_argument("--l2", action="store_true",
                    help="앱 L1 vs 앱 L1+L2 정직 측정(순리프트·l2_regressions·금지문 게이트).")
    ap.add_argument("--no-shadow", action="store_true",
                    help="--l2 에서 shadow-try 창을 끄고 측정(롤백 비교용).")
    ap.add_argument("--library", default=None, help="측정에 쓸 pattern_library 경로(기본 앱 동봉).")
    ap.add_argument("--show", type=int, default=0, help="--l2 이득 상위 N개 출력.")
    ap.add_argument("--out", default=os.path.join(_OUT, "parity_report.md"))
    args = ap.parse_args()

    corpus, inventory, gz_base, gz_overlay, settings = load_corpus()
    os.makedirs(_OUT, exist_ok=True)

    if args.l2:
        matcher, lib_n = build_matcher(gz_base, inventory, args.library)
        enable_shadow = False if args.no_shadow else None
        res = measure_l2(corpus, gz_base, inventory, settings, matcher,
                         enable_shadow=enable_shadow)
        prohib_gate = scan_prohibitions_gate(gz_base, inventory, settings,
                                             load_prohibitions(), matcher)
        report = format_l2_report(res, prohib_gate, lib_n, show=args.show)
        out = args.out if args.out != os.path.join(_OUT, "parity_report.md") \
            else os.path.join(_OUT, "parity_l2_report.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(report)
        c = res["counts"]
        print(f"앱 L1 = {_pct(c,'L1'):.1f}% ({c['L1']}/{c['n']})  |  "
              f"앱 L1+L2 = {_pct(c,'L1L2'):.1f}% ({c['L1L2']}/{c['n']})  |  "
              f"순리프트 = {_pct(c,'L1L2')-_pct(c,'L1'):+.1f}%p  |  "
              f"regressions = {len(res['regressions'])}  |  "
              f"gains = {len(res['gains'])}  |  "
              f"금지문 misfire(gate) = {len(prohib_gate['misfires'])}/{prohib_gate['total']}")
        print(f"report → {out}")
        return

    res = measure(corpus, gz_base, gz_overlay, settings)
    prohib = scan_prohibitions(gz_base, settings, load_prohibitions())
    report = format_report(res, prohib, show_diffs=args.show_diffs)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)

    ov = res["overall"]
    print(f"앱 L1 exact = {_pct(ov,'L1'):.1f}% ({ov['L1']}/{ov['n']})  |  "
          f"오버레이 L1A = {_pct(ov,'L1A'):.1f}% ({ov['L1A']}/{ov['n']})  |  "
          f"금지문 misfire = {len(prohib['misfires'])}/{prohib['total']}")
    print(f"report → {args.out}")


if __name__ == "__main__":
    main()
