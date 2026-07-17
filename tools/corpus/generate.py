"""§3.1 / §7.1·§7.2·§7.3·§7.4 — 문법×슬롯 조합으로 코퍼스(grammar) 생성.

- hassil 인라인 문법 전개: ``[옵션]`` / ``(대안|대안)`` / ``<expansion_rule>``.
- 슬롯 카테시안 조합을 만든 뒤 **템플릿당 상한(기본 40) 균등** + seed 고정 셔플로 표본 추출.
- 조사 토큰(``{가}``/``{을}`` 등)은 앞 슬롯 표면형 받침 기준으로 backend.nl.normalize 의
  josa_i_ga/josa_eul_reul 을 **재사용**해 계산.
- ``{scope.expand}`` 는 인벤토리로 도메인 전체 entity_id 를 확장.
- 각 CorpusItem 의 gold 를 validate_rule_model 로 자가검증 → 실패는 gold_invalid 로 격리(§7.4).

CorpusItem(dict):
    {"id","sentence","gold"(model dict),"area","template_id","source":"grammar",
     "tags":[...],"gold_invalid":bool,"gold_errors":[...],"anchors":[[iform..]..]}
"""
from __future__ import annotations

import os
import random
import re
import sys

import yaml

# --- 앱 import 루트 배선 (읽기 전용 import) --------------------------------
_APP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from backend.engine.rule_model import validate_rule_model  # noqa: E402
from backend.nl.normalize import (is_hangul_syllable, josa_eul_reul,  # noqa: E402
                                  josa_i_ga, normalize_ws)


# ---------------------------------------------------------------------------
# 로더
# ---------------------------------------------------------------------------
def load_templates(path) -> list[dict]:
    """templates.yaml 로드. 없으면 빈 리스트(파이프라인이 오류 없이 돌게)."""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    return [t for t in data if isinstance(t, dict) and t.get("template")]


