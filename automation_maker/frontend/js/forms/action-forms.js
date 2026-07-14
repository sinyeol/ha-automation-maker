// 액션 타입별 폼 렌더/기본값. 서비스 폼은 bootstrap의 services(KNOWN_SERVICES 미러)를 사용.
import { el, field, textInput, numberInput, selectField, checkboxField } from '../app.js';
import { store } from '../store.js';
import { entityField } from '../components/entity-picker.js';
import { createDurationInput, optionalDurationField } from '../components/duration-input.js';
import { renderConditionForm, defaultCondition, CONDITION_TYPES } from './condition-forms.js';

export const ACTION_BASIC_TYPES = [
  { type: 'service', label: '기기 제어', icon: '🎛️' },
  { type: 'delay', label: '잠시 기다리기', icon: '⏳' },
];

export const ACTION_ADVANCED_TYPES = [
  { type: 'choose', label: '경우에 따라(choose)', icon: '🔀' },
  { type: 'if', label: '만약 ~라면(if)', icon: '❓' },
  { type: 'repeat', label: '반복(repeat)', icon: '🔁' },
  { type: 'parallel', label: '동시 실행(parallel)', icon: '⛓️' },
  { type: 'wait_template', label: '조건 대기(template)', icon: '⏱️' },
  { type: 'wait_for_trigger', label: '트리거 대기', icon: '🎯' },
  { type: 'condition', label: '중간 조건 게이트', icon: '🚧' },
  { type: 'stop', label: '중단(stop)', icon: '🛑' },
];

const LABELS = {};
[...ACTION_BASIC_TYPES, ...ACTION_ADVANCED_TYPES].forEach(t => { LABELS[t.type] = t.label; });

export function actionLabel(node) {
  if (node.type === 'service' && node.action) {
    return `기기 제어 · ${node.action}`;
  }
  return LABELS[node.type] || node.type;
}

export function defaultAction(type) {
  switch (type) {
    case 'service': return { type: 'service', action: '', target: { entity_id: [] }, data: {} };
    case 'delay': return { type: 'delay', duration: { hours: 0, minutes: 5, seconds: 0 } };
    case 'wait_template': return { type: 'wait_template', wait_template: '' };
    case 'wait_for_trigger': return { type: 'wait_for_trigger', triggers: [] };
    case 'condition': return { type: 'condition', condition: defaultCondition('state') };
    case 'choose': return { type: 'choose', options: [{ conditions: [], sequence: [] }] };
    case 'if': return { type: 'if', if: [], then: [] };
    case 'repeat': return { type: 'repeat', kind: 'count', count: 2, sequence: [] };
    case 'parallel': return { type: 'parallel', branches: [[]] };
    case 'stop': return { type: 'stop', message: '' };
    default: return { type: 'delay', duration: { hours: 0, minutes: 5, seconds: 0 } };
  }
}

export function renderActionForm(node, ctx) {
  switch (node.type) {
    case 'service': return serviceForm(node, ctx);
    case 'delay': return delayForm(node, ctx);
    case 'wait_template': return waitTemplateForm(node, ctx);
    case 'wait_for_trigger': return waitTriggerForm(node, ctx);
    case 'condition': return conditionGateForm(node, ctx);
    case 'choose': return chooseForm(node, ctx);
    case 'if': return ifForm(node, ctx);
    case 'repeat': return repeatForm(node, ctx);
    case 'parallel': return parallelForm(node, ctx);
    case 'stop': return stopForm(node, ctx);
    default: return el('div', {}, '알 수 없는 액션');
  }
}

// --- 서비스(기기 제어) ---
function serviceForm(node, ctx) {
  node.target = node.target || { entity_id: [] };
  node.data = node.data || {};
  const entityId = (node.target.entity_id && node.target.entity_id[0]) || '';
  const domain = entityId ? entityId.split('.')[0] : '';

  const entityProxy = { entity_id: entityId };
  const entityBtn = entityField(entityProxy, 'entity_id', () => true, () => {
    node.target.entity_id = entityProxy.entity_id ? [entityProxy.entity_id] : [];
    const newDomain = entityProxy.entity_id.split('.')[0];
    const services = store.servicesFor(newDomain);
    node.action = services.length ? `${newDomain}.${services[0]}` : '';
    node.data = {};
    ctx.onChange();
  });

  const parts = [field('대상 엔티티', entityBtn)];

  if (domain) {
    const services = store.servicesFor(domain);
    if (services.length) {
      parts.push(field('동작', selectField(node.action, services.map(s => ({
        value: `${domain}.${s}`, label: s,
      })), v => { node.action = v; node.data = {}; ctx.onChange(); })));
    } else {
      parts.push(field('동작(서비스)', textInput(node.action, v => node.action = v, { placeholder: `${domain}.turn_on` })));
    }
    const paramEl = serviceParams(node);
    if (paramEl) parts.push(paramEl);
  }

  return el('div', { class: 'form' }, ...parts);
}

