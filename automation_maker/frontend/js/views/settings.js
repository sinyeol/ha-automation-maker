// v2 설정 화면: 시간대 경계 / 사람 매핑 / 모드 / 별칭 편집 → PUT api/v2/settings.
import { el, field, selectField } from '../app.js';
import { store } from '../store.js';
import { showToast } from '../components/toast.js';
import { openEntityPicker } from '../components/entity-picker.js';
import { getSettings, putSettings } from '../api2.js';

const SEGMENTS = [
  { key: 'dawn', label: '새벽' },
  { key: 'morning', label: '아침' },
  { key: 'day', label: '낮' },
  { key: 'evening', label: '저녁' },
  { key: 'night', label: '밤' },
];
const DEFAULT_SEG = { dawn: '00:00', morning: '06:00', day: '09:00', evening: '17:00', night: '21:00' };
const FIXED_PERSONS = ['나', '와이프'];

export function renderSettings(root) {
  root.textContent = '';
  root.appendChild(el('div', { class: 'loading' }, '불러오는 중…'));

  getSettings().then(data => build(root, data || {})).catch(() => {
    root.textContent = '';
    root.appendChild(el('div', { class: 'error-box' },
      el('p', {}, '설정을 불러오지 못했어요.'),
      el('button', { class: 'btn', onClick: () => renderSettings(root) }, '다시 시도')));
  });
}

