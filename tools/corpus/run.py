"""§3.6 — 파이프라인 오케스트레이터.

generate → (paraphrases 있으면 augment) → evaluate → mine → out/report.md.
인자: --no-augment(문법만), --limit N(템플릿당 상한). 결정적(seed 고정).

실행: python tools/corpus/run.py [--no-augment] [--limit 40] [--seed 0]
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "automation_maker"))
for p in (_HERE, _APP_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import augment as _aug  # noqa: E402
import evaluate as _ev  # noqa: E402
import generate as _gen  # noqa: E402
import mine as _mine  # noqa: E402

OUT_DIR = os.path.join(_HERE, "out")
TEMPLATES = os.path.join(_HERE, "templates.yaml")
SLOTS = os.path.join(_HERE, "slots.yaml")
PARAPHRASES = os.path.join(_HERE, "paraphrases.yaml")


def _write_report(corpus, eval_result, clusters, no_augment, path):
    bv = eval_result["by_verdict"]
    tot = sum(bv.values()) or 1
    src = eval_result["by_source"]

    def pct(d):
        t = sum(d.get(k, 0) for k in ("exact", "partial", "fail")) or 1
        return f"{100.0 * d.get('exact', 0) / t:.1f}%"

    lines = ["# 패턴 라이브러리 종합 리포트", ""]
    lines.append("## 1. 코퍼스 규모")
    n_gram = sum(1 for it in corpus if it.get("source") == "grammar")
    n_para = sum(1 for it in corpus if it.get("source") == "paraphrase")
    lines.append(f"- 총 {len(corpus)}문장 (grammar {n_gram} · paraphrase {n_para})")
    areas = {}
    for it in corpus:
        areas[it.get("area", "?")] = areas.get(it.get("area", "?"), 0) + 1
    lines.append("- 영역별: " + ", ".join(f"{k} {v}" for k, v in sorted(areas.items())))
    lines.append(f"- gold_invalid(격리): {eval_result['gold_invalid']}\n")

    lines.append("## 2. 현재 파서 커버리지")
    lines.append(f"- exact {bv.get('exact',0)} · partial {bv.get('partial',0)} "
                 f"· fail {bv.get('fail',0)} · 전체 정확률 {100.0*bv.get('exact',0)/tot:.1f}%")
    lines.append("")
    lines.append("| 소스 | exact | partial | fail | 정확률 |")
    lines.append("|---|---:|---:|---:|---:|")
    for s in sorted(src):
        d = src[s]
        lines.append(f"| {s} | {d.get('exact',0)} | {d.get('partial',0)} "
                     f"| {d.get('fail',0)} | {pct(d)} |")
    lines.append("")
    lines.append("> §7.7 낙관편향: grammar 는 템플릿 동형이라 낙관적. "
                 "일반화 지표는 **paraphrase 정확률** 우선.\n")

    lines.append("## 3. 상위 갭 패턴 (빈도순)")
    if clusters:
        lines.append("| # | 빈도 | 영역 | 오류태그 | 추상 패턴 | 예시 |")
        lines.append("|---:|---:|---|---|---|---|")
        for i, c in enumerate(clusters[:15], 1):
            tags = ",".join(_mine._diff_tags(c["sample_diff"])) or "-"
            ex = (c["examples"][0] if c["examples"] else "").replace("|", "/")
            lines.append(f"| {i} | {c['count']} | {c['area']} | {tags} "
                         f"| `{c['pattern']}` | {ex} |")
    else:
        lines.append("- 갭 없음(모든 문장 exact) 또는 코퍼스 비어 있음.")
    lines.append("")

    lines.append("## 4. 하이브리드 권고")
    lines.append("- 규칙 우선(현행 parser.py) → 낮은 confidence/미해결 시 pattern_library "
                 "템플릿 매처 → 최후 LLM few-shot (docs/HYBRID-PARSER.md 참조).")
    lines.append(f"- 추가 후보 패턴은 `out/gap_library.yaml` 참조(총 {len(clusters)} 클러스터).")
    lines.append("- 커버 상태별 시드 템플릿은 `out/pattern_library.yaml`(하이브리드 데이터 자산).")
    if no_augment:
        lines.append("- (이번 실행은 --no-augment: paraphrase 미포함)")
    lines.append("")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(description="패턴 라이브러리 파이프라인")
    ap.add_argument("--no-augment", action="store_true", help="문법 코퍼스만 사용")
    ap.add_argument("--limit", type=int, default=40, help="템플릿당 상한(균등)")
    ap.add_argument("--seed", type=int, default=0, help="셔플 시드")
    args = ap.parse_args(argv)

    inventory, gz, settings = _ev.build_inventory()
    mode_names = set(settings.get("modes", {}))

    templates = _gen.load_templates(TEMPLATES)
    slots = _gen.load_slots(SLOTS)
    if not templates:
        print(f"[경고] 템플릿 없음({TEMPLATES}) — 빈 코퍼스로 진행(시드 담당 대기).",
              file=sys.stderr)

    corpus = _gen.generate(templates, slots, inventory, seed=args.seed,
                           limit_per_template=args.limit, mode_names=mode_names)

    # augment: paraphrases.yaml 있으면 편입(라벨보존 가드 + dedup)
    if not args.no_augment and os.path.exists(PARAPHRASES):
        for tpl in templates:  # augment 가 슬롯 사전을 참조하도록 첨부
            tpl["_slots"] = slots
        paras = _aug.load_paraphrases(PARAPHRASES, templates, inventory, mode_names)
        paras = _aug.dedup(paras)
        paras = _aug.dedup_against(paras, [it["sentence"] for it in corpus])
        kept = []
        for it in paras:
            ok, reason = _aug.label_preservation_ok(it, gz)
            it["label_guard"] = {"ok": ok, "reason": reason}
            if ok:
                kept.append(it)
        corpus.extend(kept)
        print(f"[augment] paraphrase {len(kept)}/{len(paras)} 편입(가드 통과).",
              file=sys.stderr)

    result = _ev.evaluate(corpus, gz, settings, inventory)
    _ev.write_results_jsonl(result, os.path.join(OUT_DIR, "results.jsonl"))
    _ev.write_coverage_report(result, os.path.join(OUT_DIR, "coverage_report.md"))

    clusters = _mine.mine_gaps(result, gz)
    _mine.write_gap_library(clusters, os.path.join(OUT_DIR, "gap_library.yaml"))
    _mine.write_pattern_library(templates, result,
                                os.path.join(OUT_DIR, "pattern_library.yaml"))

    _write_report(corpus, result, clusters, args.no_augment,
                  os.path.join(OUT_DIR, "report.md"))
    bv = result["by_verdict"]
    print(f"[완료] {len(corpus)}문장 · exact {bv.get('exact',0)} "
          f"partial {bv.get('partial',0)} fail {bv.get('fail',0)} "
          f"→ {OUT_DIR}/report.md")


if __name__ == "__main__":
    main()