function serviceParams(node) {
  const action = node.action || '';
  node.data = node.data || {};
  if (action === 'light.turn_on' || action === 'light.toggle') {
    return sliderField('밝기 (%)', node.data.brightness_pct, 0, 100, 1, v => setData(node, 'brightness_pct', v));
  }
  if (action === 'climate.set_temperature') {
    return field('설정 온도 (°C)', numberInput(node.data.temperature, v => setData(node, 'temperature', v), { min: '5', max: '35', step: '0.5' }));
  }
  if (action === 'media_player.volume_set') {
    return sliderField('볼륨', node.data.volume_level, 0, 1, 0.05, v => setData(node, 'volume_level', v));
  }
  if (action === 'fan.set_percentage') {
    return sliderField('풍량 (%)', node.data.percentage, 0, 100, 1, v => setData(node, 'percentage', v));
  }
  if (action === 'cover.set_cover_position') {
    return sliderField('위치 (%)', node.data.position, 0, 100, 1, v => setData(node, 'position', v));
  }
  return null;
}

function setData(node, key, value) {
  if (value == null || value === '') delete node.data[key];
  else node.data[key] = value;
}

function sliderField(labelText, value, min, max, step, onChange) {
  const val = value == null ? '' : value;
  const out = el('span', { class: 'slider-val' }, val === '' ? '—' : String(val));
  const slider = el('input', {
    class: 'slider', type: 'range', min: String(min), max: String(max), step: String(step),
    value: val === '' ? String(min) : String(val),
    onInput: e => { const n = Number(e.target.value); out.textContent = String(n); onChange(n); },
  });
  return field(labelText, el('div', { class: 'slider-row' }, slider, out));
}

// --- delay ---
function delayForm(node) {
  node.duration = node.duration || { hours: 0, minutes: 5, seconds: 0 };
  return el('div', { class: 'form' }, field('기다리는 시간', createDurationInput(node.duration)));
}

// --- wait_template ---
function waitTemplateForm(node, ctx) {
  return el('div', { class: 'form' },
    field('대기 템플릿', el('textarea', {
      class: 'input textarea', rows: '2', placeholder: '{{ is_state(...) }}',
      onInput: e => node.wait_template = e.target.value,
    }, node.wait_template || '')),
    field('제한 시간', optionalDurationField(node, 'timeout', '제한 시간 두기', ctx.onChange)),
    checkboxField('제한 시간 지나도 계속 진행', node.continue_on_timeout, v => node.continue_on_timeout = v));
}

// --- wait_for_trigger ---
function waitTriggerForm(node, ctx) {
  node.triggers = node.triggers || [];
  return el('div', { class: 'form nested' },
    field('기다릴 트리거', ctx.subTriggers(node.triggers, ctx.onChange)),
    field('제한 시간', optionalDurationField(node, 'timeout', '제한 시간 두기', ctx.onChange)),
    checkboxField('제한 시간 지나도 계속 진행', node.continue_on_timeout, v => node.continue_on_timeout = v));
}

// --- 중간 조건 게이트 ---
function conditionGateForm(node, ctx) {
  node.condition = node.condition || defaultCondition('state');
  const typeSel = selectField(node.condition.type, CONDITION_TYPES
    .filter(t => !['and', 'or', 'not'].includes(t.type))
    .map(t => ({ value: t.type, label: t.label })),
    v => { node.condition = defaultCondition(v); ctx.onChange(); });
  return el('div', { class: 'form nested' },
    field('조건 종류', typeSel),
    renderConditionForm(node.condition, ctx));
}