def load_slots(path) -> dict:
    """slots.yaml 로드(슬롯명 → 필러 리스트). 없으면 빈 dict."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# hassil 인라인 문법 전개
#   sequence   = item*
#   item       = literal | '(' alts ')' | '[' alts ']' | '<' name '>'
#   alts       = sequence ('|' sequence)*
#   전개 결과 = 표면 문자열 리스트(대안=곱집합, 옵션=있음/없음 2분기, 규칙=치환).
# ---------------------------------------------------------------------------
def _parse_seq(s: str, i: int, rules: dict):
    acc = [""]
    n = len(s)
    while i < n:
        c = s[i]
        if c in "|)]":
            break
        if c in "([":
            inner, j = _parse_alts(s, i + 1, rules)  # j = 닫는 괄호 위치
            i = j + 1
            if c == "[":
                inner = inner + [""]
            acc = [a + b for a in acc for b in inner]
        elif c == "<":
            j = s.index(">", i)
            name = s[i + 1:j]
            rule_text = rules.get(name, "")
            inner, _ = _parse_alts(rule_text, 0, rules)
            i = j + 1
            acc = [a + b for a in acc for b in inner]
        else:
            acc = [a + c for a in acc]
            i += 1
    return acc, i


def _parse_alts(s: str, i: int, rules: dict):
    branches: list[str] = []
    while True:
        seqs, i = _parse_seq(s, i, rules)
        branches.extend(seqs)
        if i < len(s) and s[i] == "|":
            i += 1
            continue
        break
    return branches, i


def expand_grammar(text: str, rules: dict) -> list[str]:
    """인라인 문법 문자열 → 표면 변형 리스트(슬롯/조사 토큰은 그대로 남긴다)."""
    branches, _ = _parse_alts(text or "", 0, rules or {})
    seen = set()
    out = []
    for b in branches:
        # 공백 정규화는 슬롯 치환 뒤에 다시 한다(여기선 토큰 사이 이중공백만 정리).
        b = re.sub(r"[ \t]+", " ", b)
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


# ---------------------------------------------------------------------------
# 조사 토큰 계산 (앞 슬롯 표면형 받침 기준)
# ---------------------------------------------------------------------------
def _final_consonant(word: str) -> bool:
    if not word:
        return False
    ch = word[-1]
    return is_hangul_syllable(ch) and (ord(ch) - 0xAC00) % 28 != 0


def _josa_eun_neun(w: str) -> str:
    if not w or not is_hangul_syllable(w[-1]):
        return "은(는)"
    return "은" if _final_consonant(w) else "는"


def _josa_wa_gwa(w: str) -> str:
    if not w or not is_hangul_syllable(w[-1]):
        return "와(과)"
    return "과" if _final_consonant(w) else "와"


def _josa_ro(w: str) -> str:
    if not w or not is_hangul_syllable(w[-1]):
        return "(으)로"
    ch = w[-1]
    jong = (ord(ch) - 0xAC00) % 28
    # 받침 없음 또는 ㄹ받침(=8) → '로', 그 외 → '으로'
    return "로" if jong in (0, 8) else "으로"


# 조사 토큰명 → (계산 함수). 앞 표면형을 인자로 받는다.
_JOSA_TOKENS = {
    "이": josa_i_ga, "가": josa_i_ga,
    "을": josa_eul_reul, "를": josa_eul_reul,
    "은": _josa_eun_neun, "는": _josa_eun_neun,
    "와": _josa_wa_gwa, "과": _josa_wa_gwa,
    "로": _josa_ro, "으로": _josa_ro,
}

_PLACEHOLDER_RE = re.compile(r"\{([^}]*)\}")


def render_surface(template_text: str, combo: dict) -> str:
    """문법 전개된 문장 변형 + 슬롯 조합 → 최종 표면 문자열.

    ``{slot}`` = 표면형 삽입, ``{가}``/``{을}`` 등 = 앞 슬롯 표면형 받침 기준 조사.
    """
    out: list[str] = []
    last_surface = ""
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(template_text):
        out.append(template_text[pos:m.start()])
        name = m.group(1).strip()
        if name in combo:  # 슬롯 표면형
            surf = str(combo[name].get("surface", ""))
            out.append(surf)
            if surf.strip():
                last_surface = surf.strip()
        elif name in _JOSA_TOKENS:  # 조사 토큰
            out.append(_JOSA_TOKENS[name](last_surface))
        else:  # 알 수 없는 토큰 — 그대로 둔다
            out.append(m.group(0))
        pos = m.end()
    out.append(template_text[pos:])
    return normalize_ws("".join(out))


# ---------------------------------------------------------------------------
# gold 구체화(플레이스홀더 → 값)
# ---------------------------------------------------------------------------
_FULL_PH_RE = re.compile(r"^\{(\w+)\.(\w+)\}$")


def _domain_ids(inventory: dict, domain: str) -> list[str]:
    return sorted(e["entity_id"] for e in inventory.get("entities", [])
                  if e.get("domain") == domain)


_SENSOR_DOMAINS = {"binary_sensor", "sensor"}


def _resolve_ph(slot: str, attr: str, combo: dict, inventory: dict):
    filler = combo.get(slot)
    if attr == "expand":  # {scope.expand}
        domain = None
        if filler:
            domain = filler.get("domain")
        if not domain:
            # {scope.expand} 는 '액션 대상 기기' 도메인(문맥 도메인)으로 전개해야 한다.
            # 트리거/조건에 쓰인 모션·센서 슬롯(binary_sensor/sensor)은 스코프 대상이
            # 아니므로, 제어 기기 슬롯(device_*)을 1순위로, 그 외 비센서 엔티티 슬롯을
            # 2순위로 도메인을 유추한다(결정적: 슬롯 선언 순서 유지).
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
    """gold 구조를 재귀적으로 순회하며 ``{slot.attr}`` 를 실제 값으로 치환.

    YAML 에서 ``to: on`` / ``state: off`` 는 Python ``True``/``False`` 로 파싱되므로,
    RuleModel 규약(문자열 "on"/"off")에 맞춰 되돌린다(validate·파서 출력과 일치).
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
            val = _resolve_ph(m.group(1), m.group(2), combo, inventory)
            return val
        # 문자열 내 부분 치환(드묾)
        def sub(mm):
            v = _resolve_ph(mm.group(1), mm.group(2), combo, inventory)
            return str(v) if v is not None else mm.group(0)
        return re.sub(r"\{(\w+)\.(\w+)\}", sub, gold)
    return gold


