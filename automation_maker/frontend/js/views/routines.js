// v2 루틴 화면: 문장 입력 → 해석(parse-card) → 저장, 규칙 카드 리스트.
import { el, setViewCleanup } from '../app.js';
import { store } from '../store.js';
import { matchKorean } from '../hangul.js';
import { categoryMeta, categoryOrder } from '../taxonomy.js';
import { showToast } from '../components/toast.js';
import { confirmDialog } from '../components/modal.js';
import { createParseCard } from '../components/parse-card.js';
import {
  parseSentence, listRules, createRule, updateRule, deleteRule,
  toggleRule, runRule, getStatusQuiet,
} from '../api2.js';

const SEGMENT_LABELS = { dawn: '새벽', morning: '아침', day: '낮', evening: '저녁', night: '밤' };
const UNASSIGNED = '__unassigned__';

const EXAMPLES = [
  '화장실은 5분 동안 움직임이 없으면 환풍기와 조명을 꺼줘',
  '거실 조명은 새벽시간에 거실에 움직임이 있으면 10%로 켜줘',
  '밤 9시가 되면 현관문을 잠가줘',
];

const TIME_TRIGGERS = new Set(['daily', 'segment', 'time']);

function formatLastFired(iso) {
  if (!iso) return '아직 실행 안 됨';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// subrules(다중 조건-액션 쌍)가 있으면 순회, 없으면 최상위 4필드를 단일 서브룰로 취급(하위호환, §2.2).
function subrulesOf(model) {
  const subs = model && model.subrules;
  if (Array.isArray(subs) && subs.length) return subs;
  return [{
    triggers: (model && model.triggers) || [],
    conditions: (model && model.conditions) || [],
    actions: (model && model.actions) || [],
  }];
}

function ruleIcon(model) {
  const subs = subrulesOf(model);
  let hasTime = false;
  for (const sr of subs) {
    for (const t of sr.triggers || []) {
      if (t.type === 'mode') return '🌙';   // 모드 트리거 우선 표시
      if (TIME_TRIGGERS.has(t.type)) hasTime = true;
    }
  }
  return hasTime ? '⏰' : '📡';
}

function subruleCount(model) {
  const subs = model && model.subrules;
  return Array.isArray(subs) ? subs.length : 1;
}

function targetCount(model) {
  const ids = new Set();
  for (const sr of subrulesOf(model)) {
    for (const a of sr.actions || []) {
      const t = a.target || {};
      const e = t.entity_id != null ? t.entity_id : a.entity_id;
      if (typeof e === 'string') ids.add(e);
      else if (Array.isArray(e)) e.forEach(x => ids.add(x));
    }
  }
  return ids.size;
}

export function renderRoutines(root) {
  root.textContent = '';
  let rules = [];
  let groupMode = 'area';   // 'area' | 'category' | 'all'
  let query = '';
  let pollTimer = null;

  // --- 헤더: 엔진 상태 + 현재 시간대 ---
  const dot = el('span', { class: 'status-dot' });
  const statusLabel = el('span', {}, '연결 확인 중…');
  const segLabel = el('span', { class: 'seg-now' });
  const statusBar = el('div', { class: 'engine-status' }, dot, statusLabel, segLabel);

  // --- 입력 영역 ---
  const input = el('textarea', {
    class: 'input textarea routine-textarea',
    placeholder: '예: ' + EXAMPLES[0],
    rows: '2',
  });
  const interpretBtn = el('button', { class: 'btn btn-primary', onClick: () => doInterpret() }, '해석');
  const exampleRow = el('div', { class: 'example-chips' });
  for (const ex of EXAMPLES) {
    exampleRow.appendChild(el('button', {
      class: 'chip example', onClick: () => { input.value = ex; input.focus(); },
    }, ex));
  }
  const cardWrap = el('div', { class: 'parse-card-wrap' });
  const compose = el('div', { class: 'routine-compose' },
    input,
    el('div', { class: 'compose-actions' }, interpretBtn),
    exampleRow);

  // --- 리스트 도구모음 ---
  const searchInput = el('input', {
    class: 'input rule-search', type: 'search', placeholder: '루틴 검색',
    autocomplete: 'off',
  });
  searchInput.addEventListener('input', () => { query = searchInput.value; paintRules(); });

  const seg = el('div', { class: 'segment' });
  const segButtons = [
    { key: 'area', label: '방별' },
    { key: 'category', label: '카테고리별' },
    { key: 'all', label: '전체' },
  ].map(o => {
    const b = el('button', {
      class: 'segment-btn' + (o.key === groupMode ? ' active' : ''),
      onClick: () => {
        groupMode = o.key;
        for (const bb of seg.children) bb.classList.toggle('active', bb === b);
        paintRules();
      },
    }, o.label);
    seg.appendChild(b);
    return b;
  });

  const toolbar = el('div', { class: 'rule-toolbar' }, seg, searchInput);
  const listWrap = el('div', { class: 'auto-list' });

  root.append(
    statusBar,
    compose,
    cardWrap,
    el('h2', { class: 'page-title routines-list-title' }, '내 루틴'),
    toolbar,
    listWrap);

  // --- 해석 흐름 ---
  async function doInterpret() {
    const s = input.value.trim();
    if (!s) { showToast('문장을 입력해 주세요.', 'info'); return; }
    try {
      const result = await parseSentence(s, {});
      showParse(s, {}, result, null);
    } catch (_) { /* 토스트 처리됨 */ }
  }

  function showParse(sentence, pins, result, editRuleId) {
    cardWrap.textContent = '';
    const card = createParseCard({
      sentence, pins, result,
      onSave: (payload) => handleSave(payload, editRuleId),
    });
    cardWrap.appendChild(card);
    cardWrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  async function startEdit(rule) {
    input.value = rule.sentence;
    const pins = Object.assign({}, rule.pins || {});
    try {
      const result = await parseSentence(rule.sentence, pins);
      showParse(rule.sentence, pins, result, rule.id);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (_) {}
  }

  async function handleSave(payload, editRuleId) {
    const body = {
      sentence: payload.sentence,
      model: payload.model,
      pins: payload.pins,
      area_id: payload.area_id,
      category: payload.category,
    };
    try {
      if (editRuleId) await updateRule(editRuleId, body);
      else await createRule(body);
      showToast('저장했어요.', 'info');
      cardWrap.textContent = '';
      input.value = '';
      await refreshRules();
    } catch (_) { /* 400 등은 토스트 처리됨 */ }
  }

  // --- 리스트 ---
  async function refreshRules() {
    try {
      const data = await listRules();
      rules = (data && data.rules) || [];
    } catch (_) { rules = rules || []; }
    paintRules();
  }

  function filtered() {
    const q = query.trim();
    if (!q) return rules;
    return rules.filter(r => matchKorean(r.sentence, q) || matchKorean(r.name || '', q));
  }

  function groupsFor(items) {
    if (groupMode === 'all') return [{ title: null, items }];
    if (groupMode === 'category') {
      const map = new Map();
      for (const r of items) {
        const k = r.category || 'etc';
        if (!map.has(k)) map.set(k, []);
        map.get(k).push(r);
      }
      return [...map.keys()]
        .sort((a, b) => categoryOrder(a) - categoryOrder(b))
        .map(k => {
          const m = categoryMeta(k);
          return { title: `${m.icon} ${m.label}`, items: map.get(k) };
        });
    }
    // 방별
    const map = new Map();
    for (const r of items) {
      const k = r.area_id || UNASSIGNED;
      if (!map.has(k)) map.set(k, []);
      map.get(k).push(r);
    }
    const groups = [];
    for (const a of store.areas) {
      if (map.has(a.area_id)) groups.push({ title: a.name, items: map.get(a.area_id) });
    }
    if (map.has(UNASSIGNED)) groups.push({ title: '미배정', items: map.get(UNASSIGNED) });
    return groups;
  }

  function paintRules() {
    listWrap.textContent = '';
    const items = filtered();
    if (!items.length) {
      listWrap.appendChild(el('div', { class: 'empty-hint big' },
        el('p', {}, rules.length ? '검색 결과가 없어요.' : '아직 만든 루틴이 없어요.')));
      return;
    }
    for (const g of groupsFor(items)) {
      const section = el('div', { class: 'rule-group' });
      if (g.title) section.appendChild(el('h3', { class: 'rule-group-title' }, g.title));
      const inner = el('div', { class: 'auto-list' });
      for (const r of g.items) inner.appendChild(ruleCard(r));
      section.appendChild(inner);
      listWrap.appendChild(section);
    }
  }

  function ruleCard(r) {
    const isOn = r.enabled !== false;
    const meta = r.meta || {};
    const model = r.model || {};

    const toggle = el('button', {
      class: 'switch' + (isOn ? ' on' : ''),
      'aria-label': isOn ? '켜짐' : '꺼짐',
      onClick: async () => {
        try {
          const data = await toggleRule(r.id, !isOn);
          if (data && data.rule) Object.assign(r, data.rule);
          else r.enabled = !isOn;
          paintRules();
        } catch (_) {}
      },
    }, el('span', { class: 'switch-knob' }));

    const areaName = r.area_id ? (store.getArea(r.area_id) || {}).name || r.area_id : '미배정';
    const subParts = [ruleIcon(model) + ' ' + areaName, `대상 ${targetCount(model)}개`];
    const nSub = subruleCount(model);
    if (nSub > 1) subParts.push(`규칙 ${nSub}개`);

    const title = el('button', {
      class: 'routine-title-btn', onClick: () => startEdit(r),
    }, r.name || r.sentence);

    const metaLine = el('div', { class: 'routine-meta' },
      el('span', { class: 'auto-sub' }, subParts.join(' · ')),
      el('span', { class: 'auto-sub' }, `마지막 실행: ${formatLastFired(meta.last_fired)}`));

    const badges = el('div', { class: 'auto-badges' });
    if (meta.auto_disabled) badges.appendChild(el('span', { class: 'badge danger' }, '오류로 꺼짐'));
    else if (meta.last_error) badges.appendChild(el('span', { class: 'badge danger' }, '오류'));

    const actions = el('div', { class: 'auto-actions' },
      el('button', {
        class: 'btn small',
        onClick: async () => {
          try { await runRule(r.id); showToast('테스트 실행했어요.', 'info'); }
          catch (_) {}
        },
      }, '▶ 테스트'),
      el('button', { class: 'btn small', onClick: () => startEdit(r) }, '편집'),
      el('button', {
        class: 'btn small btn-danger',
        onClick: async () => {
          const ok = await confirmDialog('이 루틴을 삭제할까요?', { okText: '삭제', danger: true });
          if (!ok) return;
          try { await deleteRule(r.id); showToast('삭제했어요.', 'info'); await refreshRules(); }
          catch (_) {}
        },
      }, '삭제'));

    return el('div', { class: 'auto-card routine-card' },
      el('div', { class: 'routine-top' }, toggle, el('div', { class: 'routine-head' }, title, metaLine)),
      badges,
      actions);
  }

  // --- 상태 폴링 ---
  async function poll() {
    const st = await getStatusQuiet();
    if (!st) {
      dot.className = 'status-dot off';
      statusLabel.textContent = '엔진 연결 끊김';
      segLabel.textContent = '';
      return;
    }
    dot.className = 'status-dot ' + (st.connected ? 'on' : 'off');
    statusLabel.textContent = st.connected ? '엔진 연결됨' : 'HA 연결 끊김';
    const s = st.vars && st.vars.segment;
    segLabel.textContent = s ? '· 지금: ' + (SEGMENT_LABELS[s] || s) : '';
  }

  setViewCleanup(() => { if (pollTimer) clearInterval(pollTimer); });
  pollTimer = setInterval(poll, 30000);
  poll();
  refreshRules();
}
