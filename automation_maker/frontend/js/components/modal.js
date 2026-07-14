// 재사용 모달 / 바텀시트.
import { el } from '../app.js';

// openModal({ title, body(Node), footer([Node]), size:'sheet'|undefined, onClose })
export function openModal({ title, body, footer, size, onClose }) {
  const overlay = el('div', { class: 'modal-overlay' });
  const sheet = el('div', { class: 'modal' + (size === 'sheet' ? ' modal-sheet' : '') });

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    document.body.classList.remove('modal-open');
    if (onClose) onClose();
  }
  function onKey(e) { if (e.key === 'Escape') close(); }

  const header = el('div', { class: 'modal-header' },
    el('h2', { class: 'modal-title' }, title || ''),
    el('button', { class: 'icon-btn', 'aria-label': '닫기', onClick: close }, '✕'));

  const bodyEl = el('div', { class: 'modal-body' });
  if (body) bodyEl.appendChild(body);

  sheet.append(header, bodyEl);
  if (footer && footer.length) {
    sheet.appendChild(el('div', { class: 'modal-footer' }, ...footer));
  }
  overlay.appendChild(sheet);
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(overlay);
  document.body.classList.add('modal-open');

  return { close, overlay, sheet, bodyEl };
}

// 간단한 확인 다이얼로그. Promise<boolean> 반환.
export function confirmDialog(message, { okText = '확인', danger = false } = {}) {
  return new Promise(resolve => {
    let handle;
    // close()가 동기적으로 onClose를 호출하므로, 확정값을 먼저 settle해야 한다.
    let settled = false;
    const done = v => { if (!settled) { settled = true; resolve(v); } };
    const ok = el('button', { class: 'btn' + (danger ? ' btn-danger' : ' btn-primary'), onClick: () => { done(true); handle.close(); } }, okText);
    const cancel = el('button', { class: 'btn', onClick: () => handle.close() }, '취소');
    handle = openModal({
      title: '확인',
      body: el('p', { class: 'confirm-msg' }, message),
      footer: [cancel, ok],
      onClose: () => done(false),
    });
  });
}
