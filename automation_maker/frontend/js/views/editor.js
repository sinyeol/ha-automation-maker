// 카드 스택 편집기 + 저장 플로우. 트리거/조건/액션을 재귀적 listEditor로 편집.
import { el, textInput, selectField } from '../app.js';
import { store } from '../store.js';
import { get, post, put } from '../api.js';
import { openModal } from '../components/modal.js';
import { showToast } from '../components/toast.js';
import { summarizeModel } from '../nl-summary.js';
import { haConfigToModel } from '../model-convert.js';
import { TRIGGER_TYPES, defaultTrigger, renderTriggerForm, triggerLabel } from '../forms/trigger-forms.js';
import { CONDITION_TYPES, defaultCondition, renderConditionForm, conditionLabel } from '../forms/condition-forms.js';
import {
  ACTION_BASIC_TYPES, ACTION_ADVANCED_TYPES, defaultAction, renderActionForm, actionLabel,
} from '../forms/action-forms.js';

function emptyModel() {
  return { alias: '', description: '', mode: 'single', triggers: [], condition_mode: 'and', conditions: [], actions: [] };
}

const MODE_OPTIONS = [
  { value: 'single', label: '한 번에 하나만 (single)', desc: '실행 중이면 새 실행을 무시해요.' },
  { value: 'restart', label: '다시 시작 (restart)', desc: '진행 중이어도 처음부터 다시 실행해요.' },
  { value: 'queued', label: '대기열 (queued)', desc: '요청을 순서대로 하나씩 처리해요.' },
  { value: 'parallel', label: '병렬 (parallel)', desc: '동시에 여러 개를 실행해요.' },
];

const clone = (node) => JSON.parse(JSON.stringify(node));
const swap = (list, i, j) => { const t = list[i]; list[i] = list[j]; list[j] = t; };

// preview 요약이 entity_id 원문 대신 사람이 읽는 이름을 쓰도록 매핑을 만든다.
// (엔티티 이름 + zone 이름 병합)
function previewEntityNames() {
  const names = store.entityNameMap();
  for (const z of store.zones) names[z.entity_id] = z.name;
  return names;
}

// 편집 진입: config를 불러와 모델로 역변환.
export async function renderEditorForEdit(root, id) {
  root.textContent = '';
  root.appendChild(el('div', { class: 'loading' }, '불러오는 중…'));
  try {
    const res = await get(`automations/${encodeURIComponent(id)}`);
    const { model, warnings } = haConfigToModel(res.config || {});
    renderEditorView(root, { id, model, warnings });
  } catch (err) {
    root.textContent = '';
    root.appendChild(el('div', { class: 'error-box' },
      el('p', {}, '자동화를 불러오지 못했어요.'),
      el('button', { class: 'btn', onClick: () => { location.hash = '#/list'; } }, '목록으로')));
  }
}

