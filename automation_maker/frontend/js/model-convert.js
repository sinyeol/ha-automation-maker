// HA 자동화 config → UI 모델 역변환. 신문법/구문법 모두 파싱.
// 반환: { model, warnings: [한국어 문구] }. 역변환 불가 요소는 경고로 남기고 건너뛴다.

function asList(value) {
  if (value == null) return [];
  return Array.isArray(value) ? value : [value];
}

// "HH:MM:SS" 또는 {hours,minutes,seconds} → Duration 객체
function parseDuration(value) {
  if (value == null) return null;
  if (typeof value === 'object') {
    return {
      hours: Number(value.hours) || 0,
      minutes: Number(value.minutes) || 0,
      seconds: Number(value.seconds) || 0,
    };
  }
  if (typeof value === 'number') {
    return { hours: 0, minutes: 0, seconds: value };
  }
  const m = String(value).split(':').map(x => parseInt(x, 10));
  if (m.length === 3) return { hours: m[0] || 0, minutes: m[1] || 0, seconds: m[2] || 0 };
  // HA cv.time_period_str 은 "A:B" 를 시:분 으로 해석한다(분:초 아님).
  if (m.length === 2) return { hours: m[0] || 0, minutes: m[1] || 0, seconds: 0 };
  if (m.length === 1 && !isNaN(m[0])) return { hours: 0, minutes: 0, seconds: m[0] };
  return null;
}

function convertTrigger(t, warnings) {
  const kind = t.trigger || t.platform;
  if (t.id != null) {
    warnings.push(`트리거 id '${t.id}'는 이 편집기에서 지원되지 않아 저장 시 제거돼요. trigger 조건이 이 id를 참조하면 동작이 깨질 수 있어요.`);
  }
  switch (kind) {
    case 'state': {
      const n = { type: 'state', entity_id: firstId(t.entity_id, warnings, '트리거의 엔티티') };
      if (t.attribute != null) {
        warnings.push(`state 트리거의 attribute('${t.attribute}')는 이 편집기에서 지원되지 않아 저장 시 제거돼요.`);
      }
      if (t.from != null) n.from = String(t.from);
      if (t.to != null) n.to = String(t.to);
      if (t.for != null) n.for = parseDuration(t.for);
      return n;
    }
    case 'numeric_state': {
      const n = { type: 'numeric_state', entity_id: firstId(t.entity_id, warnings, '트리거의 엔티티') };
      if (t.above != null) n.above = Number(t.above);
      if (t.below != null) n.below = Number(t.below);
      if (t.for != null) n.for = parseDuration(t.for);
      return n;
    }
    case 'time': {
      if (Array.isArray(t.at) && t.at.length > 1) {
        warnings.push(`트리거의 시각 여러 개 중 첫 번째만 불러왔어요: ${t.at.join(', ')} → ${t.at[0]}`);
      }
      const at = Array.isArray(t.at) ? t.at[0] : t.at;
      return { type: 'time', at: String(at || '00:00') };
    }
    case 'time_pattern': {
      const n = { type: 'time_pattern' };
      if (t.hours != null) n.hours = String(t.hours);
      if (t.minutes != null) n.minutes = String(t.minutes);
      if (t.seconds != null) n.seconds = String(t.seconds);
      return n;
    }
    case 'sun': {
      const n = { type: 'sun', event: t.event || 'sunset' };
      if (t.offset != null) n.offset = String(t.offset);
      return n;
    }
    case 'zone': {
      return { type: 'zone', entity_id: firstId(t.entity_id, warnings, '트리거의 엔티티'), zone: firstId(t.zone), event: t.event || 'enter' };
    }
    case 'template': {
      const n = { type: 'template', value_template: t.value_template || '' };
      if (t.for != null) n.for = parseDuration(t.for);
      return n;
    }
    case 'homeassistant':
      return { type: 'homeassistant', event: t.event || 'start' };
    default:
      warnings.push(`편집기에서 지원하지 않는 트리거(${kind || '알 수 없음'})는 표시하지 않았어요.`);
      return null;
  }
}

