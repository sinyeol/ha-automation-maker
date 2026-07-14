// 조건 타입별 폼 렌더/기본값.
import { el, field, textInput, numberInput, selectField } from '../app.js';
import { entityField } from '../components/entity-picker.js';
import { optionalDurationField } from '../components/duration-input.js';
import { zoneField } from './trigger-forms.js';

export const CONDITION_TYPES = [
  { type: 'state', label: '상태가 특정 값이면', icon: '🔀' },
  { type: 'numeric_state', label: '값이 범위 안이면', icon: '📈' },
  { type: 'time', label: '시간대/요일이면', icon: '🕐' },
  { type: 'sun', label: '해 위치 기준', icon: '🌅' },
  { type: 'zone', label: '사람이 구역 안이면', icon: '📍' },
  { type: 'template', label: '템플릿(고급)', icon: '🧩' },
  { type: 'trigger', label: '이 트리거로 실행됐으면', icon: '🎯' },
  { type: 'and', label: '모두 참(AND)', icon: '➕' },
  { type: 'or', label: '하나라도 참(OR)', icon: '⚡' },
  { type: 'not', label: '반대(NOT)', icon: '🚫' },
];

const LABELS = {};
CONDITION_TYPES.forEach(t => { LABELS[t.type] = t.label; });

export function conditionLabel(node) {
  return LABELS[node.type] || node.type;
}

export function defaultCondition(type) {
  switch (type) {
    case 'state': return { type: 'state', entity_id: '', state: '' };
    case 'numeric_state': return { type: 'numeric_state', entity_id: '', above: null, below: null };
    case 'time': return { type: 'time', weekday: [] };
    case 'sun': return { type: 'sun' };
    case 'zone': return { type: 'zone', entity_id: '', zone: '' };
    case 'template': return { type: 'template', value_template: '' };
    case 'trigger': return { type: 'trigger', id: '0' };
    case 'and': return { type: 'and', conditions: [] };
    case 'or': return { type: 'or', conditions: [] };
    case 'not': return { type: 'not', conditions: [] };
    default: return { type: 'state', entity_id: '', state: '' };
  }
}

const WEEKDAYS = [
  ['mon', '월'], ['tue', '화'], ['wed', '수'], ['thu', '목'],
  ['fri', '금'], ['sat', '토'], ['sun', '일'],
];

export function renderConditionForm(node, ctx) {
  switch (node.type) {
    case 'state': return stateForm(node, ctx);
    case 'numeric_state': return numericForm(node, ctx);
    case 'time': return timeForm(node, ctx);
    case 'sun': return sunForm(node, ctx);
    case 'zone': return zoneForm(node, ctx);
    case 'template': return templateForm(node, ctx);
    case 'trigger': return triggerForm(node, ctx);
    case 'and': case 'or': case 'not': return groupForm(node, ctx);
    default: return el('div', {}, '알 수 없는 조건');
  }
}

function stateForm(node, ctx) {
  return el('div', { class: 'form' },
    field('엔티티', entityField(node, 'entity_id', () => true, ctx.onChange)),
    field('상태 값', textInput(node.state, v => node.state = v, { placeholder: '예: on' })),
    field('지속 조건', optionalDurationField(node, 'for', '이 상태가 유지되면', ctx.onChange)));
}

function numericForm(node, ctx) {
  return el('div', { class: 'form' },
    field('엔티티', entityField(node, 'entity_id', e => e.domain === 'sensor' || e.domain === 'number' || e.domain === 'input_number', ctx.onChange)),
    field('이 값 초과 (above)', numberInput(node.above, v => node.above = v)),
    field('이 값 미만 (below)', numberInput(node.below, v => node.below = v)));
}

function timeForm(node) {
  const days = el('div', { class: 'weekday-row' });
  node.weekday = node.weekday || [];
  for (const [code, ko] of WEEKDAYS) {
    const active = node.weekday.includes(code);
    const btn = el('button', {
      class: 'weekday-btn' + (active ? ' active' : ''),
      onClick: () => {
        const i = node.weekday.indexOf(code);
        if (i >= 0) node.weekday.splice(i, 1);
        else node.weekday.push(code);
        btn.classList.toggle('active');
      },
    }, ko);
    days.appendChild(btn);
  }
  return el('div', { class: 'form' },
    field('이 시각 이후 (선택)', el('input', { class: 'input', type: 'time', step: '1', value: node.after || '', onInput: e => node.after = e.target.value || undefined })),
    field('이 시각 이전 (선택)', el('input', { class: 'input', type: 'time', step: '1', value: node.before || '', onInput: e => node.before = e.target.value || undefined })),
    field('요일 (선택)', days));
}

function sunForm(node) {
  const opts = [{ value: '', label: '무시' }, { value: 'sunrise', label: '일출' }, { value: 'sunset', label: '일몰' }];
  return el('div', { class: 'form' },
    field('이 시점 이후', selectField(node.after || '', opts, v => node.after = v || undefined)),
    field('이후 오프셋 (선택)', textInput(node.after_offset, v => node.after_offset = v || undefined, { placeholder: '예: -00:30:00' })),
    field('이 시점 이전', selectField(node.before || '', opts, v => node.before = v || undefined)),
    field('이전 오프셋 (선택)', textInput(node.before_offset, v => node.before_offset = v || undefined, { placeholder: '예: +00:30:00' })));
}

function zoneForm(node, ctx) {
  return el('div', { class: 'form' },
    field('사람', entityField(node, 'entity_id', e => e.domain === 'person' || e.domain === 'device_tracker', ctx.onChange)),
    field('구역', zoneField(node, 'zone')));
}

function templateForm(node) {
  return el('div', { class: 'form' },
    field('템플릿', el('textarea', {
      class: 'input textarea', rows: '2', placeholder: '{{ ... }}',
      onInput: e => node.value_template = e.target.value,
    }, node.value_template || '')));
}

function triggerForm(node) {
  return el('div', { class: 'form' },
    field('트리거 번호', textInput(node.id, v => node.id = v, { placeholder: '0 = 첫 번째 트리거' })),
    el('p', { class: 'form-hint' }, '위 트리거 목록의 순서(0부터)를 적어요.'));
}

function groupForm(node, ctx) {
  node.conditions = node.conditions || [];
  return el('div', { class: 'form nested' },
    ctx.subConditions(node.conditions, ctx.onChange));
}
