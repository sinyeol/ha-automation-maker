"""한국어 자연어 규칙 파서 (v2). 표준 라이브러리만 사용(llm_assist 제외)."""
from .gazetteer import Gazetteer
from .parser import parse

__all__ = ["Gazetteer", "parse"]
