// 해석 확인 카드: 원문을 span 단위 칩으로 렌더 + 후보/피커로 슬롯 확정 → 재해석.
import { el } from '../app.js';
import { parseSentence } from '../api2.js';
import { openEntityPicker } from './entity-picker.js';
import { createManualMap } from './manual-map.js';

// used_llm 캡션의 백엔드 표기(§8).
const LLM_BACKEND_LABEL = { api: 'API', cli: '구독 CLI' };

// role → CSS 클래스(색상). action은 target과 같은 초록 계열.
function roleClass(role) {
  if (role === 'trigger' || role === 'condition' || role === 'target' || role === 'value') {
    return 'role-' + role;
  }
  if (role === 'action') return 'role-action';
  return 'role-target';
}

// role → 역할 색상 CSS 변수. confirmed 칩 밑줄에 인라인으로 쓴다(styles.css는 미소유).
function roleColorVar(role) {
  if (role === 'trigger') return 'var(--chip-trigger)';
  if (role === 'condition') return 'var(--chip-condition)';
  if (role === 'value') return 'var(--chip-value)';
  return 'var(--chip-target)';   // target / action / 기타
}

// span이 문장 내 유효 범위인지(칩을 문장 오버레이에 얹을 수 있는지) 판별.
// [0,0]·역전·범위 밖·비배열은 무효 → 하단 "인식된 항목" 행으로 보낸다.
function hasValidSpan(chip, sentenceLen) {
  const sp = chip && chip.span;
  if (!Array.isArray(sp) || sp.length < 2) return false;
  const s = sp[0];
  const e = sp[1];
  if (typeof s !== 'number' || typeof e !== 'number') return false;
  return s >= 0 && e > s && e <= sentenceLen;
}

function scorePct(score) {
  if (typeof score !== 'number') return '';
  return Math.round(score * 100) + '%';
}

