"""SPEC-ACCURACY §2.6 — L3 CLI 정규화 (하이브리드 파서의 최종 폴백, 앱 미수정).

규칙 파서(L1/L1A)와 템플릿 매처(L2)가 못 읽는 문장을, 구독(Max) 로그인 상태의 ``claude``
CLI 로 **"라이브러리가 아는 표준 문형으로 재작성"** 시킨 뒤 그 정규형을 다시 파싱한다.

- 앱 미수정: ``backend.nl.cli_client`` 의 헬퍼(``_find_binary``/``_build_env``/``_work_dir``)만
  **읽기 전용 재사용**한다(모듈 수정·몽키패치 없음). 스키마는 정규화 전용으로 새로 정의한다.
- 결정적 재현·저비용: ``out/cli_cache.jsonl`` 에 (sentence → 결과)를 캐시한다. 캐시가 있으면
  CLI 를 호출하지 않고 그대로 반환하므로, 한 번 채운 캐시로 재실행하면 완전히 결정적이다.
- 안전 게이트(프롬프트 강제): 새 기기/방/모드 추가 금지, 의미 보존, 맞는 표준 문형이 없으면
  ``matched=null`` + 원문 유지(``changed=false``). few-shot 은 pattern_library 예시에서
  어절/문자 n-gram Jaccard top-5 로 선택해 주입한다.

공개 API::

    normalize(sentence, few_shot_examples, inventory) -> {normalized, matched_id, changed} | None

실패(바이너리 부재·타임아웃·오류·스키마 불충족)는 모두 ``None``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Optional

# --- 앱 import 루트 배선 (읽기 전용) --------------------------------------
_APP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

# cli_client 의 인증·격리 헬퍼를 읽기 전용 재사용(모듈 미변경).
from backend.nl import cli_client as _cc  # noqa: E402

_OUT_DIR = os.path.join(os.path.dirname(__file__), "out")
_CACHE_PATH = os.path.join(_OUT_DIR, "cli_cache.jsonl")

# 재작성은 순수 파싱보다 어렵다 — 기본 sonnet, env 로 재정의 가능.
_MODEL = os.environ.get("CLI_NORMALIZE_MODEL", "sonnet")
_TIMEOUT = 90  # 초

# --json-schema: 정규화 전용(얕게 강제). matched_template_id 는 문자열 또는 null.
_NORM_SCHEMA = {
    "type": "object",
    "properties": {
        "normalized": {"type": "string"},
        "matched_template_id": {"type": ["string", "null"]},
        "changed": {"type": "boolean"},
        "confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["normalized", "changed"],
}

_SYSTEM_PROMPT = (
    "너는 한국어 홈 오토메이션 명령 문장을, 규칙 기반 파서가 이미 아는 '표준 문형'으로 "
    "재작성하는 정규화기다. 아래 원칙을 반드시 지켜라.\n"
    "1) 의미(트리거·조건·액션·대상 기기/방/모드/수치)를 100% 보존한다. 새로운 기기·방·"
    "모드·수치를 추가하거나 바꾸지 않는다. 인벤토리에 없는 엔티티를 만들지 않는다.\n"
    "2) few-shot 으로 주어진 '표준 예시 문형' 중 입력과 의미가 같은 문형의 어순·표현으로 "
    "바꾼다. 구어체·축약·조사 분리를 표준 어순으로 편다.\n"
    "3) 입력과 의미가 같은 표준 문형이 없으면 원문을 그대로 두고 matched_template_id=null, "
    "changed=false 로 둔다. 억지로 바꾸지 않는다.\n"
    "4) 오직 JSON 스키마(normalized, matched_template_id, changed, confidence, notes)로만 "
    "답한다. normalized 는 재작성된(또는 원문) 한국어 문장 한 줄이다."
)


# ---------------------------------------------------------------------------
# few-shot 선택 (어절 + 문자 n-gram Jaccard top-k)
# ---------------------------------------------------------------------------
def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _char_ngrams(s: str, n: int = 3) -> set:
    s = _norm_ws(s).replace(" ", "")
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _word_set(s: str) -> set:
    return set(_norm_ws(s).split())


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_fewshot_pool(pattern_library: list) -> list:
    """pattern_library → few-shot 후보 풀 [{id, template, example}]. 예시 문장 단위로 전개."""
    pool = []
    for tpl in pattern_library or []:
        tid = tpl.get("id")
        template = tpl.get("template", "")
        for ex in tpl.get("examples", []) or []:
            if isinstance(ex, str) and ex.strip():
                pool.append({"id": tid, "template": template, "example": ex.strip()})
    return pool


def select_fewshot(sentence: str, pool: list, k: int = 5) -> list:
    """입력 문장과 어절/문자 n-gram Jaccard 평균이 높은 예시 top-k(결정적 정렬)."""
    sw, sg = _word_set(sentence), _char_ngrams(sentence)
    scored = []
    for i, ex in enumerate(pool):
        text = ex.get("example", "")
        sc = 0.5 * _jaccard(sw, _word_set(text)) + 0.5 * _jaccard(sg, _char_ngrams(text))
        scored.append((sc, i, ex))
    # 점수 내림차순 → 동률은 원래 인덱스(결정적)
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [ex for (_sc, _i, ex) in scored[:k]]


# ---------------------------------------------------------------------------
# 인벤토리 다이제스트 (허용 기기/방/모드 — '새로 만들기' 억제용)
# ---------------------------------------------------------------------------
def _inventory_digest(inventory: dict, settings: Optional[dict] = None) -> str:
    inv = inventory or {}
    areas = sorted({a.get("name") for a in inv.get("areas", []) if a.get("name")})
    ents = []
    for e in inv.get("entities", []):
        name = e.get("name") or e.get("entity_id")
        if name:
            ents.append(name)
    ents = sorted(set(ents))[:60]
    modes = sorted((settings or {}).get("modes", {}).keys())
    parts = []
    if areas:
        parts.append("방: " + ", ".join(areas))
    if modes:
        parts.append("모드: " + ", ".join(modes))
    if ents:
        parts.append("기기/센서: " + ", ".join(ents))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 캐시
# ---------------------------------------------------------------------------
_cache: Optional[dict] = None


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    if os.path.exists(_CACHE_PATH):
        with open(_CACHE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and "sentence" in rec:
                    _cache[rec["sentence"]] = rec.get("result")
    return _cache


def _cache_put(sentence: str, result) -> None:
    cache = _load_cache()
    cache[sentence] = result
    os.makedirs(_OUT_DIR, exist_ok=True)
    with open(_CACHE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"sentence": sentence, "result": result},
                           ensure_ascii=False) + "\n")


def cache_has(sentence: str) -> bool:
    return _norm_ws(sentence) in _load_cache()


# ---------------------------------------------------------------------------
# CLI 호출
# ---------------------------------------------------------------------------
def _build_prompt(sentence: str, few_shot: list, inventory: dict,
                  settings: Optional[dict]) -> str:
    lines = []
    digest = _inventory_digest(inventory, settings)
    if digest:
        lines.append("[사용 가능한 방/모드/기기 — 이 목록 밖의 대상을 새로 만들지 마라]")
        lines.append(digest)
        lines.append("")
    if few_shot:
        lines.append("[표준 예시 문형(파서가 아는 형태)]")
        for ex in few_shot:
            tid = ex.get("id", "")
            lines.append(f"- ({tid}) {ex.get('example','')}")
        lines.append("")
    lines.append("[재작성할 입력 문장]")
    lines.append(sentence)
    lines.append("")
    lines.append("위 입력을, 의미를 보존한 채 표준 예시와 같은 어순/표현의 한 문장으로 "
                 "재작성해 JSON 으로 답하라. 맞는 표준 문형이 없으면 원문 유지 + "
                 "matched_template_id=null + changed=false.")
    return "\n".join(lines)


def _run_cli(prompt: str) -> Optional[dict]:
    """헤드리스 claude 호출 → structured_output dict 또는 None. cli_client 헬퍼 재사용."""
    binary = _cc._find_binary()
    if not binary:
        return None
    schema_json = json.dumps(_NORM_SCHEMA, ensure_ascii=False)
    cmd = [
        binary,
        "-p", prompt,
        "--output-format", "json",
        "--json-schema", schema_json,
        "--system-prompt", _SYSTEM_PROMPT,
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-turns", "2",
        "--model", _MODEL,
    ]
    env = _cc._build_env("")
    cwd = str(_cc._work_dir())
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=_TIMEOUT,
                              start_new_session=True)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if not proc.stdout:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if proc.returncode not in (0, None) or data.get("is_error"):
        return None
    struct = data.get("structured_output")
    return struct if isinstance(struct, dict) else None


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------
def normalize(sentence: str, few_shot_examples: list, inventory: dict,
              settings: Optional[dict] = None, *,
              allow_live: bool = True) -> Optional[dict]:
    """문장을 라이브러리 표준 문형으로 재작성. 캐시 우선, 미스면 CLI(allow_live).

    반환: ``{"normalized": str, "matched_id": str|None, "changed": bool,
            "confidence": float, "notes": str}`` 또는 ``None``(실패/미해결).
    캐시 히트는 항상 반환(allow_live 무관). 캐시 미스이고 allow_live=False 면 None.
    """
    key = _norm_ws(sentence)
    cache = _load_cache()
    if key in cache:
        return cache[key]
    if not allow_live:
        return None

    prompt = _build_prompt(sentence, few_shot_examples or [], inventory, settings)
    struct = _run_cli(prompt)
    if struct is None:
        # 실패는 캐시하지 않는다(다음 실행에서 재시도 가능). None 반환.
        return None

    normalized = struct.get("normalized")
    if not isinstance(normalized, str) or not normalized.strip():
        result = None
    else:
        matched = struct.get("matched_template_id")
        if not isinstance(matched, str) or not matched.strip():
            matched = None
        changed = bool(struct.get("changed")) and _norm_ws(normalized) != key
        conf = struct.get("confidence")
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        notes = struct.get("notes") if isinstance(struct.get("notes"), str) else ""
        result = {"normalized": _norm_ws(normalized), "matched_id": matched,
                  "changed": changed, "confidence": conf, "notes": notes}

    _cache_put(key, result)
    return result


def available() -> bool:
    """CLI 바이너리 존재 여부(스모크 전 체크용)."""
    return _cc._find_binary() is not None
