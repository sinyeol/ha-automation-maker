"""Phase 3C — CLI 학습(사용자 선택 옵션). 미해석 문장을 구독(Max) claude CLI 로
'라이브러리가 아는 표준 문형'으로 재작성 → 재파싱 → 검증 → 로컬 학습(자기학습 복리).

두 부분:
  (a) async analyze(sentence, gazetteer, settings, inventory) → {normalized, model,
      template_id, ok, warnings} | None
      - cli_client 헬퍼를 재사용해 정규화 프롬프트를 헤드리스 claude 로 호출한다
        (--json-schema {normalized, matched_template_id, changed, confidence, notes},
         새 기기/방/모드 추가 금지, few-shot = pattern_library 유사 예시).
      - 정규형을 규칙 파서(L1) + 템플릿 매처(L2)로 재파싱해 model 을 만든다.
      - 안전: 엔티티 실존 검증(llm_assist._validate_entities 재사용) + 환각 방어
        (정규형이 가리키는 방/모드/사람/기기가 원문에 없으면 거부).
  (b) class LearnedStore(JsonStore) — /data/learned_patterns.yaml
      - add / match(원문·근접 재매칭) / all / delete / revalidate(재기동 시 엔티티 재검증).

외부 파이썬 의존 없음(표준 라이브러리 + PyYAML). CLI 경로는 cli_client 헬퍼만 읽기 전용 재사용.
토큰/키는 어떤 경로로도 로그에 남기지 않는다.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from backend.engine.rule_model import validate_rule_model
from backend.engine.storage import JsonStore, data_dir
from backend.nl import cli_client
from backend.nl.llm_assist import _validate_entities
from backend.nl.parser import parse as nl_parse
from backend.nl.pattern_match import TemplateMatcher, load_pattern_library

log = logging.getLogger("automation_maker.learn")

# 재작성은 순수 파싱보다 어렵다 — 기본 sonnet, env 로 재정의 가능(cli_normalize.py 스펙).
_MODEL = os.environ.get("CLI_LEARN_MODEL", "sonnet")
_TIMEOUT = 90  # 초
_NEAR_TAU = 0.85  # LearnedStore 근접 재매칭 임계(문자 3-gram Jaccard)

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
# 유틸
# ---------------------------------------------------------------------------
def _ws(s) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()) if isinstance(s, str) else ""


def _char_ngrams(s: str, n: int = 3) -> set:
    s = _ws(s).replace(" ", "")
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# few-shot 선택 (어절 + 문자 n-gram Jaccard top-k) — cli_normalize.py 스펙 이식.
# ---------------------------------------------------------------------------
def _word_set(s: str) -> set:
    return set(_ws(s).split())


def build_fewshot_pool(pattern_library: list) -> list:
    """pattern_library → few-shot 후보 풀 [{id, template, example}]. 예시 문장 단위 전개."""
    pool = []
    for tpl in pattern_library or []:
        tid = tpl.get("id")
        template = tpl.get("template", "")
        for ex in tpl.get("examples", []) or []:
            if isinstance(ex, str) and ex.strip():
                pool.append({"id": tid, "template": template, "example": ex.strip()})
    return pool


def select_fewshot(sentence: str, pool: list, k: int = 5) -> list:
    """입력과 어절/문자 n-gram Jaccard 평균이 높은 예시 top-k(결정적 정렬)."""
    sw, sg = _word_set(sentence), _char_ngrams(sentence)
    scored = []
    for i, ex in enumerate(pool):
        text = ex.get("example", "")
        sc = 0.5 * _jaccard(sw, _word_set(text)) + 0.5 * _jaccard(sg, _char_ngrams(text))
        scored.append((sc, i, ex))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [ex for (_sc, _i, ex) in scored[:k]]


# pattern_library 는 프로세스 수명 동안 불변 → 1회 로드 캐시.
_LIB: Optional[list] = None


def _library() -> list:
    global _LIB
    if _LIB is None:
        _LIB = load_pattern_library()
    return _LIB


# ---------------------------------------------------------------------------
# 프롬프트 구성 (인벤토리 다이제스트 + few-shot)
# ---------------------------------------------------------------------------
def _inventory_digest(inventory: dict, settings: Optional[dict]) -> str:
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
            lines.append(f"- ({tid}) {ex.get('example', '')}")
        lines.append("")
    lines.append("[재작성할 입력 문장]")
    lines.append(sentence)
    lines.append("")
    lines.append("위 입력을, 의미를 보존한 채 표준 예시와 같은 어순/표현의 한 문장으로 "
                 "재작성해 JSON 으로 답하라. 맞는 표준 문형이 없으면 원문 유지 + "
                 "matched_template_id=null + changed=false.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 호출 (cli_client 헬퍼 재사용, 정규화 스키마)
# ---------------------------------------------------------------------------
async def _run_cli_normalize(prompt: str, oauth_token: str) -> Optional[dict]:
    """헤드리스 claude 호출 → structured_output dict 또는 None(실패는 조용히)."""
    binary = cli_client._find_binary()
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
    env = cli_client._build_env(oauth_token)
    cwd = str(cli_client._work_dir())
    async with cli_client._SEMAPHORE:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("claude CLI 정규화 타임아웃(%ss)", _TIMEOUT)
            await cli_client._kill(proc)
            return None
        except (OSError, ValueError):
            log.warning("claude CLI 정규화 실행 실패")
            await cli_client._kill(proc)
            return None
    return _parse_norm_output(proc.returncode, stdout)


def _parse_norm_output(returncode: Optional[int], stdout: bytes) -> Optional[dict]:
    if not stdout:
        return None
    try:
        data = json.loads(stdout.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if returncode not in (0, None) or data.get("is_error"):
        return None
    struct = data.get("structured_output")
    return struct if isinstance(struct, dict) else None


# ---------------------------------------------------------------------------
# 재파싱 · 환각 방어 · 엔티티 수집
# ---------------------------------------------------------------------------
def _reparse(normalized: str, gz, settings: dict, matcher: TemplateMatcher):
    """정규형 → (model, template_id). 규칙 파서(ok) 우선, 아니면 템플릿 매처."""
    res = nl_parse(normalized, gz, settings)
    if res.get("ok") and isinstance(res.get("model"), dict):
        return res["model"], None
    mr = matcher.match(normalized)
    if mr and isinstance(mr.get("model"), dict):
        return mr["model"], mr.get("matched_id")
    # 최선 노력: 파서 결과(미완성이라도) 반환 → 검증에서 ok=False 로 걸린다.
    return res.get("model"), None


def _anchor_sets(gz, text: str):
    """문장에서 gazetteer 로 해석되는 (방, 모드, 엔티티) id 집합(환각 방어용).

    분류 일관성이 핵심이다(원문·정규형에 같은 규약):
    - 모드 표면형을 먼저 선점한다(scene 엔티티 이름과 겹치는 '슬립 모드'/'취침 모드' 등을
      모드로 일관 분류 — 매처의 모드 선점과 동일).
    - 사람(person)은 엔티티 집합으로 합친다. 같은 인물이 이름(entity_surface '와이프')으로도
      동의어(person_surface '아내')로도 잡히므로, 둘을 같은 person.* id 로 통일해야 한다.
    """
    areas, modes, entities = set(), set(), set()
    claimed: list = []  # 모드로 선점된 (s, e) 스팬

    def _overlaps(s, e):
        return any(not (e <= cs or s >= ce) for cs, ce in claimed)

    for surf in sorted(gz.mode_surfaces, key=len, reverse=True):
        start = 0
        while True:
            i = text.find(surf, start)
            if i < 0:
                break
            s, e = i, i + len(surf)
            if not _overlaps(s, e):
                modes.add(gz.mode_canonical.get(surf, surf))
                claimed.append((s, e))
            start = e

    for sp in gz.match(text):
        s, e = sp.get("start"), sp.get("end")
        if s is None or _overlaps(s, e):
            continue  # 모드 선점 스팬은 엔티티로 오분류하지 않는다
        typ = sp.get("type")
        cands = sp.get("candidates") or []
        if not cands:
            continue
        if typ == "area":
            areas.add(cands[0].get("id"))
        elif typ == "mode":
            mid = cands[0].get("id")
            modes.add(gz.mode_canonical.get(mid, mid))
        elif typ == "person":
            entities.add(cands[0].get("id"))          # 사람 = 엔티티로 통일
        elif typ == "entity":
            for c in cands:
                if c.get("id"):
                    entities.add(c["id"])
    return areas, modes, entities


def _hallucination_ok(gz, original: str, normalized: str):
    """정규형이 원문에 없는 방/모드/기기(사람 포함)를 새로 만들었는지 검사. (ok, warning)."""
    oa, om, oe = _anchor_sets(gz, original)
    na, nm, ne = _anchor_sets(gz, normalized)
    bad = []
    if not na <= oa:
        bad.append("방")
    if not nm <= om:
        bad.append("모드")
    for e in ne:
        if e in oe:
            continue
        ent = gz.entity(e)
        aid = ent.get("area_id") if ent else None
        name = (ent.get("name") if ent else "") or ""
        # 명명 엔티티는 그 방이 원문에 있거나 이름이 원문에 등장할 때만 허용.
        if (aid and aid in oa) or (name and name in original):
            continue
        bad.append("기기")
        break
    if bad:
        return False, ("원문에 없는 대상을 새로 만든 것 같아요("
                       + "/".join(sorted(set(bad))) + "). 저장할 수 없어요.")
    return True, None


def _digest(inventory: dict, settings: dict) -> dict:
    return {
        "entities": (inventory or {}).get("entities", []),
        "persons": (settings or {}).get("persons") or {},
        "modes": (settings or {}).get("modes") or {},
    }


def model_entities(model: dict) -> list:
    """model(서브룰 포함) 전체에서 참조하는 entity_id 목록(정렬, 중복 제거)."""
    found: set = set()
    stack = [model]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            eid = node.get("entity_id")
            if isinstance(eid, str) and eid:
                found.add(eid)
            tgt = node.get("target")
            if isinstance(tgt, dict):
                ids = tgt.get("entity_id")
                for i in (ids if isinstance(ids, list) else [ids]):
                    if isinstance(i, str) and i:
                        found.add(i)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return sorted(found)


# ---------------------------------------------------------------------------
# 공개 API: analyze
# ---------------------------------------------------------------------------
async def analyze(sentence: str, gazetteer, settings: dict, inventory: dict, *,
                  oauth_token: Optional[str] = None,
                  allow_live: bool = True) -> Optional[dict]:
    """미해석 문장을 CLI 로 표준 문형으로 재작성 → 재파싱 → 검증.

    반환: {"normalized", "model", "template_id", "ok", "warnings", "entities"} 또는 None
    (문장 없음·CLI 불가·재작성 실패). ok=False 여도 미리보기용으로 결과는 돌려준다.
    """
    sent = _ws(sentence)
    if not sent:
        return None
    if oauth_token is None:
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")

    lib = _library()
    few = select_fewshot(sent, build_fewshot_pool(lib), k=5)
    prompt = _build_prompt(sent, few, inventory, settings)

    if not allow_live:
        return None
    struct = await _run_cli_normalize(prompt, oauth_token)
    if struct is None:
        return None
    normalized = struct.get("normalized")
    if not isinstance(normalized, str) or not normalized.strip():
        return None
    normalized = _ws(normalized)
    template_id = struct.get("matched_template_id")
    if not isinstance(template_id, str) or not template_id.strip():
        template_id = None

    warnings: list = []
    hall_ok, hall_warn = _hallucination_ok(gazetteer, sent, normalized)
    if hall_warn:
        warnings.append(hall_warn)

    matcher = TemplateMatcher(lib, gazetteer, inventory)
    model, mid = _reparse(normalized, gazetteer, settings, matcher)
    if mid and not template_id:
        template_id = mid
    if not isinstance(model, dict):
        return {"normalized": normalized, "model": None, "template_id": template_id,
                "ok": False, "entities": [],
                "warnings": warnings + ["정규형을 규칙으로 재해석하지 못했어요."]}

    # 엔티티 실존 검증(존재하지 않는 id 는 None 으로 강등 + 경고) — llm_assist 재사용.
    validated = _validate_entities({"model": model}, _digest(inventory, settings))
    model = validated.get("model", model)
    for w in validated.get("warnings") or []:
        if w not in warnings:
            warnings.append(w)

    mode_names = set((settings or {}).get("modes") or {})
    errs = validate_rule_model(model, inventory, mode_names)
    for e in errs:
        msg = e.get("message") if isinstance(e, dict) else str(e)
        if msg and msg not in warnings:
            warnings.append(msg)

    ok = bool(hall_ok and not errs)
    return {"normalized": normalized, "model": model, "template_id": template_id,
            "ok": ok, "warnings": warnings, "entities": model_entities(model)}


# ---------------------------------------------------------------------------
# LearnedStore — /data/learned_patterns.yaml (JsonStore 를 YAML 로 확장)
# ---------------------------------------------------------------------------
def _entry_id(raw: str) -> str:
    return hashlib.sha1(_ws(raw).encode("utf-8")).hexdigest()[:12]


class LearnedStore(JsonStore):
    """학습된 (원문 → 정규형 → model) 매핑 저장소. 같은/유사 문장은 CLI 없이 로컬 처리.

    포맷(YAML 리스트): [{id, raw, normalized, model, entities, created, hits}].
    """

    def __init__(self, path=None, loop=None):
        p = Path(path) if path else (data_dir() / "learned_patterns.yaml")
        super().__init__(p, [], loop)
        if not isinstance(self.data, list):
            self.data = []

    # JsonStore 는 JSON 이지만 학습 파일은 사람이 읽기 쉬운 YAML 로 저장한다.
    def _load(self, default):
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return data if isinstance(data, list) else default
        except FileNotFoundError:
            return default
        except (ValueError, OSError, yaml.YAMLError):
            return default

    def _write(self) -> None:
        self._timer = None
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self.data, fh, allow_unicode=True, sort_keys=False)
        os.replace(tmp, self.path)
        self._dirty = False

    # ---- CRUD ----
    def add(self, raw: str, normalized: str, model: dict, entities: list) -> dict:
        """학습 항목 추가(같은 원문은 교체). 저장 예약."""
        entry = {
            "id": _entry_id(raw),
            "raw": _ws(raw),
            "normalized": _ws(normalized),
            "model": copy.deepcopy(model) if isinstance(model, dict) else {},
            "entities": sorted(set(entities or [])),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hits": 0,
        }
        self.data = [e for e in self.data if e.get("id") != entry["id"]]
        self.data.append(entry)
        self.save_soon()
        return entry

    def match(self, sentence: str) -> Optional[dict]:
        """원문/정규형 완전일치 → 없으면 근접(문자 3-gram Jaccard ≥ τ) 재매칭. model 사본 반환."""
        key = _ws(sentence)
        if not key or not isinstance(self.data, list) or not self.data:
            return None
        best = None
        for e in self.data:
            if _ws(e.get("raw")) == key or _ws(e.get("normalized")) == key:
                best = e
                break
        if best is None:
            kg = _char_ngrams(key)
            best_sc = 0.0
            for e in self.data:
                sc = max(_jaccard(kg, _char_ngrams(_ws(e.get("raw")))),
                         _jaccard(kg, _char_ngrams(_ws(e.get("normalized")))))
                if sc > best_sc:
                    best_sc, best_cand = sc, e
            best = best_cand if best_sc >= _NEAR_TAU else None
        if best is None:
            return None
        best["hits"] = int(best.get("hits", 0)) + 1
        self.save_soon()
        model = copy.deepcopy(best.get("model") or {})
        if isinstance(model, dict):
            model["alias"] = sentence.strip()
        return model

    def all(self) -> list:
        return [copy.deepcopy(e) for e in self.data if isinstance(e, dict)]

    def delete(self, entry_id: str) -> bool:
        before = len(self.data)
        self.data = [e for e in self.data if e.get("id") != entry_id]
        if len(self.data) != before:
            self.save_soon()
            return True
        return False

    def revalidate(self, inventory: dict) -> list:
        """재기동 시 호출: 저장된 엔티티가 인벤토리에 더는 없으면 그 항목을 제거한다.

        반환: 제거된 항목 id 목록(빈 리스트면 변화 없음).
        """
        valid = {e.get("entity_id") for e in (inventory or {}).get("entities", [])
                 if e.get("entity_id")}
        # 방어: 인벤토리가 비면(로드 실패로 추정) 프루닝하지 않는다. 일시적 장애 한 번으로
        # 사용자가 학습시킨 패턴을 전부 지우는 사고를 막는다(호출부에도 동일 가드 존재).
        if not valid:
            return []
        kept, removed = [], []
        for e in self.data:
            ents = e.get("entities") or []
            if all(x in valid for x in ents):
                kept.append(e)
            else:
                removed.append(e.get("id"))
        if removed:
            self.data = kept
            self.save_soon()
        return removed