function build(root, data) {
  // 편집용 로컬 복사본
  const seg = Object.assign({}, DEFAULT_SEG, data.segments || {});

  const persons = data.persons || {};
  const personList = FIXED_PERSONS.map(s => ({ surface: s, entity_id: persons[s] || '', fixed: true }));
  for (const [k, v] of Object.entries(persons)) {
    if (!FIXED_PERSONS.includes(k)) personList.push({ surface: k, entity_id: v || '', fixed: false });
  }

  const modeList = [];
  for (const [name, val] of Object.entries(data.modes || {})) {
    const tgt = (val && val.target && val.target.entity_id) || '';
    const eid = Array.isArray(tgt) ? (tgt[0] || '') : tgt;
    modeList.push({ name, entity_id: eid });
  }

  const aliasList = (data.aliases || []).map(a => ({ surface: a.surface || '', entity_id: a.entity_id || '' }));

  const content = el('div', { class: 'settings-body' });
  root.textContent = '';
  root.append(
    el('h2', { class: 'page-title' }, '설정'),
    content);

  const personOptions = () => [{ value: '', label: '(없음)' }]
    .concat(store.entities.filter(e => e.domain === 'person')
      .map(e => ({ value: e.entity_id, label: e.name })));

  function render() {
    content.textContent = '';

    // 1) 시간대 경계
    const segGrid = el('div', { class: 'seg-time-grid' });
    for (const s of SEGMENTS) {
      const inp = el('input', {
        class: 'input', type: 'time', value: seg[s.key] || DEFAULT_SEG[s.key],
      });
      inp.addEventListener('input', () => { seg[s.key] = inp.value; });
      segGrid.appendChild(field(s.label, inp));
    }
    content.appendChild(section('시간대 경계', '각 시간대가 시작하는 시각이에요.', segGrid));

    // 2) 사람 매핑
    const personBox = el('div', { class: 'map-list' });
    for (const p of personList) {
      const nameCtl = p.fixed
        ? el('div', { class: 'map-fixed' }, p.surface)
        : textInput(p.surface, v => { p.surface = v; }, '표면형 (예: 아들)');
      const sel = selectField(p.entity_id, personOptions(), v => { p.entity_id = v; });
      const row = el('div', { class: 'map-row' }, nameCtl, sel);
      if (!p.fixed) row.appendChild(removeBtn(() => { personList.splice(personList.indexOf(p), 1); render(); }));
      personBox.appendChild(row);
    }
    personBox.appendChild(addBtn('+ 사람 추가', () => {
      personList.push({ surface: '', entity_id: '', fixed: false }); render();
    }));
    content.appendChild(section('사람 매핑', '문장에서 부르는 이름과 person 엔티티를 연결해요.', personBox));

    // 3) 모드 매핑
    const modeBox = el('div', { class: 'map-list' });
    for (const m of modeList) {
      const nameCtl = textInput(m.name, v => { m.name = v; }, '모드 이름 (예: 슬립 모드)');
      const pick = entityPickBtn(m.entity_id, '씬/스크립트 선택',
        e => e.domain === 'scene' || e.domain === 'script',
        id => { m.entity_id = id; render(); });
      modeBox.appendChild(el('div', { class: 'map-row' }, nameCtl, pick,
        removeBtn(() => { modeList.splice(modeList.indexOf(m), 1); render(); })));
    }
    modeBox.appendChild(addBtn('+ 모드 추가', () => { modeList.push({ name: '', entity_id: '' }); render(); }));
    content.appendChild(section('모드 매핑', '“슬립 모드” 같은 이름을 씬/스크립트에 연결해요.', modeBox));

    // 4) 별칭
    const aliasBox = el('div', { class: 'map-list' });
    for (const a of aliasList) {
      const nameCtl = textInput(a.surface, v => { a.surface = v; }, '표면형 (예: 안방 무드등)');
      const pick = entityPickBtn(a.entity_id, '엔티티 선택', null,
        id => { a.entity_id = id; render(); });
      aliasBox.appendChild(el('div', { class: 'map-row' }, nameCtl, pick,
        removeBtn(() => { aliasList.splice(aliasList.indexOf(a), 1); render(); })));
    }
    aliasBox.appendChild(addBtn('+ 별칭 추가', () => { aliasList.push({ surface: '', entity_id: '' }); render(); }));
    content.appendChild(section('별칭', '표면형 단어를 특정 엔티티로 고정해요.', aliasBox));

    // 5) AI 해석 보조 상태
    const llmText = data.llm_available
      ? 'AI 해석 보조: 사용 가능 (API 키는 애드온 설정에서 관리해요.)'
      : 'AI 해석 보조: 꺼짐 (애드온 설정에서 API 키를 입력하면 켜져요.)';
    content.appendChild(section('AI 해석 보조', null, el('p', { class: 'form-hint' }, llmText)));

    content.appendChild(el('div', { class: 'settings-save' },
      el('button', { class: 'btn btn-primary wide', onClick: () => save() }, '설정 저장')));
  }

  async function save() {
    const personsObj = {};
    for (const p of personList) if (p.surface && p.entity_id) personsObj[p.surface] = p.entity_id;

    const modesObj = {};
    for (const m of modeList) {
      if (!m.name || !m.entity_id) continue;
      const domain = m.entity_id.split('.')[0];
      modesObj[m.name] = { action: `${domain}.turn_on`, target: { entity_id: [m.entity_id] } };
    }

    const aliasesArr = aliasList
      .filter(a => a.surface && a.entity_id)
      .map(a => ({ surface: a.surface, entity_id: a.entity_id }));

    try {
      await putSettings({ segments: seg, persons: personsObj, modes: modesObj, aliases: aliasesArr });
      showToast('설정을 저장했어요.', 'info');
    } catch (_) { /* 토스트 처리됨 */ }
  }

  render();
}

// --- 로컬 헬퍼 ---
function section(title, hint, body) {
  return el('div', { class: 'section-card settings-section' },
    el('h3', { class: 'section-title' }, title),
    hint ? el('p', { class: 'form-hint section-hint' }, hint) : null,
    body);
}

function textInput(value, onInput, placeholder) {
  return el('input', {
    class: 'input', type: 'text', value: value == null ? '' : value,
    placeholder: placeholder || '',
    onInput: e => onInput(e.target.value),
  });
}

function addBtn(label, onClick) {
  return el('button', { class: 'add-btn ghost', onClick }, label);
}

function removeBtn(onClick) {
  return el('button', { class: 'icon-btn danger', 'aria-label': '삭제', onClick }, '✕');
}

function entityPickBtn(entityId, placeholder, filter, onPick) {
  const label = entityId ? store.entityName(entityId) : placeholder;
  return el('button', {
    class: 'entity-select' + (entityId ? '' : ' empty'),
    onClick: async () => {
      const id = await openEntityPicker({ filter: filter || (() => true), title: placeholder });
      if (id) onPick(id);
    },
  }, label);
}