export function renderEditorView(root, { id = null, model = null, warnings = [] } = {}) {
  const state = { id, model: model || emptyModel() };
  root.textContent = '';

  const ctx = {
    onChange: () => rebuild(),
    subConditions: (list) => listEditor(list, 'condition'),
    subActions: (list) => listEditor(list, 'action'),
    subTriggers: (list) => listEditor(list, 'trigger'),
  };

  const KIND = {
    trigger: { types: TRIGGER_TYPES, def: defaultTrigger, form: renderTriggerForm, label: triggerLabel, add: '+ 트리거 추가', empty: '트리거를 추가하세요.' },
    condition: { types: CONDITION_TYPES, def: defaultCondition, form: renderConditionForm, label: conditionLabel, add: '+ 조건 추가', empty: '조건 없이도 괜찮아요.' },
    action: { types: null, def: defaultAction, form: renderActionForm, label: actionLabel, add: '+ 동작 추가', empty: '실행할 동작을 추가하세요.' },
  };

  function iconBtn(sym, title, onClick, extra) {
    return el('button', { class: 'icon-btn' + (extra ? ' ' + extra : ''), title, onClick }, sym);
  }

  function itemCard(label, formEl, h) {
    return el('div', { class: 'item-card' },
      el('div', { class: 'item-head' },
        el('span', { class: 'item-label' }, label),
        el('div', { class: 'item-actions' },
          h.onDup && iconBtn('⧉', '복제', h.onDup),
          h.onUp && iconBtn('↑', '위로', h.onUp),
          h.onDown && iconBtn('↓', '아래로', h.onDown),
          iconBtn('✕', '삭제', h.onDel, 'danger'))),
      el('div', { class: 'item-body' }, formEl));
  }

  function listEditor(list, kind) {
    const cfg = KIND[kind];
    const items = el('div', { class: 'items' });
    if (!list.length) {
      items.appendChild(el('div', { class: 'empty-hint' }, cfg.empty));
    }
    list.forEach((node, i) => {
      const form = cfg.form(node, ctx);
      items.appendChild(itemCard(cfg.label(node), form, {
        onDup: () => { list.splice(i + 1, 0, clone(node)); ctx.onChange(); },
        onUp: i > 0 ? () => { swap(list, i, i - 1); ctx.onChange(); } : null,
        onDown: i < list.length - 1 ? () => { swap(list, i, i + 1); ctx.onChange(); } : null,
        onDel: () => { list.splice(i, 1); ctx.onChange(); },
      }));
    });

    const container = el('div', { class: 'list-editor' }, items);
    if (kind === 'action') {
      container.appendChild(el('button', {
        class: 'add-btn accent',
        onClick: () => { list.push(defaultAction('delay')); ctx.onChange(); },
      }, '+ 딜레이 추가'));
    }
    container.appendChild(el('button', {
      class: 'add-btn',
      onClick: () => openAddModal(kind, type => { list.push(cfg.def(type)); ctx.onChange(); }),
    }, cfg.add));
    return container;
  }

  function typeGrid(types, onPick) {
    const grid = el('div', { class: 'type-grid' });
    for (const t of types) {
      grid.appendChild(el('button', {
        class: 'type-card', onClick: () => onPick(t.type),
      }, el('span', { class: 'type-ic' }, t.icon || '•'), el('span', {}, t.label)));
    }
    return grid;
  }

  function openAddModal(kind, onPick) {
    let handle;
    const choose = (type) => { handle.close(); onPick(type); };
    let body;
    if (kind === 'action') {
      const basic = typeGrid(ACTION_BASIC_TYPES, choose);
      const adv = typeGrid(ACTION_ADVANCED_TYPES, choose);
      adv.style.display = 'none';
      const tBasic = el('button', { class: 'tab-btn active', onClick: () => { tBasic.classList.add('active'); tAdv.classList.remove('active'); basic.style.display = ''; adv.style.display = 'none'; } }, '기본');
      const tAdv = el('button', { class: 'tab-btn', onClick: () => { tAdv.classList.add('active'); tBasic.classList.remove('active'); basic.style.display = 'none'; adv.style.display = ''; } }, '고급');
      body = el('div', { class: 'type-picker' }, el('div', { class: 'tab-row' }, tBasic, tAdv), basic, adv);
    } else {
      body = el('div', { class: 'type-picker' }, typeGrid(KIND[kind].types, choose));
    }
    handle = openModal({ title: kind === 'trigger' ? '트리거 종류' : kind === 'condition' ? '조건 종류' : '동작 종류', body, size: 'sheet' });
  }

  // --- 카드들 ---
  function triggerCard() {
    const body = listEditor(state.model.triggers, 'trigger');
    const note = state.model.triggers.length >= 2
      ? el('p', { class: 'or-note' }, '이 중 하나라도 일어나면 실행돼요.')
      : null;
    return sectionCard('언제? (~할 때)', body, note);
  }

  function conditionCard() {
    const seg = el('div', { class: 'segment' },
      segBtn('모든 조건 (AND)', state.model.condition_mode === 'and', () => setMode('and')),
      segBtn('하나라도 (OR)', state.model.condition_mode === 'or', () => setMode('or')));
    function setMode(m) { state.model.condition_mode = m; ctx.onChange(); }
    function segBtn(text, active, onClick) {
      return el('button', { class: 'segment-btn' + (active ? ' active' : ''), onClick }, text);
    }
    const body = listEditor(state.model.conditions, 'condition');
    const head = state.model.conditions.length ? seg : null;
    return sectionCard('조건 (~인 동안, 선택)', body, head);
  }

  function actionCard() {
    const body = listEditor(state.model.actions, 'action');
    return sectionCard('실행', body, null);
  }

  function sectionCard(title, body, extra) {
    return el('section', { class: 'section-card' },
      el('h3', { class: 'section-title' }, title),
      extra,
      body);
  }

  // --- YAML 미리보기 ---
  async function showYaml() {
    try {
      const res = await post('preview', { model: state.model, entity_names: previewEntityNames() });
      const body = el('div', { class: 'yaml-modal' });
      if (res.errors && res.errors.length) body.appendChild(errorList(res.errors));
      if (res.summary) body.appendChild(el('p', { class: 'yaml-summary' }, res.summary));
      body.appendChild(el('pre', { class: 'yaml-pre' }, res.yaml || '(유효하지 않아 변환 결과가 없어요.)'));
      openModal({ title: 'YAML 미리보기', body, size: 'sheet' });
    } catch (_) { /* 토스트 처리됨 */ }
  }

  function errorList(errors) {
    return el('div', { class: 'error-list' },
      el('p', { class: 'error-list-title' }, '확인이 필요한 항목이 있어요:'),
      el('ul', {}, ...errors.map(e => el('li', {}, `${e.path ? e.path + ': ' : ''}${e.message}`))));
  }

  // --- 저장 확인 모달 ---
  function openSave() {
    const aliasInput = textInput(state.model.alias, () => {}, { placeholder: '자동화 이름 (필수)' });
    const modeDesc = el('p', { class: 'mode-desc' }, MODE_OPTIONS.find(m => m.value === state.model.mode).desc);
    const modeSel = selectField(state.model.mode, MODE_OPTIONS.map(m => ({ value: m.value, label: m.label })),
      v => { state.model.mode = v; modeDesc.textContent = MODE_OPTIONS.find(m => m.value === v).desc; });

    const summary = summarizeModel(state.model, id => store.entityName(id));
    const body = el('div', { class: 'save-modal' },
      el('div', { class: 'save-summary' }, summary),
      el('label', { class: 'field-label' }, '이름'),
      aliasInput,
      el('label', { class: 'field-label' }, '실행 모드'),
      modeSel, modeDesc);

    const warn = safetyWarning(state.model);
    if (warn) body.appendChild(el('div', { class: 'safety-warn' }, '⚠️ ' + warn));

    const errBox = el('div', {});
    body.appendChild(errBox);

    let handle;
    const cancel = el('button', { class: 'btn', onClick: () => handle.close() }, '취소');
    const confirm = el('button', { class: 'btn btn-primary', onClick: doSave }, id ? '수정 저장' : '만들기');
    handle = openModal({ title: '저장', body, footer: [cancel, confirm], size: 'sheet' });

    async function doSave() {
      const alias = aliasInput.value.trim();
      if (!alias) { showToast('이름을 입력해주세요.', 'error'); aliasInput.focus(); return; }
      state.model.alias = alias;
      if (state.model.mode === 'queued' || state.model.mode === 'parallel') {
        if (state.model.max == null) state.model.max = 10;
      } else {
        delete state.model.max;
      }
      confirm.disabled = true;
      try {
        // 검증 먼저(미리보기)
        const pv = await post('preview', { model: state.model, entity_names: previewEntityNames() });
        if (pv.errors && pv.errors.length) {
          errBox.textContent = '';
          errBox.appendChild(errorList(pv.errors));
          confirm.disabled = false;
          return;
        }
        if (state.id) await put(`automations/${encodeURIComponent(state.id)}`, { model: state.model });
        else await post('automations', { model: state.model });
        handle.close();
        showToast('저장했어요.', 'info');
        location.hash = '#/list';
      } catch (_) {
        confirm.disabled = false;
      }
    }
  }

  // --- 렌더 골격 ---
  const cardsWrap = el('div', { class: 'editor-cards' });
  function rebuild() {
    cardsWrap.textContent = '';
    cardsWrap.append(triggerCard(), conditionCard(), actionCard());
  }

  const header = el('div', { class: 'page-head' },
    el('button', { class: 'btn small', onClick: () => { location.hash = '#/list'; } }, '← 목록'),
    el('h2', { class: 'page-title' }, id ? '자동화 편집' : '새 자동화'));

  const banner = warnings && warnings.length
    ? el('div', { class: 'warn-banner' },
      el('strong', {}, '일부 항목을 그대로 불러오지 못했어요.'),
      el('ul', {}, ...warnings.map(w => el('li', {}, w))))
    : null;

  const bottomBar = el('div', { class: 'bottom-bar' },
    el('button', { class: 'btn', onClick: showYaml }, 'YAML 미리보기'),
    el('button', { class: 'btn btn-primary', onClick: openSave }, '저장'));

  rebuild();
  root.append(header);
  if (banner) root.append(banner);
  root.append(cardsWrap, bottomBar);
}

