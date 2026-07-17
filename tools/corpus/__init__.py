"""패턴 라이브러리 도구 (tools/corpus).

앱 코드는 **읽기 전용 import 만** 한다(SPEC-CORPUS §계약). 이 패키지는 앱의 파서를
구동해 커버리지·갭을 측정하는 오프라인 파이프라인이며, 출력은 `tools/corpus/out/` 아래에만
쓴다. 앱(backend·frontend)은 절대 수정하지 않는다.

파이프라인: generate → (augment) → evaluate → mine → run(report).
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# 앱 import 루트 배선: `import backend...` 가 되도록 automation_maker/ 를 sys.path 에 넣는다.
# 이 파일 위치 = <repo>/tools/corpus/__init__.py → 앱 루트 = <repo>/automation_maker.
# ---------------------------------------------------------------------------
_APP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)
