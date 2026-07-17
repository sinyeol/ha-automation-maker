"""SPEC-ACCURACY §2.6 — overlay 기준 pattern_library 재생성 (앱 미수정).

문제: 기존 `out/pattern_library.yaml` 는 run.py 가 **미패치 파서**(backend.nl.parser.parse)로
코퍼스를 평가해 만든 커버 상태다. A그룹 오버레이(parser_overlay.parse_patched)를 켜면 exact 로
전환되는 골격(예: mode_trig_on_light)이 미패치 기준에선 gap 으로 남아 L2 매처 인덱스에서 제외된다
(§2.6 게이트2: gap 템플릿 제외). 결과적으로 매처 기여 ≈0.

이 도구는 run.py 와 **동일한 코퍼스**(generate + paraphrase augment)를 만들되, 평가만
`parser_overlay.parse_patched`(+overlay gazetteer)로 수행해 covered/partial/gap 을 재산정하고,
`mine.write_pattern_library` 로 `out/pattern_library_overlay.yaml` 를 생성한다. 이래야 오버레이로
covered 가 된 골격이 매처 인덱스에 포함된다.

features 담당이 templates.yaml / parser_overlay 를 확장하면, 이 도구를 재실행하는 것만으로
overlay 라이브러리가 자동 갱신된다(재생성).

결정적: seed 고정, 랜덤/시각 미사용. 실행:
  python tools/corpus/build_overlay_library.py [--no-augment] [--limit 40] [--seed 0]
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
import parser_overlay as _ov  # noqa: E402
import structural_match as _sm  # noqa: E402

OUT_DIR = os.path.join(_HERE, "out")
TEMPLATES = os.path.join(_HERE, "templates.yaml")
SLOTS = os.path.join(_HERE, "slots.yaml")
PARAPHRASES = os.path.join(_HERE, "paraphrases.yaml")


# ---------------------------------------------------------------------------
# 코퍼스 구성: run.py.main 과 동일(generate + label-guard augment).
# ---------------------------------------------------------------------------
def build_corpus(templates, slots, inventory, gz, settings, mode_names,
                 seed: int, limit: int, no_augment: bool) -> list:
    corpus = _gen.generate(templates, slots, inventory, seed=seed,
                           limit_per_template=limit, mode_names=mode_names)
    if not no_augment and os.path.exists(PARAPHRASES):
        for tpl in templates:
            tpl["_slots"] = slots
        paras = _aug.load_paraphrases(PARAPHRASES, templates, inventory, mode_names)
        paras = _aug.dedup(paras)
        paras = _aug.dedup_against(paras, [it["sentence"] for it in corpus])
        kept = []
        for it in paras:
            ok, _reason = _aug.label_preservation_ok(it, gz)
            if ok:
                kept.append(it)
        corpus.extend(kept)
    return corpus


# ---------------------------------------------------------------------------
# 평가: evaluate.py.evaluate 의 미러 — parse 대신 parse_patched(+overlay gz).
#   반환 형식은 evaluate.evaluate 와 동일(by_template / items) 이라 mine 재사용 가능.
# ---------------------------------------------------------------------------
def evaluate_overlay(corpus, gz_overlay, settings) -> dict:
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
            actual = _ov.parse_patched(item["sentence"], gz_overlay, settings)
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
                          "exception": exc})

    evaluated = sum(by_verdict.values())
    return {"total": len(corpus), "evaluated": evaluated,
            "gold_invalid": gold_invalid, "by_verdict": by_verdict,
            "by_area": by_area, "by_template": by_template,
            "by_source": by_source, "items": items_out}


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="overlay 기준 pattern_library 재생성 (parse_patched 평가)")
    ap.add_argument("--no-augment", action="store_true", help="문법 코퍼스만 사용")
    ap.add_argument("--limit", type=int, default=40, help="템플릿당 상한(균등)")
    ap.add_argument("--seed", type=int, default=0, help="셔플 시드")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "pattern_library_overlay.yaml"))
    args = ap.parse_args(argv)

    inventory, gz_base, settings = _ev.build_inventory()
    gz_overlay = _ov.build_overlay_gazetteer(inventory, settings)
    mode_names = set(settings.get("modes", {}))

    templates = _gen.load_templates(TEMPLATES)
    slots = _gen.load_slots(SLOTS)

    corpus = build_corpus(templates, slots, inventory, gz_overlay, settings,
                          mode_names, args.seed, args.limit, args.no_augment)

    result = evaluate_overlay(corpus, gz_overlay, settings)
    _mine.write_pattern_library(templates, result, args.out)

    # 상태 분포 요약(미패치 대비 covered 증가 확인용).
    import yaml
    with open(args.out, encoding="utf-8") as f:
        lib = yaml.safe_load(f) or []
    dist = {"covered": 0, "partial": 0, "gap": 0, "unknown": 0}
    for t in lib:
        dist[t.get("status", "unknown")] = dist.get(t.get("status", "unknown"), 0) + 1

    # 미패치 라이브러리와 비교(있으면).
    base_path = os.path.join(OUT_DIR, "pattern_library.yaml")
    base_dist = {"covered": 0, "partial": 0, "gap": 0, "unknown": 0}
    base_status = {}
    if os.path.exists(base_path):
        with open(base_path, encoding="utf-8") as f:
            base = yaml.safe_load(f) or []
        for t in base:
            base_dist[t.get("status", "unknown")] = \
                base_dist.get(t.get("status", "unknown"), 0) + 1
            base_status[t.get("id")] = t.get("status")

    bv = result["by_verdict"]
    print(f"[완료] {len(corpus)}문장 · exact {bv.get('exact',0)} "
          f"partial {bv.get('partial',0)} fail {bv.get('fail',0)}")
    print(f"[overlay 라이브러리] {args.out}")
    print(f"  상태분포(overlay): {dist}")
    print(f"  상태분포(미패치):  {base_dist}")
    # 미패치 gap/partial → overlay covered 로 승격된 골격(매처 인덱스 신규 편입).
    promoted = [t.get("id") for t in lib
                if t.get("status") == "covered"
                and base_status.get(t.get("id")) not in ("covered", None)]
    if promoted:
        print(f"  covered 로 승격(매처 신규 편입): {len(promoted)}개 → {promoted}")
    to_partial = [t.get("id") for t in lib
                  if t.get("status") == "partial"
                  and base_status.get(t.get("id")) == "gap"]
    if to_partial:
        print(f"  gap→partial 승격: {len(to_partial)}개 → {to_partial}")


if __name__ == "__main__":
    main()
