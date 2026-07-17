"""§3.2 / §7.5 — 패러프레이즈(자연 변형) 로드·중복 제거·라벨보존 가드.

paraphrases.yaml 은 LLM/에이전트(Fable)가 시드 문장을 자연스러운 한국어로 바꿔 채운다.
augment 는:
  - gold_ref 로 템플릿 gold 를 상속하고, 지정(또는 기본=첫 필러) 슬롯 바인딩으로 구체화.
  - dedup: 공백정규화 exact + 문자 3-gram Jaccard ≥ 0.9 근접 중복 제거.
  - 라벨보존 가드(파서 무관): (a) 슬롯 앵커링 — 각 슬롯 이형태 중 하나가 문장에 존재,
    (b) gazetteer 해석성 — gold 엔티티가 인벤토리에서 해석 가능.
  * 파서 재파싱은 필터가 아니라 커버리지 신호로만(§7.5). 여기선 검사하지 않는다.

paraphrases.yaml 스키마:
  - gold_ref: <template id>
    bind: {slot: surface}          # 선택: 어떤 필러로 gold 를 구체화할지
    variants:
      - "자연 문장 …"
      - {text: "자연 문장 …", bind: {slot: surface}}   # 변형별 바인딩 override
"""
from __future__ import annotations

import os

import yaml

try:  # 패키지/스크립트 양쪽 실행 지원
    from . import generate as _gen
except ImportError:  # pragma: no cover
    import generate as _gen


# ---------------------------------------------------------------------------
# 로드 + gold 상속/구체화
# ---------------------------------------------------------------------------
def _pick_combo(tpl: dict, slots: dict, bind: dict) -> dict:
    """슬롯 바인딩 → combo(슬롯명→필러). bind 없으면 첫 필러."""
    combo = {}
    bind = bind or {}
    for slot in tpl.get("slots", []):
        fillers = slots.get(slot) or []
        if not fillers:
            continue
        chosen = fillers[0]
        want = bind.get(slot)
        if want is not None:
            for f in fillers:
                if f.get("surface") == want or f.get("value") == want \
                        or f.get("entity") == want:
                    chosen = f
                    break
        combo[slot] = chosen
    return combo


def load_paraphrases(path, templates, inventory=None, mode_names=None) -> list[dict]:
    """paraphrases.yaml → CorpusItem(source="paraphrase") 리스트. gold 상속·구체화·자가검증."""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    tpl_by_id = {t.get("id"): t for t in templates}
    inventory = inventory or {"areas": [], "entities": []}
    mode_names = set(mode_names or [])

    out = []
    for gi, entry in enumerate(data):
        if not isinstance(entry, dict):
            continue
        from backend.engine.rule_model import validate_rule_model
        ref = entry.get("gold_ref")
        tpl = tpl_by_id.get(ref) if ref else None
        base_bind = entry.get("bind") or {}
        # 명시 gold형(hard cases): gold_ref 없이 entry.gold 를 직접 정답 라벨로 사용한다.
        # 손으로 붙인 '사용자 의도' gold라 슬롯 앵커가 없고(가드는 엔티티 해석성으로 검사),
        # 이 항목들이 정직한 커버리지·갭의 핵심 신호다.
        explicit_gold = entry.get("gold") if tpl is None else None
        if tpl is None and explicit_gold is None:
            continue
        for vi, var in enumerate(entry.get("variants", []) or []):
            if isinstance(var, dict):
                text = var.get("text", "")
                bind = dict(base_bind, **(var.get("bind") or {}))
            else:
                text = str(var)
                bind = base_bind
            if not text.strip():
                continue
            if tpl is not None:
                # 슬롯 사전은 templates 에 첨부된 _slots 를 쓴다(run.py 가 주입).
                slots = tpl.get("_slots", {})
                combo = _pick_combo(tpl, slots, bind)
                gold = _gen.concretize_gold(tpl.get("gold", {}), combo, inventory)
                anchors = _gen._anchors_for(tpl, slots, combo)
                area, tags, tid = tpl.get("area", ""), list(tpl.get("tags", [])), ref
            else:
                gold = _gen.concretize_gold(explicit_gold, {}, inventory)
                anchors = []   # 손라벨 — 앵커 없음
                area = entry.get("area", "")
                tags = list(entry.get("tags", []))
                tid = entry.get("id", f"hard{gi}")
            model = _gen._gold_to_model(text, gold)
            errs = validate_rule_model(model, inventory, mode_names)
            out.append({
                "id": f"{tid}~p{gi}.{vi}",
                "sentence": _gen.normalize_ws(text),
                "gold": model,
                "area": area,
                "template_id": tid,
                "source": "paraphrase",
                "tags": tags,
                "gold_invalid": bool(errs),
                "gold_errors": [e.get("message") if isinstance(e, dict) else str(e)
                                for e in errs],
                "anchors": anchors,
            })
    return out


