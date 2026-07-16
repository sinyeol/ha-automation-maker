// 수동 단어 매핑 에디터(SPEC-V3 §3.3): 문장을 토큰 칩으로 쪼개 각 토큰에 역할·대상을
// 직접 지정한다. 토큰 탭 → 바텀시트(역할 셀렉트 + 역할별 컨트롤 + "여기서 규칙 나눔").
// 초안 채우기(tokenize의 suggestions 적용) · 실시간 요약(build) · [루틴으로 저장].
import { el, field, selectField, numberInput, checkboxField } from '../app.js';
import { store } from '../store.js';
import { openModal } from './modal.js';
import { openEntityPicker } from './entity-picker.js';
import { tokenizeSentence, buildFromTokens, getModes, getSettings } from '../api2.js';

// 역할 taxonomy — 백엔드 build 계약(§3.2)과 1:1. label 은 한국어(§3.3).
const ROLES = [
  { key: 'ignore', label: '무시' },
  { key: 'trigger_entity', label: '트리거 대상' },
  { key: 'condition_entity', label: '조건 대상' },
  { key: 'action_target', label: '액션 대상' },
  { key: 'event_state', label: '상태값' },
  { key: 'numeric', label: '수치 조건' },
  { key: 'duration', label: '지속시간' },
  { key: 'segment', label: '시간대' },
  { key: 'mode_ref', label: '모드' },
  { key: 'daytype', label: '요일' },
  { key: 'season', label: '계절' },
  { key: 'value', label: '값(밝기·온도)' },
  { key: 'action_verb', label: '동작' },
  { key: 'boundary', label: '규칙 나눔' },
];
const ROLE_LABEL = Object.fromEntries(ROLES.map(r => [r.key, r.label]));

const SEGMENT_OPTS = [
  { value: 'dawn', label: '새벽' }, { value: 'morning', label: '아침' },
  { value: 'day', label: '낮' }, { value: 'evening', label: '저녁' },
  { value: 'night', label: '밤' },
];
const DAYTYPE_OPTS = [
  { value: 'weekday', label: '평일' }, { value: 'weekend', label: '주말' },
  { value: 'holiday', label: '공휴일' },
];
const SEASON_OPTS = [
  { value: 'spring', label: '봄' }, { value: 'summer', label: '여름' },
  { value: 'autumn', label: '가을' }, { value: 'winter', label: '겨울' },
];
const VERB_OPTS = [
  { value: 'on', label: '켜기' }, { value: 'off', label: '끄기' },
  { value: 'toggle', label: '토글' }, { value: 'open', label: '열기' },
  { value: 'close', label: '닫기' }, { value: 'set_mode', label: '모드 설정' },
];
const CMP_OPTS = [
  { value: 'above', label: '이상(초과)' }, { value: 'below', label: '이하(미만)' },
];
const KIND_OPTS = [
  { value: 'brightness', label: '밝기(%)' }, { value: 'temperature', label: '온도(℃)' },
];
const DUR_UNITS = [
  { value: 1, label: '초' }, { value: 60, label: '분' }, { value: 3600, label: '시간' },
];

const labelOf = (opts, v) => (opts.find(o => o.value === v) || {}).label || v;

function entName(id) {
  if (!id) return '(대상 없음)';
  return store.entityName(id);
}

function durLabel(sec) {
  const s = Number(sec) || 0;
  if (s % 3600 === 0 && s >= 3600) return (s / 3600) + '시간';
  if (s % 60 === 0 && s >= 60) return (s / 60) + '분';
  return s + '초';
}

function stateWord(st) { return st === 'off' ? '꺼짐' : '켜짐'; }

// 칩에 보여줄 역할 요약(한 줄).
function roleDetail(a) {
  switch (a.role) {
    case 'trigger_entity': return '트리거 · ' + entName(a.ref);
    case 'condition_entity': return '조건 · ' + entName(a.ref);
    case 'action_target': return '대상 · ' + entName(a.ref);
    case 'event_state': return '상태 ' + stateWord(a.state);
    case 'numeric': return '수치 ' + (a.cmp === 'below' ? '≤ ' : '≥ ') + (a.value ?? '');
    case 'duration': return durLabel(a.value);
    case 'segment': return '시간대 ' + labelOf(SEGMENT_OPTS, a.ref);
    case 'mode_ref': return '모드 ' + (a.ref || '') + ' ' + stateWord(a.state);
    case 'daytype': return labelOf(DAYTYPE_OPTS, a.ref);
    case 'season': return labelOf(SEASON_OPTS, a.ref);
    case 'value': return (a.kind === 'temperature' ? '온도 ' : '밝기 ') + (a.value ?? '');
    case 'action_verb':
      return a.verb === 'set_mode'
        ? '모드설정 ' + (a.ref || '') + ' ' + stateWord(a.state)
        : '동작 · ' + labelOf(VERB_OPTS, a.verb);
    case 'boundary': return '규칙 나눔';
    default: return '';
  }
}