function firstId(value, warnings, label) {
  if (Array.isArray(value)) {
    if (value.length > 1 && warnings && label) {
      warnings.push(`${label} 여러 개 중 첫 번째만 불러왔어요: ${value.join(', ')} → ${value[0]}`);
    }
    return value[0] || '';
  }
  return value == null ? '' : String(value);
}

function convertCondition(c, warnings) {
  const kind = c.condition;
  switch (kind) {
    case 'state': {
      const n = { type: 'state', entity_id: firstId(c.entity_id, warnings, '조건의 엔티티'), state: firstId(c.state, warnings, '조건의 상태 값') };
      if (c.attribute != null) {
        warnings.push(`state 조건의 attribute('${c.attribute}')는 이 편집기에서 지원되지 않아 저장 시 제거돼요.`);
      }
      if (c.for != null) n.for = parseDuration(c.for);
      return n;
    }
    case 'numeric_state': {
      const n = { type: 'numeric_state', entity_id: firstId(c.entity_id, warnings, '조건의 엔티티') };
      if (c.above != null) n.above = Number(c.above);
      if (c.below != null) n.below = Number(c.below);
      return n;
    }
    case 'time': {
      const n = { type: 'time' };
      if (c.after != null) n.after = String(c.after);
      if (c.before != null) n.before = String(c.before);
      if (c.weekday != null) n.weekday = asList(c.weekday);
      return n;
    }
    case 'sun': {
      const n = { type: 'sun' };
      if (c.after != null) n.after = String(c.after);
      if (c.before != null) n.before = String(c.before);
      if (c.after_offset != null) n.after_offset = String(c.after_offset);
      if (c.before_offset != null) n.before_offset = String(c.before_offset);
      return n;
    }
    case 'zone':
      return { type: 'zone', entity_id: firstId(c.entity_id, warnings, '조건의 엔티티'), zone: firstId(c.zone) };
    case 'template':
      return { type: 'template', value_template: c.value_template || '' };
    case 'trigger':
      return { type: 'trigger', id: String(firstId(c.id)) };
    case 'and': case 'or': case 'not':
      return { type: kind, conditions: asList(c.conditions).map(x => convertCondition(x, warnings)).filter(Boolean) };
    default:
      warnings.push(`편집기에서 지원하지 않는 조건(${kind || '알 수 없음'})은 표시하지 않았어요.`);
      return null;
  }
}

