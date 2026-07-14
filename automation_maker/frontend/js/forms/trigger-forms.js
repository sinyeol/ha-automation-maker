// 트리거 타입별 폼 렌더/기본값.
import { el, field, textInput, numberInput, selectField } from '../app.js';
import { store } from '../store.js';
import { entityField } from '../components/entity-picker.js';
import { createDurationInput, optionalDurationField } from '../components/duration-input.js';

// 구역(zone.*) 선택 필드. zone은 일반 엔티티 인벤토리에 없으므로
// bootstrap의 zones 목록으로 셀렉트를 만들고, 목록이 없으면 직접 입력 폴백.
export function zoneField(node, key, onChange) {
  const zones = store.zones;
  if (!zones.length) {
    return textInput(node[key], v => { node[key] = v; }, { placeholder: '예: zone.home' });
  }
  const options = [{ value: '', label: '구역 선택…' }]
    .concat(zones.map(z => ({ value: z.entity_id, label: z.name })));
  return selectField(node[key] || '', options, v => { node[key] = v; if (onChange) onChange(); });
}

export const TRIGGER_TYPES = [
  { type: 'state', label: '상태가 바뀌면', icon: '🔀' },
  { type: 'numeric_state', label: '값이 기준을 넘으면', icon: '📈' },
  { type: 'time', label: '특정 시각이 되면', icon: '⏰' },
  { type: 'time_pattern', label: '주기적으로', icon: '🔁' },
  { type: 'sun', label: '해가 뜨거나 질 때', icon: '🌅' },
  { type: 'zone', label: '구역을 드나들면', icon: '📍' },
  { type: 'template', label: '템플릿(고급)', icon: '🧩' },
  { type: 'homeassistant', label: 'HA 시작/종료', icon: '⚙️' },
];

const LABELS = {};
TRIGGER_TYPES.forEach(t => { LABELS[t.type] = t.label; });

export function triggerLabel(node) {
  return LABELS[node.type] || node.type;
}

export function defaultTrigger(type) {
  switch (type) {
    case 'state': return { type: 'state', entity_id: '' };
    case 'numeric_state': return { type: 'numeric_state', entity_id: '', above: null, below: null };
    case 'time': return { type: 'time', at: '07:00' };
    case 'time_pattern': return { type: 'time_pattern', minutes: '/5' };
    case 'sun': return { type: 'sun', event: 'sunset' };
    case 'zone': return { type: 'zone', entity_id: '', zone: '', event: 'enter' };
    case 'template': return { type: 'template', value_template: '' };
    case 'homeassistant': return { type: 'homeassistant', event: 'start' };
    default: return { type: 'state', entity_id: '' };
  }
}

export function renderTriggerForm(node, ctx) {
  switch (node.type) {
    case 'state': return stateForm(node, ctx);
    case 'numeric_state': return numericForm(node, ctx);
    case 'time': return timeForm(node, ctx);
    case 'time_pattern': return patternForm(node, ctx);
    case 'sun': return sunForm(node, ctx);
    case 'zone': return zoneForm(node, ctx);
    case 'template': return templateForm(node, ctx);
    case 'homeassistant': return haForm(node, ctx);
    default: return el('div', {}, '알 수 없는 트리거');
  }
}

function stateForm(node, ctx) {
  return el('div', { class: 'form' },
    field('엔티티', entityField(node, 'entity_id', () => true, ctx.onChange)),
    field('이전 상태 (선택)', textInput(node.from, v => node.from = v || undefined, { placeholder: '예: off' })),
    field('바뀐 상태 (선택)', textInput(node.to, v => node.to = v || undefined, { placeholder: '예: on' })),
    field('지속 조건', optionalDurationField(node, 'for', '이 상태가 유지되면', ctx.onChange)));
}

function numericForm(node, ctx) {
  return el('div', { class: 'form' },
    field('엔티티', entityField(node, 'entity_id', e => e.domain === 'sensor' || e.domain === 'number' || e.domain === 'input_number', ctx.onChange)),
    field('이 값 초과 (above)', numberInput(node.above, v => node.above = v)),
    field('이 값 미만 (below)', numberInput(node.below, v => node.below = v)),
    field('지속 조건', optionalDurationField(node, 'for', '이 상태가 유지되면', ctx.onChange)),
    el('p', { class: 'form-hint' }, 'above / below 중 하나 이상 입력하세요.'));
}

function timeForm(node) {
  return el('div', { class: 'form' },
    field('시각', el('input', {
      class: 'input', type: 'time', step: '1', value: node.at || '07:00',
      onInput: e => node.at = e.target.value,
    })));
}

function patternForm(node) {
  return el('div', { class: 'form' },
    el('p', { class: 'form-hint' }, '"/5" = 5마다, "0" = 정각. 비우면 무시.'),
    field('시', textInput(node.hours, v => node.hours = v || undefined, { placeholder: '예: /2' })),
    field('분', textInput(node.minutes, v => node.minutes = v || undefined, { placeholder: '예: /5' })),
    field('초', textInput(node.seconds, v => node.seconds = v || undefined, { placeholder: '예: 0' })));
}

function sunForm(node) {
  return el('div', { class: 'form' },
    field('이벤트', selectField(node.event, [
      { value: 'sunrise', label: '일출' }, { value: 'sunset', label: '일몰' },
    ], v => node.event = v)),
    field('오프셋 (± HH:MM:SS, 선택)', textInput(node.offset, v => node.offset = v || undefined, { placeholder: '예: -00:45:00' })));
}

function zoneForm(node, ctx) {
  return el('div', { class: 'form' },
    field('사람', entityField(node, 'entity_id', e => e.domain === 'person' || e.domain === 'device_tracker', ctx.onChange)),
    field('구역', zoneField(node, 'zone')),
    field('동작', selectField(node.event, [
      { value: 'enter', label: '들어오면' }, { value: 'leave', label: '나가면' },
    ], v => node.event = v)));
}

function templateForm(node, ctx) {
  return el('div', { class: 'form' },
    field('템플릿', el('textarea', {
      class: 'input textarea', rows: '2', placeholder: '{{ ... }}',
      onInput: e => node.value_template = e.target.value,
    }, node.value_template || '')),
    field('지속 조건', optionalDurationField(node, 'for', '참으로 유지되면', ctx.onChange)));
}

function haForm(node) {
  return el('div', { class: 'form' },
    field('시점', selectField(node.event, [
      { value: 'start', label: 'HA 시작 시' }, { value: 'shutdown', label: 'HA 종료 시' },
    ], v => node.event = v)));
}
