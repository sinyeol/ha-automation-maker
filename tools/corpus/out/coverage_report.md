# 커버리지 리포트 (현재 파서 기준)

- 코퍼스 총 1079문장 (평가 1079 · gold_invalid 제외 0)
- exact 520 · partial 313 · fail 246 · 전체 정확률 48.2%

### 영역별

| 항목 | exact | partial | fail | 정확률(exact) |
|---|---:|---:|---:|---:|
| misparse | 0 | 3 | 11 | 0.0% |
| mode | 113 | 120 | 127 | 31.4% |
| multiclause | 148 | 78 | 86 | 47.4% |
| single | 16 | 2 | 4 | 72.7% |
| target | 241 | 100 | 8 | 69.1% |
| time | 2 | 3 | 9 | 14.3% |
| value | 0 | 7 | 1 | 0.0% |

### 템플릿별

| 항목 | exact | partial | fail | 정확률(exact) |
|---|---:|---:|---:|---:|
| hard_alt_endings_door | 0 | 0 | 3 | 0.0% |
| hard_comma_delay_off | 0 | 2 | 0 | 0.0% |
| hard_comma_held_off | 0 | 1 | 0 | 0.0% |
| hard_comma_two_actions | 0 | 0 | 1 | 0.0% |
| hard_duration_gan_bath | 0 | 0 | 1 | 0.0% |
| hard_duration_gan_living | 0 | 0 | 2 | 0.0% |
| hard_duration_hour_half | 0 | 0 | 2 | 0.0% |
| hard_held_door_open | 0 | 0 | 1 | 0.0% |
| hard_mode_cancel_action | 0 | 2 | 0 | 0.0% |
| hard_mode_copula_condition | 0 | 0 | 2 | 0.0% |
| hard_mode_copula_trigger_ambiguous | 0 | 0 | 1 | 0.0% |
| hard_mode_release_action | 0 | 2 | 0 | 0.0% |
| hard_mode_unlock_trigger | 0 | 0 | 2 | 0.0% |
| hard_stem_cold | 0 | 0 | 1 | 0.0% |
| hard_stem_dusty | 0 | 0 | 1 | 0.0% |
| hard_stem_hot | 0 | 0 | 2 | 0.0% |
| hard_stem_humid | 0 | 0 | 1 | 0.0% |
| hard_stem_not_moving | 0 | 0 | 1 | 0.0% |
| hard_stem_not_moving_held | 0 | 0 | 1 | 0.0% |
| hard_target_all_off | 0 | 3 | 0 | 0.0% |
| hard_target_ceiling | 0 | 1 | 0 | 0.0% |
| hard_target_except_living | 0 | 1 | 1 | 0.0% |
| hard_target_kidsroom | 0 | 1 | 0 | 0.0% |
| hard_target_mood_evening | 0 | 1 | 0 | 0.0% |
| hard_target_seonpunggi | 0 | 0 | 1 | 0.0% |
| hard_time_mwf | 0 | 1 | 1 | 0.0% |
| hard_time_range | 0 | 2 | 1 | 0.0% |
| hard_time_weekday_tv | 1 | 0 | 0 | 100.0% |
| hard_time_weekend | 1 | 0 | 1 | 50.0% |
| hard_value_ac_settemp | 0 | 1 | 0 | 0.0% |
| hard_value_bare_number | 0 | 1 | 0 | 0.0% |
| hard_value_boiler_settemp | 0 | 1 | 0 | 0.0% |
| hard_value_half | 0 | 2 | 0 | 0.0% |
| hard_value_max | 0 | 2 | 0 | 0.0% |
| hard_value_negative | 0 | 0 | 1 | 0.0% |
| mode_cond_motion_off | 15 | 27 | 1 | 34.9% |
| mode_cond_motion_on | 20 | 24 | 1 | 44.4% |
| mode_cond_whenclause | 19 | 24 | 0 | 44.2% |
| mode_qm_release_off | 0 | 0 | 43 | 0.0% |
| mode_seg_cond_motion_on | 7 | 24 | 13 | 15.9% |
| mode_trig_off_light | 20 | 1 | 23 | 45.5% |
| mode_trig_on_light | 23 | 3 | 20 | 50.0% |
| mode_trig_scope_off | 9 | 13 | 21 | 20.9% |
| multiclause_dim_off | 39 | 3 | 2 | 88.6% |
| multiclause_golden_b | 20 | 20 | 6 | 43.5% |
| multiclause_mode_cond_onoff | 18 | 24 | 2 | 40.9% |
| multiclause_plain_onoff | 35 | 7 | 3 | 77.8% |
| multiclause_qm_5bungan | 0 | 0 | 43 | 0.0% |
| multiclause_qm_comma | 0 | 0 | 3 | 0.0% |
| multiclause_seg_dim_off | 36 | 4 | 4 | 81.8% |
| multiclause_three_rules | 0 | 20 | 23 | 0.0% |
| single_golden_1_bathroom_fan_off | 1 | 1 | 1 | 33.3% |
| single_golden_2_arrival_two_persons | 1 | 1 | 1 | 33.3% |
| single_golden_3_numeric_ac_on | 2 | 0 | 1 | 66.7% |
| single_golden_4_loop_basic | 3 | 0 | 0 | 100.0% |
| single_golden_5_segment_brightness | 3 | 0 | 0 | 100.0% |
| single_golden_6_bedroom_loop | 2 | 0 | 0 | 100.0% |
| single_golden_7_bathroom_loop | 2 | 0 | 0 | 100.0% |
| single_golden_8_segment_trigger | 2 | 0 | 1 | 66.7% |
| target_basic_off | 40 | 3 | 1 | 90.9% |
| target_basic_on | 38 | 5 | 3 | 82.6% |
| target_climate_on | 43 | 0 | 0 | 100.0% |
| target_fan_on | 44 | 0 | 0 | 100.0% |
| target_percent | 40 | 4 | 1 | 88.9% |
| target_qm_moodlight | 0 | 31 | 0 | 0.0% |
| target_scope_all | 0 | 43 | 0 | 0.0% |
| target_two_actions | 36 | 7 | 1 | 81.8% |

### 소스별 (grammar/paraphrase 분리, §7.7)

| 항목 | exact | partial | fail | 정확률(exact) |
|---|---:|---:|---:|---:|
| grammar | 479 | 269 | 169 | 52.2% |
| paraphrase | 41 | 44 | 77 | 25.3% |

> §7.7 낙관편향: grammar 문장은 템플릿과 동형이라 커버리지가 낙관적이다. 실제 일반화 성능은 **paraphrase 소스 정확률**을 우선 본다.
