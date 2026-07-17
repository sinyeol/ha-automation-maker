# 커버리지 리포트 (현재 파서 기준)

- 코퍼스 총 1597문장 (평가 1597 · gold_invalid 제외 0)
- exact 653 · partial 504 · fail 440 · 전체 정확률 40.9%

### 영역별

| 항목 | exact | partial | fail | 정확률(exact) |
|---|---:|---:|---:|---:|
| climate-set | 0 | 33 | 7 | 0.0% |
| cover | 52 | 0 | 0 | 100.0% |
| media | 40 | 34 | 26 | 40.0% |
| misparse | 0 | 3 | 11 | 0.0% |
| mode | 46 | 157 | 157 | 12.8% |
| multiclause | 97 | 123 | 92 | 31.1% |
| numeric | 115 | 21 | 24 | 71.9% |
| safety | 17 | 0 | 41 | 29.3% |
| scope-exclude | 0 | 8 | 20 | 0.0% |
| single | 16 | 2 | 4 | 72.7% |
| target | 237 | 104 | 8 | 67.9% |
| time | 2 | 3 | 9 | 14.3% |
| time-range | 31 | 9 | 40 | 38.8% |
| value | 0 | 7 | 1 | 0.0% |

### 템플릿별

| 항목 | exact | partial | fail | 정확률(exact) |
|---|---:|---:|---:|---:|
| climateset_temp_target | 0 | 33 | 7 | 0.0% |
| cover_motion_open | 40 | 0 | 0 | 100.0% |
| cover_seg_close | 6 | 0 | 0 | 100.0% |
| cover_seg_open | 6 | 0 | 0 | 100.0% |
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
| media_daily_off | 12 | 8 | 0 | 60.0% |
| media_mode_off | 8 | 6 | 26 | 20.0% |
| media_motion_on | 20 | 20 | 0 | 50.0% |
| mode_cond_motion_off | 7 | 35 | 1 | 16.3% |
| mode_cond_motion_on | 5 | 39 | 1 | 11.1% |
| mode_cond_whenclause | 6 | 37 | 0 | 14.0% |
| mode_qm_release_off | 0 | 0 | 43 | 0.0% |
| mode_seg_cond_motion_on | 9 | 26 | 9 | 20.5% |
| mode_trig_off_light | 9 | 4 | 31 | 20.5% |
| mode_trig_on_light | 7 | 1 | 38 | 15.2% |
| mode_trig_scope_off | 3 | 11 | 29 | 7.0% |
| multiclause_dim_off | 20 | 22 | 2 | 45.5% |
| multiclause_golden_b | 7 | 33 | 6 | 15.2% |
| multiclause_mode_cond_onoff | 13 | 29 | 2 | 29.5% |
| multiclause_plain_onoff | 32 | 10 | 3 | 71.1% |
| multiclause_qm_5bungan | 0 | 0 | 43 | 0.0% |
| multiclause_qm_comma | 0 | 0 | 3 | 0.0% |
| multiclause_seg_dim_off | 20 | 20 | 4 | 45.5% |
| multiclause_three_rules | 5 | 9 | 29 | 11.6% |
| numeric_humidity_erv_on | 19 | 21 | 0 | 47.5% |
| numeric_season_temp_ac | 32 | 0 | 8 | 80.0% |
| numeric_temp_ac_on | 33 | 0 | 7 | 82.5% |
| numeric_temp_boiler_on | 31 | 0 | 9 | 77.5% |
| safety_door_lock | 6 | 0 | 0 | 100.0% |
| safety_mode_lock | 11 | 0 | 29 | 27.5% |
| safety_moisture_valve_off | 0 | 0 | 12 | 0.0% |
| scopeexclude_mode_light_off | 0 | 8 | 20 | 0.0% |
| single_golden_1_bathroom_fan_off | 1 | 1 | 1 | 33.3% |
| single_golden_2_arrival_two_persons | 1 | 1 | 1 | 33.3% |
| single_golden_3_numeric_ac_on | 2 | 0 | 1 | 66.7% |
| single_golden_4_loop_basic | 3 | 0 | 0 | 100.0% |
| single_golden_5_segment_brightness | 3 | 0 | 0 | 100.0% |
| single_golden_6_bedroom_loop | 2 | 0 | 0 | 100.0% |
| single_golden_7_bathroom_loop | 2 | 0 | 0 | 100.0% |
| single_golden_8_segment_trigger | 2 | 0 | 1 | 66.7% |
| target_basic_off | 32 | 11 | 1 | 72.7% |
| target_basic_on | 38 | 5 | 3 | 82.6% |
| target_climate_on | 43 | 0 | 0 | 100.0% |
| target_fan_on | 44 | 0 | 0 | 100.0% |
| target_percent | 26 | 18 | 1 | 57.8% |
| target_qm_moodlight | 0 | 31 | 0 | 0.0% |
| target_scope_all | 20 | 23 | 0 | 46.5% |
| target_two_actions | 34 | 9 | 1 | 77.3% |
| timerange_daily_light_off | 31 | 9 | 0 | 77.5% |
| timerange_daytype_seg_light | 0 | 0 | 40 | 0.0% |

### 소스별 (grammar/paraphrase 분리, §7.7)

| 항목 | exact | partial | fail | 정확률(exact) |
|---|---:|---:|---:|---:|
| grammar | 609 | 463 | 363 | 42.4% |
| paraphrase | 44 | 41 | 77 | 27.2% |

> §7.7 낙관편향: grammar 문장은 템플릿과 동형이라 커버리지가 낙관적이다. 실제 일반화 성능은 **paraphrase 소스 정확률**을 우선 본다.
