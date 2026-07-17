"""SPEC-ACCURACY §2.6 — 3단(+overlay) 누적 정확도 측정 하네스 (앱 미수정).

held-out 실사용 문장(heldout.yaml + paraphrases.yaml 의 명시 gold hard case)에 대해
**L1 → L1A → +L2 → +L3** 를 누적 적용하며 각 단계의 exact% 를 측정한다.

  L1  = 앱 parser.parse (기준선)
  L1A = parser_overlay.parse_patched (A그룹 결정적 규칙 오버레이 + 모드 동의어 gazetteer)
  +L2 = L1A 가 needs_help(conf<0.6 or not ok or unmatched) 면 pattern_match.TemplateMatcher
  +L3 = L2 도 못 흡수(매처 None)면 cli_normalize 로 표준 문형 재작성 → parse_patched 재파싱

각 단계 후보 model 을 structural_match.compare 로 exact 판정한다. 보고(out/hybrid_report.md):
  - 단계별 누적 exact%(전체·dev·test, source×area×difficulty)
  - 순리프트(이득−회귀: L1 exact 였는데 이후 깨진 수 별도 집계)
  - solved_by ∈ {rule, ruleA, template, cli, none} 귀속
  - L2 구조상한(매처 후보가 exact 인 비율), CLI 캐시 적중/실호출
  - **test split +L3 exact% 가 정직 70% 지표** — 리포트 상단 명시.

dev/test 는 hash(sentence)%2(md5, 결정적) 분할. τ 는 dev 동결(test 재적합 금지: 매처
tau=0.72 기본값 그대로 사용, test 로 재튜닝하지 않는다).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

import yaml

# --- 앱 import 루트 배선 (읽기 전용) --------------------------------------
_APP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from backend.nl.parser import parse  # noqa: E402

try:  # 패키지/스크립트 양쪽 실행 지원
    from . import augment as _aug
    from . import cli_normalize as _cli
    from . import evaluate as _ev
    from . import generate as _gen
    from . import parser_overlay as _ov
    from . import pattern_match as _pm
    from . import structural_match as _sm
except ImportError:  # pragma: no cover
    import augment as _aug
    import cli_normalize as _cli
    import evaluate as _ev
    import generate as _gen
    import parser_overlay as _ov
    import pattern_match as _pm
    import structural_match as _sm

_HERE = os.path.dirname(__file__)
_OUT = os.path.join(_HERE, "out")


# ---------------------------------------------------------------------------
# 로딩
# ---------------------------------------------------------------------------
def _difficulty_of(item: dict) -> str:
    for t in item.get("tags", []):
        if isinstance(t, str) and t.startswith("diff:"):
            return t.split(":", 1)[1]
    return "?"


def _template_ids(templates: list) -> set:
    return {t.get("id") for t in templates}


def load_heldout(templates, inventory, mode_names) -> list:
    """heldout.yaml(전체) + paraphrases.yaml 의 명시 gold(hard case)만 held-out 으로 로드.

    paraphrases 의 gold_ref 항목은 라이브러리(templates) 유래라 held-out 이 아니므로 제외한다.
    각 item 에 dataset(heldout|para_hard) · difficulty 부여.
    """
    tpl_ids = _template_ids(templates)
    items = []

    ho = _aug.load_paraphrases(os.path.join(_HERE, "heldout.yaml"),
                               templates, inventory, mode_names)
    for it in ho:
        it["dataset"] = "heldout"
        it["difficulty"] = _difficulty_of(it)
        items.append(it)

    para = _aug.load_paraphrases(os.path.join(_HERE, "paraphrases.yaml"),
                                 templates, inventory, mode_names)
    for it in para:
        # 명시 gold hard case = template_id 가 실제 템플릿 id 가 아님(entry.id 사용).
        if it.get("template_id") in tpl_ids:
            continue
        it["dataset"] = "para_hard"
        it["difficulty"] = _difficulty_of(it)
        items.append(it)

    # gold_invalid 는 측정에서 제외(자체검증 gold 오류 격리)
    return [it for it in items if not it.get("gold_invalid")]


def load_pattern_library(path=None) -> tuple:
    """L2 매처용 패턴 라이브러리 로드.

    overlay 기준(pattern_library_overlay.yaml)을 **우선** 로드한다. 이 파일은
    build_overlay_library.py 가 parse_patched(A그룹 오버레이)로 코퍼스를 재평가해 만든
    커버 상태라, A/B그룹으로 covered/partial 이 된 골격이 매처 인덱스에 포함된다. 없으면
    기존 미패치 pattern_library.yaml 로 폴백.

    반환: (library, source_name)
    """
    if path:
        candidates = [path]
    else:
        candidates = [os.path.join(_OUT, "pattern_library_overlay.yaml"),
                      os.path.join(_OUT, "pattern_library.yaml")]
    for p in candidates:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return (yaml.safe_load(f) or []), os.path.basename(p)
    return [], "(없음)"


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def _split_bucket(sentence: str) -> str:
    """md5 기반 결정적 dev/test 분할(%2)."""
    h = hashlib.md5(sentence.encode("utf-8")).hexdigest()
    return "dev" if int(h[:8], 16) % 2 == 0 else "test"


def _needs_help(res: dict, threshold: float = 0.6) -> bool:
    if not res:
        return True
    if not res.get("ok"):
        return True
    if res.get("unmatched"):
        return True
    conf = res.get("confidence")
    if conf is None or conf < threshold:
        return True
    return False


def _exact(gold: dict, model: dict) -> bool:
    try:
        return _sm.compare(gold, model)["verdict"] == "exact"
    except Exception:  # noqa: BLE001 — 방어적: 비교 실패는 non-exact
        return False


def _safe_parse(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 — 파서 예외는 빈 모델로(non-exact)
        return {"ok": False, "model": {}, "unmatched": [], "confidence": 0.0}


# ---------------------------------------------------------------------------
# 누적 집계 컨테이너
# ---------------------------------------------------------------------------
_STAGES = ("L1", "L1A", "L2", "L3")


def _new_counter():
    return {s: {"exact": 0, "n": 0} for s in _STAGES}


def _bump(counter, stage_exacts, present=True):
    for s in _STAGES:
        counter[s]["n"] += 1
        if stage_exacts[s]:
            counter[s]["exact"] += 1


def _pct(c) -> float:
    return 100.0 * c["exact"] / c["n"] if c["n"] else 0.0


# ---------------------------------------------------------------------------
# 메인 평가
# ---------------------------------------------------------------------------
def evaluate_hybrid(cli_budget: int = 0, tau: float = 0.72, verbose: bool = False):
    inventory, gz_base, settings = _ev.build_inventory()
    mode_names = list(settings.get("modes", {}).keys())
    gz_overlay = _ov.build_overlay_gazetteer(inventory, settings)

    templates = _gen.load_templates(os.path.join(_HERE, "templates.yaml"))
    slots = _gen.load_slots(os.path.join(_HERE, "slots.yaml"))
    for t in templates:
        t["_slots"] = slots

    corpus = load_heldout(templates, inventory, mode_names)

    library, library_src = load_pattern_library()
    matcher = _pm.TemplateMatcher(library, gz_overlay, inventory, tau=tau)
    fewshot_pool = _cli.build_fewshot_pool(library)

    overall = _new_counter()
    by_split = {"dev": _new_counter(), "test": _new_counter()}
    by_source = {}
    by_area = {}
    by_diff = {}
    solved_by = {"rule": 0, "ruleA": 0, "template": 0, "cli": 0, "none": 0}

    # 리프트/회귀
    gains = 0            # not e1 -> e_final
    regressions = 0      # e1 -> not e_final
    overlay_regressions = 0   # e1 -> not e1a
    # L2 구조상한 / 순리프트
    matcher_candidates = 0
    matcher_exact = 0
    matcher_suppressed = 0   # struct_replace 억제(L1A ok 보호)
    l2_gains = 0             # L1A non-exact → L2 exact (매처 채택 시)
    l2_regressions = 0       # L1A exact → L2 non-exact (매처 채택 시)
    # CLI 통계
    cli_cache_hits = 0
    cli_live_calls = 0
    cli_applied = 0      # cli 재작성이 적용된(changed) 수
    cli_helped = 0       # cli 로 exact 가 된 수

    rows = []

    for it in corpus:
        s = it["sentence"]
        gold = it["gold"]
        split = _split_bucket(s)

        # --- L1 ---
        r1 = _safe_parse(parse, s, gz_base, settings)
        m1 = r1.get("model", {})
        e1 = _exact(gold, m1)

        # --- L1A (overlay) ---
        r1a = _safe_parse(_ov.parse_patched, s, gz_overlay, settings)
        m1a = r1a.get("model", {})
        e1a = _exact(gold, m1a)

        need = _needs_help(r1a)

        # --- +L2 (template matcher, needs_help 시) ---
        # 정책(§2.6): 매처는 **L1A 가 저장 불가(not ok)** 인 진짜 미해결 문장만 흡수한다.
        #   L1A 가 ok(저장가능)면 — 저confidence 라도 — 규칙 파서 결과를 신뢰하고 매처(퍼지
        #   템플릿 매칭)로 덮어쓰지 않는다. 실측상 L1A ok 문장을 매처가 덮으면 회귀만 낸다
        #   ("거실 움직임 감지되면 텔레비전 켜": L1A exact 인데 매처 slot_fill 이 오매칭).
        #   오탐 억제는 매처 내부 3게이트(구조태그 부분집합 + 극성 기능어 멀티셋 + 마진)와
        #   이 ok 게이트로 이중화. slot_fill/struct_replace 정책은 매처가 결정(유지).
        used_template = False
        m_l2 = m1a
        if need:
            l1a_savable = bool(r1a.get("ok"))
            if l1a_savable:
                # 저장가능한 L1A 는 보호(매처 억제). needs_help 는 conf<0.6 로 뜬 것.
                matcher_suppressed += 1
            else:
                match = matcher.match(s)
                if match:
                    m_l2 = match.get("model", m1a)
                    used_template = True
                    matcher_candidates += 1
                    if _exact(gold, m_l2):
                        matcher_exact += 1
        e_l2 = _exact(gold, m_l2)
        # L2 순리프트(L1A 대비): 매처가 실제 채택됐을 때만 집계.
        if used_template:
            if not e1a and e_l2:
                l2_gains += 1
            elif e1a and not e_l2:
                l2_regressions += 1

        # --- +L3 (cli 정규화, L2 도 못 흡수 시: needs_help & 매처 None) ---
        used_cli = False
        m_l3 = m_l2
        l3_fires = need and not used_template
        if l3_fires:
            cached = _cli.cache_has(s)
            cli_res = None
            if cached:
                cli_res = _cli.normalize(s, None, inventory, settings, allow_live=False)
                cli_cache_hits += 1
            elif cli_budget > 0:
                fs = _cli.select_fewshot(s, fewshot_pool, k=5)
                cli_res = _cli.normalize(s, fs, inventory, settings, allow_live=True)
                cli_live_calls += 1
                cli_budget -= 1
            if cli_res and cli_res.get("changed") and cli_res.get("normalized"):
                r3 = _safe_parse(_ov.parse_patched, cli_res["normalized"],
                                 gz_overlay, settings)
                m_l3 = r3.get("model", m_l2)
                used_cli = True
                cli_applied += 1
        e_l3 = _exact(gold, m_l3)
        if used_cli and e_l3 and not e_l2:
            cli_helped += 1

        stage_exacts = {"L1": e1, "L1A": e1a, "L2": e_l2, "L3": e_l3}

        # 집계
        _bump(overall, stage_exacts)
        _bump(by_split[split], stage_exacts)
        by_source.setdefault(it.get("dataset", "?"), _new_counter())
        _bump(by_source[it["dataset"]], stage_exacts)
        by_area.setdefault(it.get("area", "?"), _new_counter())
        _bump(by_area[it.get("area", "?")], stage_exacts)
        by_diff.setdefault(it.get("difficulty", "?"), _new_counter())
        _bump(by_diff[it.get("difficulty", "?")], stage_exacts)

        # 귀속
        if e1:
            solved_by["rule"] += 1
        elif e1a:
            solved_by["ruleA"] += 1
        elif e_l2 and used_template:
            solved_by["template"] += 1
        elif e_l3 and used_cli:
            solved_by["cli"] += 1
        else:
            solved_by["none"] += 1

        # 리프트/회귀
        if not e1 and e_l3:
            gains += 1
        if e1 and not e_l3:
            regressions += 1
        if e1 and not e1a:
            overlay_regressions += 1

        rows.append({"id": it.get("id"), "sentence": s, "split": split,
                     "dataset": it.get("dataset"), "area": it.get("area"),
                     "difficulty": it.get("difficulty"),
                     "e1": e1, "e1a": e1a, "e_l2": e_l2, "e_l3": e_l3,
                     "used_template": used_template, "used_cli": used_cli})
        if verbose:
            flag = "".join("1" if x else "0" for x in (e1, e1a, e_l2, e_l3))
            print(f"[{flag}] {split:4} {it.get('area',''):12} {s}")

    return {
        "n": len(corpus),
        "overall": overall,
        "by_split": by_split,
        "by_source": by_source,
        "by_area": by_area,
        "by_diff": by_diff,
        "solved_by": solved_by,
        "gains": gains, "regressions": regressions,
        "net_lift": gains - regressions,
        "overlay_regressions": overlay_regressions,
        "matcher_candidates": matcher_candidates,
        "matcher_exact": matcher_exact,
        "matcher_suppressed": matcher_suppressed,
        "l2_gains": l2_gains, "l2_regressions": l2_regressions,
        "l2_net_lift": l2_gains - l2_regressions,
        "library_src": library_src,
        "cli_cache_hits": cli_cache_hits,
        "cli_live_calls": cli_live_calls,
        "cli_applied": cli_applied, "cli_helped": cli_helped,
        "tau": tau,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------
def _stage_row(label, counter) -> str:
    cells = " | ".join(f"{_pct(counter[s]):.1f}% ({counter[s]['exact']}/{counter[s]['n']})"
                       for s in _STAGES)
    return f"| {label} | {cells} |"


def _group_table(title, mapping) -> str:
    lines = [f"### {title}", "",
             "| 그룹 | L1 | L1A | +L2 | +L3 |",
             "|---|---|---|---|---|"]
    for key in sorted(mapping):
        lines.append(_stage_row(key, mapping[key]))
    lines.append("")
    return "\n".join(lines)


def write_report(res: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    test = res["by_split"]["test"]
    dev = res["by_split"]["dev"]
    ov = res["overall"]
    test_l3 = _pct(test["L3"])
    reached = "예 ✅" if test_l3 >= 70.0 else "아니오 ❌"

    L = []
    L.append("# 하이브리드 누적 정확도 리포트 (held-out, 앱 미수정)")
    L.append("")
    L.append("## 정직 정확도 L1→L1A→+L2→+L3")
    L.append("")
    L.append(f"- **정직 지표 = test split +L3 exact% = {test_l3:.1f}%** "
             f"({test['L3']['exact']}/{test['L3']['n']})")
    L.append(f"- **70% 도달 여부: {reached}**")
    L.append(f"- 누적 단계별(test): "
             f"L1 {_pct(test['L1']):.1f}% → L1A {_pct(test['L1A']):.1f}% → "
             f"+L2 {_pct(test['L2']):.1f}% → +L3 {_pct(test['L3']):.1f}%")
    L.append(f"- 참고(전체 held-out): "
             f"L1 {_pct(ov['L1']):.1f}% → L1A {_pct(ov['L1A']):.1f}% → "
             f"+L2 {_pct(ov['L2']):.1f}% → +L3 {_pct(ov['L3']):.1f}% (n={res['n']})")
    L.append("")
    L.append("> 정직성: heldout.yaml + paraphrases 명시 gold(hard case)는 라이브러리 구축에 "
             "쓰지 않은 held-out 이다. τ={:.2f} 는 dev 에서 동결하고 test 로 재튜닝하지 않았다."
             .format(res["tau"]))
    L.append("")

    L.append("## 단계별 누적 exact% (dev / test / 전체)")
    L.append("")
    L.append("| split | L1 | L1A | +L2 | +L3 |")
    L.append("|---|---|---|---|---|")
    L.append(_stage_row("dev", dev))
    L.append(_stage_row("test", test))
    L.append(_stage_row("전체", ov))
    L.append("")

    L.append("## 순리프트 / 회귀 (전체 held-out, L1 기준 최종 +L3)")
    L.append("")
    L.append(f"- 이득(gain: L1 non-exact → +L3 exact): **{res['gains']}**")
    L.append(f"- 회귀(regression: L1 exact → +L3 non-exact): **{res['regressions']}**")
    L.append(f"- **순리프트(이득−회귀): {res['net_lift']}**")
    L.append(f"- 그중 overlay(L1→L1A) 단독 회귀: {res['overlay_regressions']}")
    L.append("")

    L.append("## solved_by 귀속 (최초로 exact 를 만든 레이어)")
    L.append("")
    sb = res["solved_by"]
    tot = sum(sb.values()) or 1
    L.append("| 레이어 | 수 | 비율 |")
    L.append("|---|---:|---:|")
    for k in ("rule", "ruleA", "template", "cli", "none"):
        L.append(f"| {k} | {sb[k]} | {100.0*sb[k]/tot:.1f}% |")
    L.append("")

    L.append("## L2 템플릿 매처 구조상한 / 순리프트")
    L.append("")
    mc, me = res["matcher_candidates"], res["matcher_exact"]
    ceil = (100.0 * me / mc) if mc else 0.0
    L.append(f"- 매처 라이브러리 소스: `{res.get('library_src', '?')}`")
    L.append(f"- needs_help 문장 중 매처가 후보를 낸(채택) 수: {mc} "
             f"(L1A ok 보호로 매처 미시도 {res.get('matcher_suppressed', 0)})")
    L.append(f"- 채택 후보가 구조적으로 exact: {me} → 매처 상한 {ceil:.1f}%")
    L.append(f"- **L2 순리프트(L1A→L2, 매처 채택분): "
             f"이득 {res.get('l2_gains', 0)} − 회귀 {res.get('l2_regressions', 0)} "
             f"= {res.get('l2_net_lift', 0)}**")
    L.append("")

    L.append("## L3 CLI 정규화 통계")
    L.append("")
    L.append(f"- 캐시 적중: {res['cli_cache_hits']} · 실호출(live): {res['cli_live_calls']}")
    L.append(f"- 재작성 적용(changed): {res['cli_applied']} · 그중 exact 로 전환: {res['cli_helped']}")
    L.append("> CLI 는 캐시 우선(결정적 재현). 캐시 미스는 --cli-budget 만큼만 실호출한다. "
             "예산 밖 L3 문장은 이번 실행에서 미해결로 집계된다(재실행 시 캐시로 채워짐).")
    L.append("")

    L.append(_group_table("소스별 (heldout / para_hard)", res["by_source"]))
    L.append(_group_table("영역별", res["by_area"]))
    L.append(_group_table("난이도별", res["by_diff"]))

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="하이브리드 누적 정확도 측정 (L1→L1A→+L2→+L3)")
    ap.add_argument("--cli-budget", type=int, default=0,
                    help="캐시 미스 시 실제 CLI 호출 최대 횟수(기본 0=캐시만).")
    ap.add_argument("--tau", type=float, default=0.72, help="템플릿 매처 유사도 임계(dev 동결).")
    ap.add_argument("--verbose", action="store_true", help="문장별 단계 플래그 출력.")
    ap.add_argument("--out", default=os.path.join(_OUT, "hybrid_report.md"))
    args = ap.parse_args()

    res = evaluate_hybrid(cli_budget=args.cli_budget, tau=args.tau,
                          verbose=args.verbose)
    write_report(res, args.out)

    test = res["by_split"]["test"]
    print(f"held-out n={res['n']}  "
          f"test +L3 exact = {_pct(test['L3']):.1f}%  "
          f"(L1 {_pct(test['L1']):.1f}% → L1A {_pct(test['L1A']):.1f}% → "
          f"+L2 {_pct(test['L2']):.1f}% → +L3 {_pct(test['L3']):.1f}%)")
    print(f"net_lift={res['net_lift']}  solved_by={res['solved_by']}")
    print(f"report → {args.out}")


if __name__ == "__main__":
    main()