def _gold_to_model(sentence: str, gold: dict) -> dict:
    """템플릿 gold(subrules 또는 top-level) → validate 가능한 model dict."""
    model = {"alias": sentence.strip(), "description": "", "mode": "single"}
    model.update(gold)
    return model


def _anchors_for(tpl: dict, slots: dict, combo: dict) -> list[list[str]]:
    """각 슬롯의 선택된 필러와 같은 값/엔티티를 갖는 표면 이형태 목록(패러프레이즈 앵커용)."""
    anchors = []
    for slot in tpl.get("slots", []):
        chosen = combo.get(slot)
        if not chosen:
            continue
        key = chosen.get("entity") or chosen.get("value") or chosen.get("surface")
        iforms = []
        for f in slots.get(slot, []):
            fk = f.get("entity") or f.get("value") or f.get("surface")
            if fk == key and f.get("surface"):
                iforms.append(f["surface"])
        if iforms:
            anchors.append(sorted(set(iforms)))
    return anchors


# ---------------------------------------------------------------------------
# 템플릿 전개
# ---------------------------------------------------------------------------
def _slot_combos(tpl: dict, slots: dict):
    """템플릿 slots 의 필러 카테시안 조합(순서 결정적)."""
    names = tpl.get("slots", [])
    pools = []
    for nm in names:
        fillers = slots.get(nm)
        if not fillers:
            return []  # 필요한 슬롯 사전이 없으면 이 템플릿은 생성 불가
        pools.append([(nm, f) for f in fillers])
    combos = [{}]
    for pool in pools:
        combos = [dict(c, **{nm: f}) for c in combos for (nm, f) in pool]
    return combos


def expand_template(tpl: dict, slots: dict, inventory: dict,
                    limit_per_template: int = 40, seed: int = 0,
                    mode_names=None) -> list[dict]:
    """한 템플릿 → CorpusItem 리스트(문법×슬롯, 상한 균등 표본).

    expansion_rules 는 slots.yaml 의 공용 예약키(``<off>``/``<on>`` 등)와 템플릿별 정의를
    병합한다. 키는 각괄호 유무를 정규화(``<off>`` == ``off``)해 문장부의 ``<off>`` 참조와 맞춘다.
    """
    rules = {}
    for src in (slots.get("expansion_rules") or {}, tpl.get("expansion_rules") or {}):
        for k, v in src.items():
            rules[k.strip("<>")] = v
    variants = expand_grammar(tpl.get("template", ""), rules)
    combos = _slot_combos(tpl, slots)
    if not variants or not combos:
        return []

    pairs = [(v, c) for v in variants for c in combos]
    # even 분포: 템플릿당 상한 균등 + seed 고정 셔플(조합수 비례 편향 금지, §7.3)
    rng = random.Random(f"{seed}:{tpl.get('id','?')}")
    rng.shuffle(pairs)
    if limit_per_template and len(pairs) > limit_per_template:
        pairs = pairs[:limit_per_template]

    mode_names = set(mode_names or [])
    items = []
    for k, (variant, combo) in enumerate(pairs):
        sentence = render_surface(variant, combo)
        gold = concretize_gold(tpl.get("gold", {}), combo, inventory)
        model = _gold_to_model(sentence, gold)
        errs = validate_rule_model(model, inventory, mode_names)
        items.append({
            "id": f"{tpl.get('id','tpl')}#{k}",
            "sentence": sentence,
            "gold": model,
            "area": tpl.get("area", ""),
            "template_id": tpl.get("id", ""),
            "source": "grammar",
            "tags": list(tpl.get("tags", [])),
            "gold_invalid": bool(errs),
            "gold_errors": [e.get("message") if isinstance(e, dict) else str(e)
                            for e in errs],
            "anchors": _anchors_for(tpl, slots, combo),
        })
    return items


def generate(templates, slots, inventory, seed: int = 0,
             limit_per_template: int = 40, mode_names=None) -> list[dict]:
    """전체 템플릿 → CorpusItem 리스트(결정적)."""
    out = []
    for tpl in templates:
        out.extend(expand_template(tpl, slots, inventory,
                                   limit_per_template=limit_per_template,
                                   seed=seed, mode_names=mode_names))
    return out
