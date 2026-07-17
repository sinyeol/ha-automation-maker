# tools/corpus — 패턴 라이브러리 생성·평가 도구

파서의 **패턴 라이브러리를 보강**하기 위한 오프라인 파이프라인이다. 다중규칙·수동매핑·모드
문장의 예시를 생성하고, **현재 파서를 구동**해 커버리지·갭을 측정한다. 최종 지향은
"코드화된 규칙 + 예시 라이브러리"의 **하이브리드**(설계는 `docs/HYBRID-PARSER.md`).

> 이 도구는 앱을 수정하지 않는다. `backend.*` 는 **읽기 전용 import** 만 하고, 출력은 전부
> `tools/corpus/out/` 아래에 쓴다.

## 파이프라인

```
templates.yaml + slots.yaml
   │  generate.py   (hassil 인라인 문법 × 슬롯 조합, 조사 자동, gold 자가검증)
   ▼
corpus(grammar) ─┐
paraphrases.yaml ┤  augment.py  (gold 상속·구체화 + dedup + 라벨보존 가드)
   ▼             │
corpus(+paraphrase)
   │  evaluate.py  (parse() 구동 → structural_match.compare → 정확/부분/실패)
   ▼
out/results.jsonl + out/coverage_report.md
   │  mine.py      (실패·부분 → 추상 템플릿 delexicalize → 갭 클러스터)
   ▼
out/gap_library.yaml + out/pattern_library.yaml
   │  run.py
   ▼
out/report.md (종합)
```

## 실행

```bash
python tools/corpus/run.py                 # 전체(문법 + 패러프레이즈)
python tools/corpus/run.py --no-augment    # 문법 코퍼스만
python tools/corpus/run.py --limit 20      # 템플릿당 상한 20 (기본 40, 균등)
python tools/corpus/run.py --seed 7        # 셔플 시드 변경
```

`PyYAML` 외 외부 의존은 없다(앱 requirements 에 이미 존재). 모든 단계는 결정적이다
(random/Date 미사용 — seed 고정 셔플).

### 입력 파일 (시드 담당이 작성)
- `templates.yaml` — 시드 문법(라벨 자동). hassil 인라인 문법 지원:
  `[옵션]`, `(대안|대안)`, `<expansion_rule>`. 조사 토큰 `{가}`/`{을}`/`{은}`/`{와}`/`{로}` 는
  앞 슬롯 표면형 받침 기준으로 자동 계산된다. 슬롯 표면형은 `{slot}`, gold 값 삽입은
  `{slot.value}`/`{slot.entity}`/`{slot.dur}`/`{scope.expand}`.
- `slots.yaml` — 공유 슬롯 필러 사전(표면형 → 엔티티/값). 엔티티는 `mock_data` 실존 id 만.
- `paraphrases.yaml` — LLM/에이전트 생성 자연 변형. `gold_ref` 로 템플릿 gold 상속,
  선택적 `bind: {slot: surface}` 로 구체화할 필러 지정(기본=첫 필러).

templates/slots 가 아직 없어도 `run.py` 는 빈 코퍼스로 오류 없이 완주한다.

## 산출물 (`out/`)

| 파일 | 설명 | 커밋 |
|---|---|---|
| `report.md` | 종합 리포트(규모·커버리지·상위 갭·하이브리드 권고) | 스냅샷 커밋 |
| `gap_library.yaml` | 추가 후보 패턴(빈도순, 오류태그) | 스냅샷 커밋 |
| `pattern_library.yaml` | 시드 템플릿 + 커버 상태[covered/partial/gap] + 예시 (하이브리드 데이터 자산) | 스냅샷 커밋 |
| `coverage_report.md` | 영역·템플릿·**소스별(grammar/paraphrase) 분리** 정확률표 | 재생성 |
| `results.jsonl` | item별 판정·diff 덤프(대용량) | **gitignore** |

### .gitignore 권장 항목
아래는 재생성 가능한 대용량 덤프이므로 통합 담당이 저장소 `.gitignore` 에 추가한다:

```
tools/corpus/out/results.jsonl
tools/corpus/out/corpus_*.jsonl
```

`out/report.md`, `out/gap_library.yaml`, `out/pattern_library.yaml` 은 스냅샷으로 커밋한다.

## 판정 기준 (structural_match)
- **exact** — 모든 subrule 의 triggers/conditions/actions 핵심필드 집합이 일치.
- **partial** — triggers 는 맞고 일부 조건/액션/서브룰 개수 불일치(또는 `ok=False`).
- **fail** — `ok=False`+triggers 불일치, 파서 예외, 또는 triggers 불일치.

diff 오류 5분류(§7.6): `missing_node` / `extra_node` / `value_mismatch` /
`wrong_node_type` / `entity_confusion` — `gap_library.yaml` 의 진단열로 쓰인다.

## 신뢰성 장치
- **gold 자가검증**(§7.4): 각 CorpusItem 의 gold 를 `validate_rule_model` 로 검증해
  템플릿 자체 오류를 `gold_invalid` 로 격리 → 파서 커버리지 통계에서 제외.
- **라벨보존 가드**(§7.5): 슬롯 앵커링 + gazetteer 해석성으로 패러프레이즈 드리프트 차단.
  **파서 재파싱은 필터가 아니라 신호**(검사기=피시험 파서면 갭 발견이 무너진다).
- **낙관편향 표기**(§7.7): grammar 문장은 템플릿 동형이라 커버리지가 낙관적 → 리포트는
  paraphrase 소스 정확률을 별도로 보여준다.