// --- choose ---
function chooseForm(node, ctx) {
  node.options = node.options || [];
  const blocks = node.options.map((opt, i) => {
    opt.conditions = opt.conditions || [];
    opt.sequence = opt.sequence || [];
    return el('div', { class: 'branch-block' },
      el('div', { class: 'branch-head' },
        el('span', {}, `옵션 ${i + 1}`),
        el('button', { class: 'icon-btn danger', title: '옵션 삭제', onClick: () => { node.options.splice(i, 1); ctx.onChange(); } }, '✕')),
      el('div', { class: 'branch-label' }, '이 조건이면'),
      ctx.subConditions(opt.conditions, ctx.onChange),
      el('div', { class: 'branch-label' }, '이것을 실행'),
      ctx.subActions(opt.sequence, ctx.onChange));
  });
  const addOpt = el('button', { class: 'add-btn ghost', onClick: () => { node.options.push({ conditions: [], sequence: [] }); ctx.onChange(); } }, '+ 옵션 추가');

  const hasDefault = Array.isArray(node.default);
  const defToggle = checkboxField('어느 조건에도 안 맞을 때(default)', hasDefault, v => {
    if (v) node.default = [];
    else delete node.default;
    ctx.onChange();
  });
  const defBlock = hasDefault ? ctx.subActions(node.default, ctx.onChange) : null;

  return el('div', { class: 'form nested' }, ...blocks, addOpt, defToggle, defBlock);
}

// --- if / then / else ---
function ifForm(node, ctx) {
  node.if = node.if || [];
  node.then = node.then || [];
  const hasElse = Array.isArray(node.else);
  const elseToggle = checkboxField('아니면(else) 실행', hasElse, v => {
    if (v) node.else = [];
    else delete node.else;
    ctx.onChange();
  });
  return el('div', { class: 'form nested' },
    el('div', { class: 'branch-label' }, '만약 이 조건이면'),
    ctx.subConditions(node.if, ctx.onChange),
    el('div', { class: 'branch-label' }, '이것을 실행'),
    ctx.subActions(node.then, ctx.onChange),
    elseToggle,
    hasElse ? ctx.subActions(node.else, ctx.onChange) : null);
}

// --- repeat ---
function repeatForm(node, ctx) {
  node.sequence = node.sequence || [];
  const parts = [
    field('반복 방식', selectField(node.kind, [
      { value: 'count', label: '횟수만큼' }, { value: 'while', label: '조건이 참인 동안' }, { value: 'until', label: '조건이 참이 될 때까지' },
    ], v => {
      node.kind = v;
      // count 로 전환 시 화면 기본값(2)을 모델에도 커밋해야 저장이 통과한다.
      if (v === 'count') { if (node.count == null) node.count = 2; }
      else { delete node.count; }
      ctx.onChange();
    })),
  ];
  if (node.kind === 'count') {
    parts.push(field('반복 횟수', numberInput(node.count == null ? 2 : node.count, v => node.count = v, { min: '1', step: '1' })));
  } else {
    node.conditions = node.conditions || [];
    parts.push(el('div', { class: 'branch-label' }, '조건'));
    parts.push(ctx.subConditions(node.conditions, ctx.onChange));
  }
  parts.push(el('div', { class: 'branch-label' }, '반복할 동작'));
  parts.push(ctx.subActions(node.sequence, ctx.onChange));
  return el('div', { class: 'form nested' }, ...parts);
}

// --- parallel ---
function parallelForm(node, ctx) {
  node.branches = node.branches || [[]];
  const blocks = node.branches.map((branch, i) =>
    el('div', { class: 'branch-block' },
      el('div', { class: 'branch-head' },
        el('span', {}, `갈래 ${i + 1}`),
        node.branches.length > 1 ? el('button', { class: 'icon-btn danger', title: '갈래 삭제', onClick: () => { node.branches.splice(i, 1); ctx.onChange(); } }, '✕') : null),
      ctx.subActions(branch, ctx.onChange)));
  const addBranch = el('button', { class: 'add-btn ghost', onClick: () => { node.branches.push([]); ctx.onChange(); } }, '+ 갈래 추가');
  return el('div', { class: 'form nested' }, ...blocks, addBranch);
}

// --- stop ---
function stopForm(node) {
  return el('div', { class: 'form' },
    field('중단 사유(메모)', textInput(node.message, v => node.message = v, { placeholder: '예: 조건 불충족' })));
}