// 안전장치(가스/밸브/잠금) 관련 액션 경고.
function safetyWarning(model) {
  const svcs = [];
  collectServiceActions(model.actions, svcs);
  for (const a of svcs) {
    const act = a.action || '';
    const domain = act.split('.')[0];
    if (domain === 'lock' || act.includes('lock')) return '문 잠금/해제 동작이 포함돼 있어요. 저장 후 실제로 작동하니 주의하세요.';
    if (domain === 'valve') return '가스밸브 등 안전 관련 기기 제어가 포함돼 있어요. 잘못 동작하면 위험할 수 있으니 확인하세요.';
    // 다중 엔티티 타깃과 area 타깃을 검사하되, area 엔티티는 액션 도메인과
    // 같은 도메인만 본다(예: light 액션이면 같은 방의 가스밸브(switch)는 오탐이므로 제외).
    const ids = (a.target && a.target.entity_id) || [];
    let ents = ids.map(id => store.getEntity(id)).filter(Boolean);
    for (const areaId of (a.target && a.target.area_id) || []) {
      ents = ents.concat(store.entitiesInArea(areaId).filter(e => e.entity_id.split('.')[0] === domain));
    }
    if (ents.some(e => e.category === 'safety' || (e.name || '').includes('가스'))) {
      return '가스밸브 등 안전 관련 기기 제어가 포함돼 있어요. 잘못 동작하면 위험할 수 있으니 확인하세요.';
    }
  }
  return null;
}

function collectServiceActions(actions, out) {
  for (const a of actions || []) {
    if (!a) continue;
    if (a.type === 'service') out.push(a);
    else if (a.type === 'choose') {
      for (const o of a.options || []) collectServiceActions(o.sequence, out);
      collectServiceActions(a.default, out);
    } else if (a.type === 'if') {
      collectServiceActions(a.then, out);
      collectServiceActions(a.else, out);
    } else if (a.type === 'repeat') {
      collectServiceActions(a.sequence, out);
    } else if (a.type === 'parallel') {
      for (const b of a.branches || []) collectServiceActions(b, out);
    }
  }
}
