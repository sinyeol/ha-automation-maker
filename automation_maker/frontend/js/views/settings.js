// v2/v3 설정 화면: 시간대 경계 / 사람 매핑 / 모드 관리 / 별칭 / LLM 백엔드 → PUT api/v2/settings.
import { el, field, selectField } from '../app.js';
import { store } from '../store.js';
import { showToast } from '../components/toast.js';
import { openEntityPicker } from '../components/entity-picker.js';
import { getSettings, putSettings, getModes, toggleMode } from '../api2.js';

const SEGMENTS = [
  { key: 'dawn', label: '새벽' },
  { key: 'morning', label: '아침' },
  { key: 'day', label: '낮' },
  { key: 'evening', label: '저녁' },
  { key: 'night', label: '밤' },
];
const DEFAULT_SEG = { dawn: '00:00', morning: '06:00', day: '09:00', evening: '17:00', night: '21:00' };
const FIXED_PERSONS = ['나', '와이프'];

const LLM_BACKENDS = [
  { key: 'off', label: '끄기', hint: 'AI 해석 없이 내장 파서만 사용해요.' },
  { key: 'api', label: 'API 키', hint: 'Anthropic API 키로 해석을 보조해요.' },
  { key: 'cli', label: '구독 CLI', hint: 'Claude 구독(claude 명령)으로 해석을 보조해요.' },
];

// llm_available: 불리언(구버전, API 키 유무) 또는 객체(백엔드별 준비 여부). 관대하게 조회.
function llmReady(avail, backend) {
  if (avail == null) return false;
  if (typeof avail === 'boolean') return backend === 'api' ? avail : false;
  if (backend === 'api') return !!(avail.api != null ? avail.api : avail.api_key);
  if (backend === 'cli') {
    return !!(avail.cli != null ? avail.cli
      : (avail.cli_installed != null ? avail.cli_installed : avail.oauth_token));
  }
  return true;   // off 는 항상 준비됨
}

