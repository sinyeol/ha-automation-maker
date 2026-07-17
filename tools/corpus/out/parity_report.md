# 앱 L1 정직 측정 + A/B 패리티 (parity_check)

- held-out n = 520
- **앱 L1 (backend.nl.parser.parse) exact = 65.6% (341/520)**
- 오버레이 L1A (parse_patched, 목표 74.2%) exact = 71.0% (369/520)
- 갭(오버레이 − 앱) = +5.4%p

## 금지문 안전 게이트 (special:prohibition)
- 전수 23문장 · **forbidden 액션 방출(misfire) = 0/23** · ok=True = 0

## 카테고리별 (dataset)

| 그룹 | 앱 L1 | 오버레이 L1A | 갭 | n |
|---|---:|---:|---:|---:|
| heldout | 67.8% (316/466) | 72.3% (337/466) | +4.5%p | 466 |
| para_hard | 46.3% (25/54) | 59.3% (32/54) | +13.0%p | 54 |

## 카테고리별 (difficulty)

| 그룹 | 앱 L1 | 오버레이 L1A | 갭 | n |
|---|---:|---:|---:|---:|
| ? | 46.3% (25/54) | 59.3% (32/54) | +13.0%p | 54 |
| easy | 83.8% (119/142) | 87.3% (124/142) | +3.5%p | 142 |
| hard | 54.0% (75/139) | 56.1% (78/139) | +2.2%p | 139 |
| medium | 65.9% (122/185) | 73.0% (135/185) | +7.0%p | 185 |

## 카테고리별 (tag)

| 그룹 | 앱 L1 | 오버레이 L1A | 갭 | n |
|---|---:|---:|---:|---:|
| ? | 65.4% (134/205) | 78.0% (160/205) | +12.7%p | 205 |
| action_params | 70.8% (17/24) | 87.5% (21/24) | +16.7%p | 24 |
| aspect_condition | 81.8% (18/22) | 86.4% (19/22) | +4.5%p | 22 |
| duration_revert | 63.6% (14/22) | 72.7% (16/22) | +9.1%p | 22 |
| else_branch | 25.0% (6/24) | 25.0% (6/24) | +0.0%p | 24 |
| interval_repeat | 47.8% (11/23) | 17.4% (4/23) | -30.4%p | 23 |
| negation_exception | 45.8% (11/24) | 45.8% (11/24) | +0.0%p | 24 |
| numeric_edge_between | 79.2% (19/24) | 83.3% (20/24) | +4.2%p | 24 |
| postposed_clause | 60.0% (15/25) | 64.0% (16/25) | +4.0%p | 25 |
| presence_quantifier | 76.0% (19/25) | 80.0% (20/25) | +4.0%p | 25 |
| quoted_message | 72.0% (18/25) | 72.0% (18/25) | +0.0%p | 25 |
| sun_offset | 86.4% (19/22) | 86.4% (19/22) | +0.0%p | 22 |
| surface_variation | 64.0% (16/25) | 60.0% (15/25) | -4.0%p | 25 |
| weather_external | 60.0% (6/10) | 60.0% (6/10) | +0.0%p | 10 |
| weekday_calendar | 90.0% (18/20) | 90.0% (18/20) | +0.0%p | 20 |

## 패리티 diff (앱 L1 ≠ 오버레이 L1A): 46문장
- 앱 L1 exact 인데 오버레이 non-exact: ['ho90_interval_repeat_08~p126.0', 'ho90_interval_repeat_09~p127.0', 'ho90_interval_repeat_10~p128.0', 'ho90_interval_repeat_11~p129.0', 'ho90_interval_repeat_13~p131.0', 'ho90_interval_repeat_15~p133.0', 'ho90_interval_repeat_22~p140.0', 'ho90_surface_variation_10~p317.0', 'ho90_postposed_clause_15~p347.0']
