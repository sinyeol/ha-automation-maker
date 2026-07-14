// 모델 → 한국어 자연어 요약(백엔드 summarize와 유사, 저장 확인 모달용).

function nameOf(entityName, id) {
  if (!id) return '(엔티티)';
  try { return entityName(id) || id; } catch (_) { return id; }
}

function durText(d) {
  if (!d) return '';
  const parts = [];
  if (d.hours) parts.push(`${d.hours}시간`);
  if (d.minutes) parts.push(`${d.minutes}분`);
  if (d.seconds) parts.push(`${d.seconds}초`);
  return parts.join(' ');
}

function describeTrigger(t, en) {
  switch (t.type) {
    case 'state': {
      const n = nameOf(en, t.entity_id);
      if (t.to) return `${n}이(가) '${t.to}' 상태가 되면`;
      return `${n}의 상태가 바뀌면`;
    }
    case 'numeric_state': {
      const n = nameOf(en, t.entity_id);
      if (t.above != null && t.below != null) return `${n} 값이 ${t.above}~${t.below} 사이가 되면`;
      if (t.above != null) return `${n} 값이 ${t.above}을(를) 넘으면`;
      if (t.below != null) return `${n} 값이 ${t.below} 아래로 내려가면`;
      return `${n} 값이 바뀌면`;
    }
    case 'time': return `${t.at || '지정 시각'}이 되면`;
    case 'time_pattern': return '주기적으로';
    case 'sun': return t.event === 'sunrise' ? '해가 뜨면' : '해가 지면';
    case 'zone': {
      const n = nameOf(en, t.entity_id);
      const z = nameOf(en, t.zone);
      return t.event === 'leave' ? `${n}이(가) ${z}에서 나가면` : `${n}이(가) ${z}에 들어오면`;
    }
    case 'template': return '조건(템플릿)이 참이 되면';
    case 'homeassistant': return t.event === 'shutdown' ? 'HA가 종료될 때' : 'HA가 시작될 때';
    default: return '어떤 일이 생기면';
  }
}

// 조건 설명: 접속 어미 없이 명사형으로 끝나는 구절을 반환한다.
// (조립부에서 '이고, '/'이거나, '와 '일 때'를 붙인다)
function describeCondition(c, en) {
  switch (c.type) {
    case 'state': return `${nameOf(en, c.entity_id)}이(가) '${c.state}' 상태`;
    case 'numeric_state': {
      const n = nameOf(en, c.entity_id);
      if (c.above != null && c.below != null) return `${n} 값이 ${c.above}~${c.below} 사이`;
      if (c.above != null) return `${n} 값이 ${c.above} 초과`;
      if (c.below != null) return `${n} 값이 ${c.below} 미만`;
      return `${n} 값 조건 충족`;
    }
    case 'time': {
      const bits = [];
      if (c.after) bits.push(`${c.after} 이후`);
      if (c.before) bits.push(`${c.before} 이전`);
      if (c.weekday && c.weekday.length) bits.push(`${c.weekday.join('·')}요일`);
      return bits.join(' ') || '특정 시간대';
    }
    case 'sun': return '해 위치 조건 충족';
    case 'zone': return `${nameOf(en, c.entity_id)}이(가) ${nameOf(en, c.zone)}에 있는 상태`;
    case 'template': return '템플릿 조건 충족';
    case 'trigger': return `${c.id}번 트리거로 시작된 상태`;
    case 'and': return '(하위 조건 모두 참)';
    case 'or': return '(하위 조건 중 하나 참)';
    case 'not': return '(하위 조건이 거짓)';
    default: return '조건 충족';
  }
}

function describeAction(a, en) {
  switch (a.type) {
    case 'service': {
      const id = a.target && a.target.entity_id && a.target.entity_id[0];
      const n = id ? nameOf(en, id) : '기기';
      const act = a.action || '';
      let verb = act;
      if (act.endsWith('.turn_on')) verb = '켜기';
      else if (act.endsWith('.turn_off')) verb = '끄기';
      else if (act.endsWith('.toggle')) verb = '토글';
      else if (act.endsWith('.open_cover')) verb = '열기';
      else if (act.endsWith('.close_cover')) verb = '닫기';
      else if (act.endsWith('.lock')) verb = '잠그기';
      else if (act.endsWith('.unlock')) verb = '잠금 해제';
      return `${n} ${verb}`;
    }
    case 'delay': return `${durText(a.duration) || '잠시'} 기다리기`;
    case 'wait_template': return '조건 충족까지 대기';
    case 'wait_for_trigger': return '트리거 대기';
    case 'condition': return '조건 확인(안 맞으면 중단)';
    case 'choose': return '경우에 따라 분기';
    case 'if': return '조건에 따라 실행';
    case 'repeat': return '반복 실행';
    case 'parallel': return '동시 실행';
    case 'stop': return '중단';
    default: return '동작';
  }
}

export function summarizeModel(model, entityName) {
  const en = entityName || (id => id);
  const triggers = model.triggers || [];
  const conditions = model.conditions || [];
  const actions = model.actions || [];

  const sentences = [];

  if (triggers.length) {
    const joiner = triggers.length > 1 ? ' 또는 ' : '';
    const trigStr = triggers.map(t => describeTrigger(t, en)).join(joiner);
    let s = trigStr;
    if (conditions.length) {
      const sep = model.condition_mode === 'or' ? '이거나, ' : '이고, ';
      s += ', ' + conditions.map(c => describeCondition(c, en)).join(sep) + '일 때';
    }
    sentences.push(s + ',');
  }

  if (actions.length) {
    const txt = actions.map(a => describeAction(a, en)).join(', ');
    // '반복 실행' 등 '실행'으로 끝나면 '…를 실행해요' 중복을 피한다.
    sentences.push(txt.endsWith('실행') ? `${txt}해요.` : `${txt}${eulReul(txt)} 실행해요.`);
  } else {
    sentences.push('실행할 동작이 없어요.');
  }

  return sentences.join(' ').replace(/,\s*$/, '.');
}

// 받침 유무에 따라 을/를 선택 (비한글로 끝나면 병기).
function eulReul(word) {
  const code = word.charCodeAt(word.length - 1);
  if (code >= 0xac00 && code <= 0xd7a3) return (code - 0xac00) % 28 > 0 ? '을' : '를';
  return '을(를)';
}
