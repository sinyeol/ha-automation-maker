// 진입점: DOM 헬퍼 + 해시 라우터 + 부트스트랩 로딩
import { store } from './store.js';
import { showToast } from './components/toast.js';
import { renderList } from './views/list.js';
import { renderWizard } from './views/wizard.js';
import { renderEditorForEdit } from './views/editor.js';

// el(tag, attrs, ...children): 유일한 DOM 생성 헬퍼.
// 사용자 데이터는 항상 textContent(문자열 자식)로만 삽입한다(XSS 방지).
export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'dataset' && typeof v === 'object') {
        for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
      } else if (k === 'style' && typeof v === 'object') {
        Object.assign(node.style, v);
      } else if (k.startsWith('on') && typeof v === 'function') {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (v === true) {
        node.setAttribute(k, '');
      } else {
        node.setAttribute(k, v);
      }
    }
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) appendChildren(node, c);
    else if (c instanceof Node) node.appendChild(c);
    else node.appendChild(document.createTextNode(String(c)));
  }
}

// --- 공용 폼 필드 헬퍼 (모든 forms/views에서 재사용) ---
export function field(labelText, ...controls) {
  return el('div', { class: 'field' },
    labelText ? el('label', { class: 'field-label' }, labelText) : null,
    el('div', { class: 'field-control' }, ...controls));
}

export function selectField(value, options, onChange) {
  const s = el('select', { class: 'input', onChange: e => onChange(e.target.value) });
  for (const o of options) {
    const opt = el('option', { value: o.value }, o.label);
    if (o.value === value) opt.selected = true;
    s.appendChild(opt);
  }
  return s;
}

export function textInput(value, onChange, attrs = {}) {
  return el('input', Object.assign({
    class: 'input', type: 'text', value: value == null ? '' : value,
    onInput: e => onChange(e.target.value),
  }, attrs));
}

export function numberInput(value, onChange, attrs = {}) {
  return el('input', Object.assign({
    class: 'input', type: 'number', value: value == null ? '' : value,
    onInput: e => onChange(e.target.value === '' ? null : Number(e.target.value)),
  }, attrs));
}

export function checkboxField(labelText, checked, onChange) {
  const box = el('input', { type: 'checkbox', onChange: e => onChange(e.target.checked) });
  if (checked) box.checked = true;
  return el('label', { class: 'checkbox-field' }, box, el('span', {}, labelText));
}

export function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

let bootstrapPromise = null;
function ensureBootstrap() {
  if (store.state.bootstrap) return Promise.resolve();
  if (!bootstrapPromise) bootstrapPromise = store.loadBootstrap();
  return bootstrapPromise;
}

function updateModeBadge() {
  const badge = document.getElementById('mode-badge');
  if (!badge) return;
  if (store.state.mode === 'dev') {
    badge.textContent = 'DEV';
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
}

function route() {
  const root = document.getElementById('app');
  const hash = (location.hash || '#/list').replace(/^#/, '') || '/list';
  ensureBootstrap().then(() => {
    updateModeBadge();
    if (hash === '/list') renderList(root);
    else if (hash === '/new') renderWizard(root);
    else if (hash.startsWith('/edit/')) {
      let id;
      try { id = decodeURIComponent(hash.slice('/edit/'.length)); }
      catch (_) { location.hash = '#/list'; return; }
      renderEditorForEdit(root, id);
    } else { location.hash = '#/list'; }
  }).catch(err => {
    console.error(err);
    bootstrapPromise = null;
    root.textContent = '';
    root.appendChild(el('div', { class: 'error-box' },
      el('p', {}, '초기 데이터를 불러오지 못했어요.'),
      el('button', { class: 'btn', onClick: () => route() }, '다시 시도')));
  });
}

window.addEventListener('hashchange', route);
if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', route);
} else {
  route();
}