// 새 형식({initial,on_action,off_action})과 구 형식({action,target}) 모두에서 씬 엔티티를 뽑는다.
function sceneFromMode(val) {
  const act = (val && val.on_action) || (val && (val.action || val.target) ? val : null);
  const tgt = act && act.target && act.target.entity_id;
  if (Array.isArray(tgt)) return tgt[0] || '';
  return tgt || '';
}

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
    modeList.push({
      name,
      initial: (val && val.initial === 'on') ? 'on' : 'off',
      sceneEntity: sceneFromMode(val),
      off_action: (val && val.off_action) || null,   // UI 미노출이지만 보존
    });
  }

  const aliasList = (data.aliases || []).map(a => ({ surface: a.surface || '', entity_id: a.entity_id || '' }));

  // 애드온 옵션(LLM_BACKEND) 폴백까지 반영된 top-level 계산값을 읽는다. 원시 data.llm.backend 만
  // 보면 env 로만 켠 백엔드가 '끄기'로 표시되고, 그 상태로 저장하면 settings.json 에 backend:'off' 가
  // 덮어써져 사용자 의도가 조용히 꺼진다(api_v2._llm_backend / _augment_settings 참고).
  let llmBackend = data.llm_backend || 'off';
  if (!LLM_BACKENDS.some(b => b.key === llmBackend)) llmBackend = 'off';
  const llmAvail = data.llm_available;

  let modeStates = new Map();   // 라이브 상태(engine): name → "on"|"off"

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

    // 3) 모드 관리
    content.appendChild(renderModes());

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

    // 5) LLM 백엔드
    content.appendChild(renderLlm());

    content.appendChild(el('div', { class: 'settings-save' },
      el('button', { class: 'btn btn-primary wide', onClick: () => save() }, '설정 저장')));
  }

  // --- 모드 관리 섹션 ---
  function renderModes() {
    const box = el('div', { class: 'map-list mode-manage' });
    for (const m of modeList) {
      const nameCtl = textInput(m.name, v => { m.name = v; }, '모드 이름 (예: 슬립 모드)');
      const initSel = selectField(m.initial,
        [{ value: 'off', label: '초기값 꺼짐' }, { value: 'on', label: '초기값 켜짐' }],
        v => { m.initial = v; });
      const scenePick = entityPickBtn(m.sceneEntity, '켜질 때 실행할 씬(선택)',
        e => e.domain === 'scene' || e.domain === 'script',
        id => { m.sceneEntity = id; render(); });

      const top = el('div', { class: 'map-row' }, nameCtl,
        removeBtn(() => { modeList.splice(modeList.indexOf(m), 1); render(); }));

      // 라이브 상태 토글 — 저장된 모드만(engine 이 상태를 가짐).
      const stateRow = el('div', { class: 'mode-state-row' });
      if (modeStates.has(m.name)) {
        const on = modeStates.get(m.name) === 'on';
        const tg = el('button', {
          class: 'switch' + (on ? ' on' : ''),
          'aria-label': on ? '켜짐' : '꺼짐',
          onClick: async () => {
            try {
              const res = await toggleMode(m.name, !on);
              applyModeStates(res && res.modes);
              render();
            } catch (_) { /* 토스트 처리됨 */ }
          },
        }, el('span', { class: 'switch-knob' }));
        stateRow.append(el('span', { class: 'form-hint' }, '현재 상태'), tg,
          el('span', { class: 'mode-state-label' }, on ? '켜짐' : '꺼짐'));
      } else {
        stateRow.appendChild(el('span', { class: 'form-hint' }, '저장하면 상태를 켜고 끌 수 있어요.'));
      }

      box.appendChild(el('div', { class: 'mode-item' },
        top,
        el('div', { class: 'map-row' }, initSel, scenePick),
        stateRow));
    }
    box.appendChild(addBtn('+ 모드 추가', () => {
      modeList.push({ name: '', initial: 'off', sceneEntity: '', off_action: null }); render();
    }));
    return section('모드 관리',
      '“슬립 모드” 같은 상태 변수예요. 트리거·조건으로 쓰고, 켜질 때 씬을 함께 실행할 수 있어요.', box);
  }

  function applyModeStates(modes) {
    if (!modes) return;
    if (Array.isArray(modes)) {
      for (const m of modes) if (m && m.name) modeStates.set(m.name, m.state);
    } else if (typeof modes === 'object') {
      for (const [k, v] of Object.entries(modes)) modeStates.set(k, v);
    }
  }

  // --- LLM 백엔드 섹션 ---
  function renderLlm() {
    const box = el('div', { class: 'llm-backends' });
    for (const b of LLM_BACKENDS) {
      const ready = b.key === 'off' ? true : llmReady(llmAvail, b.key);
      const input = el('input', {
        type: 'radio', name: 'llm-backend', value: b.key,
        onChange: () => { llmBackend = b.key; },
      });
      if (b.key === llmBackend) input.checked = true;
      const statusText = b.key === 'off'
        ? ''
        : (ready ? '준비됨' : (b.key === 'api' ? '키 없음 (애드온 설정에서 입력)' : 'CLI/토큰 없음'));
      const status = statusText
        ? el('span', { class: 'llm-status' + (ready ? ' ok' : ' off') }, statusText)
        : null;
      box.appendChild(el('label', { class: 'llm-option' },
        input,
        el('span', { class: 'llm-opt-main' },
          el('span', { class: 'llm-opt-label' }, b.label),
          el('span', { class: 'llm-opt-hint' }, b.hint)),
        status));
    }
    return section('LLM 해석 백엔드',
      'API 키/구독 토큰은 애드온 설정(환경)에서 관리해요. 여기서는 사용 여부만 정해요.', box);
  }

  async function save() {
    const personsObj = {};
    for (const p of personList) if (p.surface && p.entity_id) personsObj[p.surface] = p.entity_id;

    const modesObj = {};
    for (const m of modeList) {
      if (!m.name) continue;
      const entry = { initial: m.initial === 'on' ? 'on' : 'off' };
      entry.on_action = m.sceneEntity
        ? { action: 'scene.turn_on', target: { entity_id: [m.sceneEntity] } }
        : null;
      entry.off_action = m.off_action || null;
      modesObj[m.name] = entry;
    }

    const aliasesArr = aliasList
      .filter(a => a.surface && a.entity_id)
      .map(a => ({ surface: a.surface, entity_id: a.entity_id }));

    const llmObj = Object.assign({}, data.llm || {}, { backend: llmBackend });

    try {
      await putSettings({
        segments: seg, persons: personsObj, modes: modesObj,
        aliases: aliasesArr, llm: llmObj,
      });
      showToast('설정을 저장했어요.', 'info');
    } catch (_) { /* 토스트 처리됨 */ }
  }

  render();

  // 라이브 모드 상태를 비동기로 불러와 토글에 반영(백엔드 미구현 시 조용히 생략).
  getModes().then(res => {
    applyModeStates(res && res.modes);
    if (modeStates.size) render();
  }).catch(() => {});
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
