"""APP-PORT-PLAN §4.1 (S9) — 이식 완료 앱 파서 기준 pattern_library 재생성(원커맨드).

문제(§4 ①): 낡은 pattern_library 는 옛 파서 기준 covered 라 현재 파서 세계와 어긋난다.
→ **현재 앱 파서**(backend.nl.parser.parse)로 templates×slots + paraphrase(라벨보존) 코퍼스를
평가해 covered/partial/gap 을 재산정하고, covered+partial 골격만 gold·grammar 예시와 함께
`automation_maker/backend/nl/pattern_library.yaml` 로 내보낸다.

정직성 가드(§4.1 · 게이트4):
  - 예시는 **grammar(templates×slots) 생성문 중 exact** 만 사용 → 구성상 held-out 과 disjoint.
  - 그래도 **held-out 전체 문장 md5 블랙리스트**로 교차검사해, 라이브러리(예시)에 held-out
    문장이 하나라도 있으면 제거하고 개수를 로그로 남긴다(오염 0 을 증명).
  - covered 판정 채점기는 정직 채점기(structural_match)를 그대로 쓴다.
  - now_fn 고정(_LABEL_NOW)으로 calendar(interval_anchor) 축까지 결정적.

실행:  python tools/corpus/export_app_library.py [--limit 40] [--seed 0] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "automation_maker"))
for p in (_HERE, _APP_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import build_overlay_library as _bol  # noqa: E402 — build_corpus 재사용
import evaluate as _ev  # noqa: E402
import evaluate_hybrid as _eh  # noqa: E402
import generate as _gen  # noqa: E402
import mine as _mine  # noqa: E402
import structural_match as _sm  # noqa: E402

from backend.nl.parser import parse as _parse  # noqa: E402

TEMPLATES = os.path.join(_HERE, "templates.yaml")
SLOTS = os.path.join(_HERE, "slots.yaml")
OUT_APP = os.path.join(_APP_ROOT, "backend", "nl", "pattern_library.yaml")

# parity_check 와 동일한 라벨주 now(calendar 결정성). held-out 오염 아님(gold 미유입).
_LABEL_NOW = datetime(2026, 7, 17, 12, 0, 0)


def _md5(s: str) -> str:
    return hashlib.md5((s or "").strip().encode("utf-8")).hexdigest()


def _heldout_hashes(templates, inventory, mode_names) -> set:
    """held-out(heldout.yaml + para_hard) 전체 문장의 md5 블랙리스트(오염 검사용)."""
    corpus = _eh.load_heldout(templates, inventory, mode_names)
    return {_md5(it["sentence"]) for it in corpus}


def evaluate_app(corpus, gz, settings) -> dict:
    """corpus 를 현재 앱 parse(now 고정)로 평가 → by_template 판정 + exact 예시."""
    by_template = {}
    examples = {}
    for item in corpus:
        if item.get("gold_invalid"):
            continue
        try:
            actual = _parse(item["sentence"], gz, settings, now_fn=lambda: _LABEL_NOW)
        except Exception:  # noqa: BLE001
            actual = {"model": {}}
        verdict = _sm.compare(item["gold"], actual.get("model", {}))["verdict"]
        tid = item.get("template_id", "?")
        by_template.setdefault(tid, {"exact": 0, "partial": 0, "fail": 0})
        by_template[tid][verdict] = by_template[tid].get(verdict, 0) + 1
        # 예시: grammar 생성문 중 exact 만(구성상 held-out 과 disjoint).
        if verdict == "exact" and item.get("source") == "grammar":
            examples.setdefault(tid, [])
            if len(examples[tid]) < 3:
                examples[tid].append(item["sentence"])
    return {"by_template": by_template, "examples": examples}


def build_library(templates, evaled, blacklist):
    """covered/partial 템플릿만 gold·예시와 함께 라이브러리 항목으로. 오염 예시는 제거."""
    by_template = evaled["by_template"]
    examples = evaled["examples"]
    out = []
    dist = {"covered": 0, "partial": 0, "gap": 0, "unknown": 0}
    stripped = 0
    for tpl in templates:
        tid = tpl.get("id")
        status = _mine._cover_status(by_template.get(tid, {}))
        dist[status] = dist.get(status, 0) + 1
        if status not in ("covered", "partial"):
            continue
        exs = []
        for ex in examples.get(tid, []):
            if _md5(ex) in blacklist:  # held-out 오염 방지(방어적 제거)
                stripped += 1
                continue
            exs.append(ex)
        out.append({
            "id": tid,
            "area": tpl.get("area", ""),
            "template": tpl.get("template", ""),
            "slots": list(tpl.get("slots", [])),
            "tags": list(tpl.get("tags", [])),
            "status": status,
            "gold": tpl.get("gold", {}),
            "examples": exs,
        })
    return out, dist, stripped


def contamination_check(library, blacklist) -> list:
    """라이브러리(예시·템플릿)에 held-out 문장이 남아있는지 최종 검사 → 오염 문장 목록."""
    bad = []
    for tpl in library:
        for ex in tpl.get("examples", []) or []:
            if _md5(ex) in blacklist:
                bad.append((tpl.get("id"), ex))
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description="앱 파서 기준 pattern_library 재생성(S9 §4.1)")
    ap.add_argument("--limit", type=int, default=40, help="템플릿당 상한(균등)")
    ap.add_argument("--seed", type=int, default=0, help="셔플 시드")
    ap.add_argument("--dry-run", action="store_true", help="파일 쓰지 않고 통계만 출력")
    ap.add_argument("--out", default=OUT_APP)
    args = ap.parse_args(argv)

    inventory, gz_base, settings = _ev.build_inventory()
    mode_names = set(settings.get("modes", {}))
    templates = _gen.load_templates(TEMPLATES)
    slots = _gen.load_slots(SLOTS)

    corpus = _bol.build_corpus(templates, slots, inventory, gz_base, settings,
                               mode_names, args.seed, args.limit, no_augment=False)
    evaled = evaluate_app(corpus, gz_base, settings)

    blacklist = _heldout_hashes(templates, inventory, mode_names)
    library, dist, stripped = build_library(templates, evaled, blacklist)

    bad = contamination_check(library, blacklist)
    if bad:
        print(f"[오염 검사 실패] held-out 문장이 라이브러리에 {len(bad)}건 남음:")
        for tid, ex in bad[:10]:
            print(f"  - {tid}: {ex}")
        raise SystemExit(2)

    print(f"[코퍼스] {len(corpus)}문장(grammar+paraphrase) · held-out 블랙리스트 {len(blacklist)}해시")
    print(f"[상태분포(앱 파서)] {dist} · 편입(covered+partial) {len(library)}개")
    print(f"[오염 검사] 제거된 예시 {stripped}건 · 최종 라이브러리 오염 0건 ✓")
    print(f"  covered: {[t['id'] for t in library if t['status']=='covered']}")
    print(f"  partial: {[t['id'] for t in library if t['status']=='partial']}")

    if args.dry_run:
        print("[dry-run] 파일 미기록")
        return

    header = (
        "# pattern_library.yaml — L2 템플릿 매처 데이터 자산(앱 동봉, read-only).\n"
        "#\n"
        "# tools/corpus/templates.yaml 를 **현재 앱 파서**(backend.nl.parser.parse)로 평가해\n"
        "# covered/partial 골격만 증류한 것이다(APP-PORT-PLAN §4.1 / S9). 각 항목: template(문형)\n"
        "# + gold(정답 골격) + slots + examples(grammar exact 예시). held-out 오염 0(블랙리스트 검사).\n"
        "# 재생성: python tools/corpus/export_app_library.py\n"
        f"# 상태분포(앱 파서): {dist} · 편입(covered+partial) {len(library)}개.\n"
    )
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(library, f, allow_unicode=True, sort_keys=False)
    print(f"[완료] → {args.out}")


if __name__ == "__main__":
    main()