// 역할 → 칩 색상 그룹.
function roleGroup(role) {
  if (role === 'trigger_entity') return 'trigger';
  if (role === 'condition_entity' || role === 'segment' || role === 'mode_ref'
    || role === 'daytype' || role === 'season' || role === 'numeric'
    || role === 'event_state' || role === 'duration') return 'condition';
  if (role === 'action_target' || role === 'action_verb') return 'target';
  if (role === 'value') return 'value';
  return 'ignore';
}

// 엔티티 선택 버튼(피커 재사용).
function entityPickBtn(current, onPick) {
  const btn = el('button', {
    class: 'entity-select' + (current ? '' : ' empty'),
    onClick: async () => {
      const id = await openEntityPicker({ title: '엔티티 선택' });
      if (id) { onPick(id); btn.textContent = store.entityName(id); btn.classList.remove('empty'); }
    },
  }, current ? store.entityName(current) : '엔티티 선택');
  return btn;
}

// on/off 2단 세그먼트.
function onOffSeg(value, onChange) {
  const seg = el('div', { class: 'segment mm-onoff' });
  for (const o of [{ v: 'on', l: '켜짐' }, { v: 'off', l: '꺼짐' }]) {
    const b = el('button', {
      class: 'segment-btn' + (o.v === value ? ' active' : ''),
      onClick: () => {
        value = o.v;
        for (const bb of seg.children) bb.classList.toggle('active', bb === b);
        onChange(o.v);
      },
    }, o.l);
    seg.appendChild(b);
  }
  return seg;
}

// 모드 이름 셀렉트(설정 모드 + 현재 값 합집합).
function modeSelect(current, modeNames, onChange) {
  const names = [...new Set([...(modeNames || []), current].filter(Boolean))];
  if (!names.length) {
    return el('div', { class: 'form-hint' }, '설정에서 모드를 먼저 추가하세요.');
  }
  const opts = names.map(n => ({ value: n, label: n }));
  return selectField(current || names[0], opts, onChange);
}

