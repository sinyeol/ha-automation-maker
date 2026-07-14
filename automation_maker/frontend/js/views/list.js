// 자동화 목록 뷰.
import { el, navigate } from '../app.js';
import { store } from '../store.js';
import { post, del } from '../api.js';
import { showToast } from '../components/toast.js';
import { confirmDialog } from '../components/modal.js';

function formatLastTriggered(iso) {
  if (!iso) return '아직 실행 안 됨';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function renderList(root) {
  root.textContent = '';
  const listWrap = el('div', { class: 'auto-list' });

  const header = el('div', { class: 'page-head' },
    el('h2', { class: 'page-title' }, '내 자동화'),
    el('div', { class: 'page-actions' },
      el('button', { class: 'btn', onClick: () => refresh() }, '새로고침'),
      el('button', { class: 'btn btn-primary', onClick: () => navigate('#/new') }, '+ 새 자동화')));

  root.append(header, listWrap);

  function paint() {
    listWrap.textContent = '';
    const items = store.state.automations;
    if (!items.length) {
      listWrap.appendChild(el('div', { class: 'empty-hint big' },
        el('p', {}, '아직 만든 자동화가 없어요.'),
        el('button', { class: 'btn btn-primary', onClick: () => navigate('#/new') }, '첫 자동화 만들기')));
      return;
    }
    for (const a of items) listWrap.appendChild(card(a));
  }

  function card(a) {
    const isOn = a.state === 'on';
    const editable = a.editable;

    const toggle = el('button', {
      class: 'switch' + (isOn ? ' on' : ''),
      'aria-label': isOn ? '켜짐' : '꺼짐',
      disabled: !editable || null,
      onClick: async () => {
        try {
          await post(`automations/${encodeURIComponent(a.automation_id)}/toggle`, { on: !isOn });
          a.state = isOn ? 'off' : 'on';
          paint();
        } catch (_) { /* 토스트는 api.js가 처리 */ }
      },
    }, el('span', { class: 'switch-knob' }));

    const meta = el('div', { class: 'auto-meta' },
      el('span', { class: 'auto-name' }, a.alias || a.entity_id),
      el('span', { class: 'auto-sub' }, `마지막 실행: ${formatLastTriggered(a.last_triggered)}`));

    const badges = el('div', { class: 'auto-badges' });
    if (!editable) badges.appendChild(el('span', { class: 'badge' }, 'YAML 관리형'));

    const actions = el('div', { class: 'auto-actions' },
      el('button', {
        class: 'btn small', disabled: !editable || null,
        onClick: async () => {
          try { await post(`automations/${encodeURIComponent(a.automation_id)}/run`, {}); showToast('실행했어요.', 'info'); } catch (_) {}
        },
      }, '▶ 실행'),
      el('button', {
        class: 'btn small', disabled: !editable || null,
        onClick: () => navigate(`#/edit/${encodeURIComponent(a.automation_id)}`),
      }, '편집'),
      el('button', {
        class: 'btn small btn-danger', disabled: !editable || null,
        onClick: async () => {
          const ok = await confirmDialog(`'${a.alias || a.entity_id}' 자동화를 삭제할까요?`, { okText: '삭제', danger: true });
          if (!ok) return;
          try {
            await del(`automations/${encodeURIComponent(a.automation_id)}`);
            showToast('삭제했어요.', 'info');
            await refresh();
          } catch (_) {}
        },
      }, '삭제'));

    return el('div', { class: 'auto-card' },
      el('div', { class: 'auto-top' }, toggle, meta),
      badges,
      actions);
  }

  async function refresh() {
    try {
      await store.refreshAutomations();
    } catch (_) { /* 토스트 처리됨 */ }
    paint();
  }

  // 목록 뷰 진입 시 항상 재조회.
  refresh();
}
