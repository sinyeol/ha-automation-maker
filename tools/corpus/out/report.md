# 패턴 라이브러리 종합 리포트

## 1. 코퍼스 규모
- 총 1079문장 (grammar 917 · paraphrase 162)
- 영역별: misparse 14, mode 360, multiclause 312, single 22, target 349, time 14, value 8
- gold_invalid(격리): 0

## 2. 현재 파서 커버리지
- exact 520 · partial 313 · fail 246 · 전체 정확률 48.2%

| 소스 | exact | partial | fail | 정확률 |
|---|---:|---:|---:|---:|
| grammar | 479 | 269 | 169 | 52.2% |
| paraphrase | 41 | 44 | 77 | 25.3% |

> §7.7 낙관편향: grammar 는 템플릿 동형이라 낙관적. 일반화 지표는 **paraphrase 정확률** 우선.

## 3. 상위 갭 패턴 (빈도순)
| # | 빈도 | 영역 | 오류태그 | 추상 패턴 | 예시 |
|---:|---:|---|---|---|---|
| 1 | 13 | multiclause | extra_node,missing_node | `{ROOM} {DEVICE}{J} 감지되면 {DEVICE}{J} 켜주고 {DUR}간 {DEVICE}{J} 없으면 꺼` | 욕실 움직임이 감지되면 베란다 조명을 켜주고 5분간 움직임이 없으면 꺼 |
| 2 | 12 | multiclause | extra_node,missing_node | `{DEVICE}{J} 감지되면 {DEVICE}{J} 켜주고 {DUR}간 {DEVICE}{J} 없으면 끄다` | 욕실 모션이 감지되면 안방 조명을 켜주고 5분간 움직임이 없으면 끄다 |
| 3 | 7 | multiclause | extra_node,missing_node | `{ROOM} {DEVICE}{J} 감지되면 {ROOM} {DEVICE}{J} 켜주고 {DUR}간 {DEVICE}{J} 없으면 꺼줘` | 욕실 움직임이 감지되면 현관 불을 켜주고 5분간 움직임이 없으면 꺼줘 |
| 4 | 7 | target | entity_confusion | `{ROOM} {DEVICE}{J} 감지되면 다 {DEVICE}{J} 꺼` | 안방 움직임이 감지되면 다 작은방 조명을 꺼 |
| 5 | 5 | mode | missing_node | `취침 모드이고 {ROOM} {DEVICE}{J} 감지되면 {DEVICE}{J} 틀어줘` | 취침 모드이고 거실 인기척이 감지되면 주방 조명을 틀어줘 |
| 6 | 5 | mode | missing_node | `취침모드이고 {ROOM} {DEVICE}{J} 작동하면 {DEVICE}{J} 꺼줘` | 취침모드이고 욕실 움직임이 작동하면 작은방 조명을 꺼줘 |
| 7 | 5 | mode | missing_node | `{SEG}{J} 취침 모드이고 {DEVICE}{J} 감지되면 {ROOM} {DEVICE}{J} 켜줘` | 저녁에 취침 모드이고 거실 모션이 감지되면 안방 불을 켜줘 |
| 8 | 5 | multiclause | missing_node | `{SEG}{J} 취침 모드이고 {ROOM} {DEVICE}{J} 작동하면 {ROOM} {DEVICE}{J} {PERCENT} 켜주고 {DUR} 동안 {DEVICE}{J} 없으면 꺼줘` | 아침에 취침 모드이고 안방 움직임이 작동하면 욕실 불을 30% 켜주고 3분 동안 모션이 없으면 꺼줘 |
| 9 | 5 | multiclause | entity_confusion | `{ROOM} {DEVICE}{J} 감지되면 {ROOM}등을 켜주고 {DUR} 동안 {DEVICE}{J} 없으면 {ROOM}등을 꺼주고 {DEVICE}{J} 켜지면 집의 모든 {DEVICE}{J} 끄다` | 거실 움직임이 감지되면 안방등을 켜주고 5분 동안 움직임이 없으면 안방등을 꺼주고 슬립모드가 켜지면 집의 모든 조명을 끄다 |
| 10 | 4 | mode | extra_node,missing_node | `취침모드가 꺼지면 {ROOM} {DEVICE}{J} 꺼주세요` | 취침모드가 꺼지면 욕실 불을 꺼주세요 |
| 11 | 4 | mode | missing_node | `취침 모드이고 {ROOM} {DEVICE}{J} 작동하면 {ROOM} {DEVICE}{J} 꺼줘` | 취침 모드이고 거실 움직임이 작동하면 거실 불을 꺼줘 |
| 12 | 4 | mode | missing_node | `취침 모드이고 {ROOM} {DEVICE}{J} 작동하면 {ROOM} {DEVICE}{J} 끄다` | 취침 모드이고 욕실 움직임이 작동하면 부엌 조명을 끄다 |
| 13 | 4 | mode | missing_node | `취침모드일 때 {ROOM} {DEVICE}{J} 감지되면 {ROOM} {DEVICE}{J} 켜` | 취침모드일 때 욕실 움직임이 감지되면 거실 조명을 켜 |
| 14 | 4 | mode | missing_node | `취침 모드가 해제되면 {ROOM} {DEVICE}{J} 틀어줘` | 취침 모드가 해제되면 거실 조명을 틀어줘 |
| 15 | 4 | mode | extra_node,missing_node | `{DEVICE}{J} 해제되면 {ROOM} {DEVICE}{J} 켜` | 슬립모드가 해제되면 안방 불을 켜 |

## 4. 하이브리드 권고
- 규칙 우선(현행 parser.py) → 낮은 confidence/미해결 시 pattern_library 템플릿 매처 → 최후 LLM few-shot (docs/HYBRID-PARSER.md 참조).
- 추가 후보 패턴은 `out/gap_library.yaml` 참조(총 340 클러스터).
- 커버 상태별 시드 템플릿은 `out/pattern_library.yaml`(하이브리드 데이터 자산).
