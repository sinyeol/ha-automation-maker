"""§3.3 정직 채점기 — 단일 소스 셔임(APP-PORT-PLAN §4b).

이 파일의 실제 구현은 앱 tests 로 이식됐다: `automation_maker/tests/structural_compare.py`.
중복(두 벌 유지)을 막기 위해 tools/corpus 는 앱 본을 그대로 재사용한다. 앱 import 루트를
배선한 뒤 전 심볼을 재노출한다(compare/normalize_model + 내부 헬퍼·필드상수까지).

역사적 배경: 이 파일이 원본이었고 오버레이/evaluate 가 여기서 import 했다. Phase 7 에서
정직 채점을 앱 회귀 테스트에 편입하며 단일 소스를 앱 쪽으로 옮겼다.
"""
from __future__ import annotations

import os
import sys

_APP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

# 공개 API + tools/tests 가 참조하는 내부 심볼·필드상수까지 전량 재노출(단일 소스).
from tests.structural_compare import (  # noqa: E402,F401
    _ACTION_FIELDS, _COND_FIELDS, _TRIGGER_FIELDS, _canon_duration, _canon_node,
    _canon_scalar, _canon_value, _classify_pair, _diff_category, _flatten_subrules,
    _multiset, _node_get, _remove_first, compare, normalize_model)
