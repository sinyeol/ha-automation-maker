// 토스트 알림. (순환 의존을 피하려 el 헬퍼 대신 raw DOM 사용)
let container = null;

function ensureContainer() {
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  return container;
}

export function showToast(message, type = 'info', timeout = 4000) {
  const box = ensureContainer();
  const toast = document.createElement('div');
  toast.className = 'toast toast-' + type;
  toast.textContent = message;
  box.appendChild(toast);
  // 다음 프레임에 표시 트랜지션
  requestAnimationFrame(() => toast.classList.add('show'));
  const remove = () => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 250);
  };
  toast.addEventListener('click', remove);
  if (timeout > 0) setTimeout(remove, timeout);
  return remove;
}