// createManualMap({ sentence, onSave }) → HTMLElement
export function createManualMap({ sentence, onSave }) {
  let tokens = [];
  let assignByIndex = new Map();   // index → assignment
  let suggestions = [];            // tokenize 응답 캐시(초안 채우기용)
  let modeNames = [];
  let lastBuild = null;
  let buildTimer = null;

  const root = el('div', { class: 'manual-map' });
  const chipRow = el('div', { class: 'mm-chips' });
  const summaryBox = el('div', { class: 'mm-summary' });
  const errorBox = el('div', { class: 'mm-errors' });

  const draftBtn = el('button', { class: 'btn small', onClick: () => applyDraft() }, '초안 채우기');
  const saveBtn = el('button', {
    class: 'btn btn-primary', disabled: true,
    onClick: () => {
      if (!lastBuild || !lastBuild.ok || !onSave) return;
      onSave({ sentence, model: lastBuild.model, pins: {}, area_id: null, category: null });
    },
  }, '루틴으로 저장');

  root.append(
    el('div', { class: 'mm-toolbar' },
      el('span', { class: 'form-hint' }, '단어를 눌러 역할을 지정하세요.'),
      draftBtn),
    chipRow,
    summaryBox,
    errorBox,
    el('div', { class: 'mm-actions' }, saveBtn));

  function assignOf(idx) {
    let a = assignByIndex.get(idx);
    if (!a) { a = { index: idx, role: 'ignore' }; assignByIndex.set(idx, a); }
    return a;
  }

  function assignmentsList() {
    return tokens.map(t => assignOf(t.index));
  }

  function applyDraft() {
    for (const s of suggestions) {
      if (s && typeof s.index === 'number') {
        assignByIndex.set(s.index, Object.assign({ index: s.index, role: 'ignore' }, s));
      }
    }
    renderChips();
    scheduleBuild();
  }

  function renderChips() {
    chipRow.textContent = '';
    for (const t of tokens) {
      const a = assignOf(t.index);
      const grp = roleGroup(a.role);
      const chip = el('button', {
        class: 'mm-chip mm-grp-' + grp + (a.role === 'ignore' ? ' is-ignore' : ''),
        onClick: () => openSheet(t),
      });
      chip.appendChild(el('span', { class: 'mm-tok' }, t.text));
      if (a.role !== 'ignore') {
        chip.appendChild(el('span', { class: 'mm-role' }, roleDetail(a) || ROLE_LABEL[a.role]));
      }
      chipRow.appendChild(chip);
      if (a.boundary || a.role === 'boundary') {
        chipRow.appendChild(el('span', { class: 'mm-divider', 'aria-hidden': 'true' }, '✂'));
      }
    }
  }

  function openSheet(token) {
    const cur = assignOf(token.index);
    // 편집용 복사본 — 취소 시 원본 유지.
    const draft = Object.assign({ index: token.index, role: 'ignore' }, cur);

    const controls = el('div', { class: 'mm-controls' });
    const boundaryWrap = el('div', { class: 'mm-boundary' });

    function paintControls() {
      controls.textContent = '';
      const r = draft.role;
      if (r === 'trigger_entity' || r === 'condition_entity' || r === 'action_target') {
        controls.appendChild(field('엔티티', entityPickBtn(draft.ref, id => { draft.ref = id; })));
      } else if (r === 'event_state') {
        controls.appendChild(field('상태', onOffSeg(draft.state || 'on', v => { draft.state = v; })));
      } else if (r === 'numeric') {
        controls.appendChild(field('비교', selectField(draft.cmp || 'above', CMP_OPTS, v => { draft.cmp = v; })));
        controls.appendChild(field('값', numberInput(draft.value == null ? null : draft.value, v => { draft.value = v; })));
      } else if (r === 'duration') {
        controls.appendChild(durationCtl(draft));
      } else if (r === 'segment') {
        controls.appendChild(field('시간대', selectField(draft.ref || 'dawn', SEGMENT_OPTS, v => { draft.ref = v; })));
      } else if (r === 'mode_ref') {
        controls.appendChild(field('모드', modeSelect(draft.ref, modeNames, v => { draft.ref = v; })));
        controls.appendChild(field('상태', onOffSeg(draft.state || 'on', v => { draft.state = v; })));
      } else if (r === 'daytype') {
        controls.appendChild(field('요일', selectField(draft.ref || 'weekday', DAYTYPE_OPTS, v => { draft.ref = v; })));
      } else if (r === 'season') {
        controls.appendChild(field('계절', selectField(draft.ref || 'spring', SEASON_OPTS, v => { draft.ref = v; })));
      } else if (r === 'value') {
        controls.appendChild(field('종류', selectField(draft.kind || 'brightness', KIND_OPTS, v => { draft.kind = v; })));
        controls.appendChild(field('값', numberInput(draft.value == null ? null : draft.value, v => { draft.value = v; })));
      } else if (r === 'action_verb') {
        controls.appendChild(field('동작', selectField(draft.verb || 'on', VERB_OPTS, v => { draft.verb = v; paintControls(); })));
        if (draft.verb === 'set_mode') {
          controls.appendChild(field('모드', modeSelect(draft.ref, modeNames, v => { draft.ref = v; })));
          controls.appendChild(field('상태', onOffSeg(draft.state || 'on', v => { draft.state = v; })));
        }
      }
    }

    const roleSel = selectField(draft.role, ROLES.map(r => ({ value: r.key, label: r.label })),
      v => { draft.role = v; paintControls(); });
    boundaryWrap.appendChild(checkboxField('여기서 규칙 나눔 (다음 규칙 시작)', !!draft.boundary,
      v => { draft.boundary = v; }));

    const body = el('div', { class: 'mm-sheet' },
      el('div', { class: 'mm-sheet-tok' }, token.text),
      field('역할', roleSel),
      controls,
      boundaryWrap);
    paintControls();

    const cancel = el('button', { class: 'btn', onClick: () => handle.close() }, '취소');
    const apply = el('button', {
      class: 'btn btn-primary',
      onClick: () => {
        commit(token.index, draft);
        handle.close();
        renderChips();
        scheduleBuild();
      },
    }, '적용');
    const handle = openModal({ title: '단어 역할 지정', body, footer: [cancel, apply], size: 'sheet' });
  }

  // duration 컨트롤: 초 단위(draft.value)를 숫자+단위로 편집.
  function durationCtl(draft) {
    let sec = Number(draft.value) || 0;
    let unit = 1;
    if (sec && sec % 3600 === 0) unit = 3600;
    else if (sec && sec % 60 === 0) unit = 60;
    let num = sec ? sec / unit : '';
    const numEl = numberInput(num === '' ? null : num, v => { num = v; recompute(); });
    const unitEl = selectField(String(unit), DUR_UNITS.map(u => ({ value: String(u.value), label: u.label })),
      v => { unit = Number(v); recompute(); });
    function recompute() { draft.value = Math.round((Number(num) || 0) * unit); }
    return field('지속시간', el('div', { class: 'mm-dur' }, numEl, unitEl));
  }

  // 커밋 — 관련 필드만 정리해 저장(불필요 필드 제거로 build 계약 준수).
  function commit(idx, draft) {
    const a = { index: idx, role: draft.role };
    if (draft.boundary) a.boundary = true;
    switch (draft.role) {
      case 'trigger_entity':
      case 'condition_entity':
      case 'action_target':
        a.ref = draft.ref || null; break;
      case 'event_state':
        a.state = draft.state || 'on'; break;
      case 'numeric':
        a.value = draft.value; a.cmp = draft.cmp || 'above'; break;
      case 'duration':
        a.value = draft.value; break;
      case 'segment':
        a.ref = draft.ref || 'dawn'; break;
      case 'mode_ref':
        // 셀렉트에 보이는 기본 모드(modeNames[0])를 미선택 시 폴백 — "화면엔 모드 보이는데 ref:null" 방지.
        a.ref = draft.ref || modeNames[0] || null; a.state = draft.state || 'on'; break;
      case 'daytype':
        a.ref = draft.ref || 'weekday'; break;
      case 'season':
        a.ref = draft.ref || 'spring'; break;
      case 'value':
        a.value = draft.value; a.kind = draft.kind || 'brightness'; break;
      case 'action_verb':
        a.verb = draft.verb || 'on';
        if (draft.verb === 'set_mode') { a.ref = draft.ref || modeNames[0] || null; a.state = draft.state || 'on'; }
        break;
      default: break;
    }
    assignByIndex.set(idx, a);
  }

  function scheduleBuild() {
    if (buildTimer) clearTimeout(buildTimer);
    buildTimer = setTimeout(refreshBuild, 250);
  }

  async function refreshBuild() {
    let data = null;
    try {
      data = await buildFromTokens(sentence, assignmentsList());
    } catch (_) { /* 토스트는 api.js */ }
    lastBuild = data;
    renderResult();
  }

  function renderResult() {
    summaryBox.textContent = '';
    errorBox.textContent = '';
    if (!lastBuild) { saveBtn.disabled = true; return; }
    if (lastBuild.summary) summaryBox.appendChild(el('div', { class: 'mm-summary-line' }, lastBuild.summary));
    const errs = lastBuild.errors || [];
    if (errs.length) {
      const list = el('ul', {});
      for (const e of errs) list.appendChild(el('li', {}, e));
      errorBox.appendChild(el('div', { class: 'error-list' },
        el('p', { class: 'error-list-title' }, '아직 저장할 수 없어요'), list));
    }
    for (const w of lastBuild.warnings || []) {
      errorBox.appendChild(el('p', { class: 'form-hint' }, w));
    }
    saveBtn.disabled = !lastBuild.ok;
  }

  async function init() {
    chipRow.appendChild(el('div', { class: 'form-hint' }, '단어를 나누는 중…'));
    try {
      const [modesRes, tokRes] = await Promise.all([loadModes(), tokenizeSentence(sentence)]);
      modeNames = modesRes;
      tokens = (tokRes && tokRes.tokens) || [];
      suggestions = (tokRes && tokRes.suggestions) || [];
    } catch (_) {
      tokens = [];
    }
    if (!tokens.length) {
      chipRow.textContent = '';
      chipRow.appendChild(el('div', { class: 'form-hint' }, '토큰을 불러오지 못했어요.'));
      return;
    }
    for (const t of tokens) assignOf(t.index);
    renderChips();
    scheduleBuild();
  }

  async function loadModes() {
    try {
      const d = await getModes();
      const names = (d && d.modes || []).map(m => m.name).filter(Boolean);
      if (names.length) return names;
    } catch (_) { /* 폴백 */ }
    try {
      const s = await getSettings();
      return Object.keys((s && s.modes) || {});
    } catch (_) { return []; }
  }

  init();
  return root;
}