// createParseCard({ sentence, pins, result, onSave }) → HTMLElement
// pins는 slot_key → entity_id. 후보/피커 선택 시 갱신 후 재해석한다.
export function createParseCard({ sentence, pins, result, onSave }) {
  const pinsState = Object.assign({}, pins || {});
  let cur = result;
  let busy = false;
  let dropdown = null;      // 현재 열린 후보 드롭다운
  let onDocClick = null;
  let manualOpen = false;   // "직접 지정" 에디터 표시 여부
  let manualNode = null;    // 재해석 사이에도 편집 상태를 유지하기 위해 1회 생성 후 보관

  const root = el('div', { class: 'parse-card' });

  function closeDropdown() {
    if (dropdown) { dropdown.remove(); dropdown = null; }
    if (onDocClick) { document.removeEventListener('mousedown', onDocClick, true); onDocClick = null; }
  }

  async function reparse() {
    if (busy) return;
    busy = true;
    closeDropdown();
    root.classList.add('is-busy');
    try {
      cur = await parseSentence(sentence, pinsState);
    } catch (_) {
      // 토스트는 api.js가 처리. 이전 결과 유지.
    } finally {
      busy = false;
      root.classList.remove('is-busy');
      render();
    }
  }

  function setPin(slotKey, entityId) {
    if (!slotKey) return;
    if (entityId) pinsState[slotKey] = entityId;
    else delete pinsState[slotKey];
    reparse();
  }

  async function pickManual(chip) {
    closeDropdown();
    const id = await openEntityPicker({ title: `'${chip.text}' 엔티티 선택` });
    if (id) setPin(chip.slot_key, id);
  }

  function openCandidates(anchor, chip) {
    if (dropdown) { closeDropdown(); return; }
    const dd = el('div', { class: 'chip-dropdown' });
    for (const c of chip.candidates || []) {
      dd.appendChild(el('button', {
        class: 'chip-cand',
        onClick: () => { closeDropdown(); setPin(chip.slot_key, c.id); },
      },
        el('span', { class: 'cand-main' },
          el('span', { class: 'cand-label' }, c.label || c.id),
          c.sublabel ? el('span', { class: 'cand-sub' }, c.sublabel) : null),
        el('span', { class: 'cand-score' }, scorePct(c.score))));
    }
    dd.appendChild(el('button', {
      class: 'chip-cand none',
      onClick: () => { closeDropdown(); pickManual(chip); },
    }, '이 중에 없음…'));

    root.appendChild(dd);
    const cr = anchor.getBoundingClientRect();
    const rr = root.getBoundingClientRect();
    let left = cr.left - rr.left;
    const maxLeft = root.clientWidth - dd.offsetWidth - 6;
    if (left > maxLeft) left = Math.max(6, maxLeft);
    dd.style.left = left + 'px';
    dd.style.top = (cr.bottom - rr.top + 4) + 'px';
    dropdown = dd;

    onDocClick = (e) => { if (!dd.contains(e.target) && e.target !== anchor) closeDropdown(); };
    setTimeout(() => document.addEventListener('mousedown', onDocClick, true), 0);
  }

  function chipToken(chip) {
    const status = chip.status || 'confirmed';
    const cls = 'chip-token ' + roleClass(chip.role) + ' status-' + status;
    const interactive = status === 'uncertain' || status === 'unresolved';
    const attrs = { class: cls };
    if (interactive) {
      attrs.role = 'button';
      attrs.tabindex = '0';
      attrs.onClick = (e) => {
        if (status === 'uncertain') openCandidates(e.currentTarget, chip);
        else pickManual(chip);
      };
    }
    const node = el('span', attrs, chip.text);
    // confirmed 칩은 테두리가 없어 원문과 구분이 약하다. 역할색 밑줄 + (역할 클래스가 주는)
    // 연한 배경으로 4개 role이 모두 보이게 한다. styles.css를 소유하지 않으므로 인라인 지정.
    if (status === 'confirmed') {
      const color = roleColorVar(chip.role);
      node.style.textDecorationLine = 'underline';
      node.style.textDecorationColor = color;
      node.style.textDecorationThickness = '2px';
      node.style.textUnderlineOffset = '2px';
    }
    if (chip.chosen) node.title = chip.chosen;
    return node;
  }

  // 문장 오버레이(valid span)와 하단 행(무효 span·겹침 칩)을 함께 만든다.
  // 반환: { wrap, overflow } — overflow는 하단 "인식된 항목" 행에 렌더할 칩 목록.
  function renderSentence() {
    const wrap = el('div', { class: 'parse-sentence' });
    const overflow = [];
    const inline = [];
    for (const chip of (cur.chips || [])) {
      if (hasValidSpan(chip, sentence.length)) inline.push(chip);
      else overflow.push(chip);          // span 없음/무효 → 하단 행
    }
    inline.sort((a, b) => a.span[0] - b.span[0]);
    let pos = 0;
    for (const chip of inline) {
      const s = chip.span[0];
      const e = chip.span[1];
      if (s < pos) { overflow.push(chip); continue; }   // 앞 칩과 겹침 → 하단 행
      if (s > pos) wrap.appendChild(document.createTextNode(sentence.slice(pos, s)));
      wrap.appendChild(chipToken(chip));
      pos = e;
    }
    if (pos < sentence.length) wrap.appendChild(document.createTextNode(sentence.slice(pos)));
    return { wrap, overflow };
  }

  // 하단 "인식된 항목" 칩 행. 문장에 얹지 못한 칩을 같은 3상태 스타일·탭 동작으로 렌더.
  function renderChipRow(chips) {
    const row = el('div', { class: 'parse-chip-row' });
    row.style.display = 'flex';
    row.style.flexDirection = 'column';
    row.style.gap = '6px';
    row.appendChild(el('div', { class: 'form-hint' }, '인식된 항목'));
    const chipsWrap = el('div', { class: 'chip-row' });
    for (const chip of chips) chipsWrap.appendChild(chipToken(chip));
    row.appendChild(chipsWrap);
    return row;
  }

  function render() {
    closeDropdown();
    root.textContent = '';

    const { wrap, overflow } = renderSentence();
    root.appendChild(wrap);
    if (overflow.length) root.appendChild(renderChipRow(overflow));

    if (cur.summary) {
      root.appendChild(el('div', { class: 'parse-summary' }, cur.summary));
    }

    if (Array.isArray(cur.unmatched) && cur.unmatched.length) {
      root.appendChild(el('div', { class: 'warn-banner' },
        el('span', {}, '이 부분은 이해하지 못했어요: '),
        el('span', {}, cur.unmatched.join(', '))));
    }

    if (Array.isArray(cur.warnings) && cur.warnings.length) {
      const box = el('div', { class: 'parse-warnings' });
      for (const w of cur.warnings) box.appendChild(el('p', { class: 'form-hint' }, w));
      root.appendChild(box);
    }

    if (cur.used_llm) {
      const backend = LLM_BACKEND_LABEL[cur.llm_backend];
      const text = backend
        ? `AI 도움으로 해석했어요. (${backend})`
        : 'AI 도움으로 해석했어요.';
      root.appendChild(el('div', { class: 'parse-caption' }, text));
    }

    const hasUnresolved = (cur.chips || []).some(c => c.status === 'unresolved');
    const saveBtn = el('button', {
      class: 'btn btn-primary',
      disabled: (hasUnresolved || busy) || null,
      onClick: () => onSave && onSave({
        sentence,
        model: cur.model,
        pins: pinsState,
        area_id: cur.area_id || null,
        category: cur.category || null,
      }),
    }, '루틴 저장');

    root.appendChild(el('div', { class: 'parse-actions' },
      el('button', { class: 'btn', disabled: busy || null, onClick: () => reparse() }, '다시 해석'),
      saveBtn));

    if (hasUnresolved) {
      root.appendChild(el('p', { class: 'form-hint' }, '빨간 점선으로 표시된 부분을 선택하면 저장할 수 있어요.'));
    }

    // 하단 "직접 지정" 토글 → 수동 매핑 에디터를 인라인 표시(§3.3).
    const manualToggle = el('button', {
      class: 'btn small mm-toggle' + (manualOpen ? ' active' : ''),
      'aria-expanded': manualOpen ? 'true' : 'false',
      onClick: () => { manualOpen = !manualOpen; render(); },
    }, manualOpen ? '직접 지정 닫기' : '직접 지정');
    root.appendChild(el('div', { class: 'mm-toggle-row' }, manualToggle));

    if (manualOpen) {
      if (!manualNode) {
        manualNode = createManualMap({ sentence, onSave });
      }
      root.appendChild(manualNode);
    }
  }

  render();
  return root;
}
