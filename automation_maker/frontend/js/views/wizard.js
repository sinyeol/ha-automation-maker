// 새 자동화 위저드: "언제 실행할까요?" 4택 + 고급.
import { el } from '../app.js';
import { defaultTrigger } from '../forms/trigger-forms.js';
import { renderEditorView } from './editor.js';

function emptyModel() {
  return { alias: '', description: '', mode: 'single', triggers: [], condition_mode: 'and', conditions: [], actions: [] };
}

const CHOICES = [
  { icon: '⏰', title: '시간이 되면', desc: '특정 시각 · 주기 · 해 뜨고 짐', trigger: 'time' },
  { icon: '🚶', title: '사람이 오가면', desc: '구역 출입 · 재실', trigger: 'zone' },
  { icon: '📡', title: '센서가 감지하면', desc: '모션 · 문열림 · 누수 등', trigger: 'state' },
  { icon: '📊', title: '값이 변하면', desc: '온도 · 습도 · 전력 등 수치', trigger: 'numeric_state' },
];

export function renderWizard(root) {
  root.textContent = '';

  const cards = el('div', { class: 'wizard-grid' });
  for (const c of CHOICES) {
    cards.appendChild(el('button', {
      class: 'wizard-card',
      onClick: () => startWith(root, c.trigger),
    },
      el('span', { class: 'wizard-icon' }, c.icon),
      el('span', { class: 'wizard-card-title' }, c.title),
      el('span', { class: 'wizard-card-desc' }, c.desc)));
  }

  const advanced = el('button', {
    class: 'btn ghost wide', onClick: () => startEmpty(root),
  }, '고급: 직접 구성 (빈 편집기)');

  root.append(
    el('div', { class: 'page-head' },
      el('button', { class: 'btn small', onClick: () => { location.hash = '#/list'; } }, '← 목록'),
      el('h2', { class: 'page-title' }, '언제 실행할까요?')),
    el('p', { class: 'wizard-lead' }, '자동화가 시작될 계기를 골라주세요.'),
    cards,
    advanced);
}

function startWith(root, triggerType) {
  const model = emptyModel();
  model.triggers.push(defaultTrigger(triggerType));
  renderEditorView(root, { id: null, model });
}

function startEmpty(root) {
  renderEditorView(root, { id: null, model: emptyModel() });
}