function convertAction(a, warnings) {
  // 서비스 호출: 신문법 action / 구문법 service
  const svc = a.action || a.service;
  if (svc && typeof svc === 'string' && svc.includes('.') && !('choose' in a) && !('repeat' in a)) {
    const node = { type: 'service', action: svc, target: {}, data: {} };
    const target = a.target || {};
    const entIds = asList(target.entity_id != null ? target.entity_id : a.entity_id);
    if (entIds.length) node.target.entity_id = entIds;
    if (target.area_id != null) node.target.area_id = asList(target.area_id);
    if (target.device_id != null) node.target.device_id = asList(target.device_id);
    node.data = Object.assign({}, a.data || {});
    return node;
  }
  if ('delay' in a) {
    return { type: 'delay', duration: parseDuration(a.delay) || { hours: 0, minutes: 0, seconds: 0 } };
  }
  if ('wait_template' in a) {
    const n = { type: 'wait_template', wait_template: a.wait_template };
    if (a.timeout != null) n.timeout = parseDuration(a.timeout);
    if (a.continue_on_timeout != null) n.continue_on_timeout = !!a.continue_on_timeout;
    return n;
  }
  if ('wait_for_trigger' in a) {
    const n = { type: 'wait_for_trigger', triggers: asList(a.wait_for_trigger).map(t => convertTrigger(t, warnings)).filter(Boolean) };
    if (a.timeout != null) n.timeout = parseDuration(a.timeout);
    if (a.continue_on_timeout != null) n.continue_on_timeout = !!a.continue_on_timeout;
    return n;
  }
  if ('condition' in a && typeof a.condition === 'object') {
    return { type: 'condition', condition: convertCondition(a.condition, warnings) };
  }
  if ('condition' in a && typeof a.condition === 'string') {
    // 축약형 게이트: 액션 dict 자체가 조건 정의.
    const c = convertCondition(a, warnings);
    if (c) return { type: 'condition', condition: c };
  }
  if ('choose' in a) {
    const options = asList(a.choose).map(opt => ({
      conditions: asList(opt.conditions).map(c => convertCondition(c, warnings)).filter(Boolean),
      sequence: asList(opt.sequence).map(x => convertAction(x, warnings)).filter(Boolean),
    }));
    const node = { type: 'choose', options };
    if (a.default != null) node.default = asList(a.default).map(x => convertAction(x, warnings)).filter(Boolean);
    return node;
  }
  if ('if' in a) {
    const node = {
      type: 'if',
      if: asList(a.if).map(c => convertCondition(c, warnings)).filter(Boolean),
      then: asList(a.then).map(x => convertAction(x, warnings)).filter(Boolean),
    };
    if (a.else != null) node.else = asList(a.else).map(x => convertAction(x, warnings)).filter(Boolean);
    return node;
  }
  if ('repeat' in a) {
    const r = a.repeat || {};
    const node = { type: 'repeat', sequence: asList(r.sequence).map(x => convertAction(x, warnings)).filter(Boolean) };
    if (r.count != null) { node.kind = 'count'; node.count = Number(r.count); }
    else if (r.while != null) { node.kind = 'while'; node.conditions = asList(r.while).map(c => convertCondition(c, warnings)).filter(Boolean); }
    else if (r.until != null) { node.kind = 'until'; node.conditions = asList(r.until).map(c => convertCondition(c, warnings)).filter(Boolean); }
    else { node.kind = 'count'; node.count = 1; }
    return node;
  }
  if ('parallel' in a) {
    const branches = asList(a.parallel).map(b => {
      const seq = (b && b.sequence != null) ? asList(b.sequence) : asList(b);
      return seq.map(x => convertAction(x, warnings)).filter(Boolean);
    });
    return { type: 'parallel', branches };
  }
  if ('stop' in a) {
    return { type: 'stop', message: String(a.stop || '') };
  }
  warnings.push('편집기에서 지원하지 않는 액션이 있어 표시하지 않았어요.');
  return null;
}

export function haConfigToModel(config) {
  const warnings = [];
  config = config || {};

  if (config && typeof config === 'object' && !Array.isArray(config)) {
    ['variables', 'trigger_variables', 'initial_state'].forEach(key => {
      if (key in config) {
        warnings.push(`최상위 '${key}' 설정은 이 편집기에서 지원되지 않아 저장 시 제거돼요.`);
      }
    });
  }

  const triggers = asList(config.triggers != null ? config.triggers : config.trigger)
    .map(t => convertTrigger(t, warnings)).filter(Boolean);

  let rawConditions = asList(config.conditions != null ? config.conditions : config.condition);
  let conditionMode = 'and';
  // 최상위가 단일 or 래퍼면 풀어준다.
  if (rawConditions.length === 1 && rawConditions[0] && rawConditions[0].condition === 'or' &&
      Array.isArray(rawConditions[0].conditions)) {
    conditionMode = 'or';
    rawConditions = rawConditions[0].conditions;
  }
  const conditions = rawConditions.map(c => convertCondition(c, warnings)).filter(Boolean);

  const actions = asList(config.actions != null ? config.actions : config.action)
    .map(a => convertAction(a, warnings)).filter(Boolean);

  const model = {
    alias: config.alias || '',
    description: config.description || '',
    mode: config.mode || 'single',
    triggers,
    condition_mode: conditionMode,
    conditions,
    actions,
  };
  if (config.max != null) model.max = config.max;

  return { model, warnings };
}