# ---------------------------------------------------------------------------
# dedup: 공백정규화 exact + 3-gram Jaccard ≥ 0.9
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return _gen.normalize_ws(s or "").replace(" ", "")


def _char_ngrams(s: str, n: int = 3) -> set:
    s = _norm(s)
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedup(items: list[dict], threshold: float = 0.9) -> list[dict]:
    """exact(공백정규화) + 근접(3-gram Jaccard≥threshold) 중복 제거. 첫 항목 유지."""
    kept = []
    seen_exact = set()
    grams = []
    for it in items:
        key = _norm(it.get("sentence", ""))
        if key in seen_exact:
            continue
        g = _char_ngrams(it.get("sentence", ""))
        if any(_jaccard(g, gg) >= threshold for gg in grams):
            continue
        seen_exact.add(key)
        grams.append(g)
        kept.append(it)
    return kept


def dedup_against(items: list[dict], existing_sentences, threshold: float = 0.9):
    """기존(grammar) 문장 집합과 겹치는 패러프레이즈 제거."""
    ex_exact = {_norm(s) for s in existing_sentences}
    ex_grams = [_char_ngrams(s) for s in existing_sentences]
    out = []
    for it in items:
        key = _norm(it.get("sentence", ""))
        if key in ex_exact:
            continue
        g = _char_ngrams(it.get("sentence", ""))
        if any(_jaccard(g, gg) >= threshold for gg in ex_grams):
            continue
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# 라벨보존 가드 (파서 무관)
# ---------------------------------------------------------------------------
def _gold_entity_ids(model: dict) -> list[str]:
    ids = []
    subs = model.get("subrules")
    rules = subs if isinstance(subs, list) else [model]
    for sr in rules:
        for bucket in ("triggers", "conditions", "actions"):
            for node in sr.get(bucket, []) or []:
                eid = node.get("entity_id")
                if isinstance(eid, str):
                    ids.append(eid)
                tgt = node.get("target")
                if isinstance(tgt, dict):
                    t = tgt.get("entity_id")
                    if isinstance(t, str):
                        ids.append(t)
                    elif isinstance(t, list):
                        ids.extend(x for x in t if isinstance(x, str))
    return ids


def label_preservation_ok(item: dict, gazetteer) -> tuple:
    """(a) 슬롯 앵커링 + (b) gazetteer 해석성. 반환 (ok, reason)."""
    sentence = item.get("sentence", "")
    # (a) 슬롯 앵커링: 각 슬롯의 이형태 중 하나가 문장에 존재해야 함
    for iforms in item.get("anchors", []):
        if not any(f and f in sentence for f in iforms):
            return False, f"슬롯 앵커 부재: {iforms}"
    # (b) gazetteer 해석성: gold 엔티티가 인벤토리에서 해석 가능해야 함
    for eid in _gold_entity_ids(item.get("gold", {})):
        if gazetteer.entity(eid) is None:
            return False, f"gold 엔티티 미해석: {eid}"
    return True, ""
