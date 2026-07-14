// 시/분/초 입력. value(Duration 객체)를 제자리에서 변경한다.
import { el } from '../app.js';

export function createDurationInput(value, onChange) {
  const d = value || { hours: 0, minutes: 0, seconds: 0 };
  if (d.hours == null) d.hours = 0;
  if (d.minutes == null) d.minutes = 0;
  if (d.seconds == null) d.seconds = 0;

  function unit(key, max, suffix) {
    const input = el('input', {
      class: 'input dur-input', type: 'number', min: '0', max: String(max),
      value: String(d[key] || 0),
      onInput: e => {
        let n = parseInt(e.target.value, 10);
        if (isNaN(n) || n < 0) n = 0;
        d[key] = n;
        if (onChange) onChange(d);
      },
    });
    return el('span', { class: 'dur-unit' }, input, el('span', { class: 'dur-suffix' }, suffix));
  }

  return el('div', { class: 'duration-input' },
    unit('hours', 99, '시간'),
    unit('minutes', 59, '분'),
    unit('seconds', 59, '초'));
}

// duration 객체가 전부 0인지.
export function isDurationZero(d) {
  return !d || ((d.hours || 0) === 0 && (d.minutes || 0) === 0 && (d.seconds || 0) === 0);
}

// 선택적 지속시간: 체크박스 + (켜졌을 때만) 시/분/초 입력.
// node[key]가 있으면 켜진 상태로 본다. onChange는 구조 변경 콜백(체크 토글 시 호출).
export function optionalDurationField(node, key, labelText, onChange) {
  const enabled = node[key] != null;
  const box = el('input', {
    type: 'checkbox',
    onChange: e => {
      if (e.target.checked) node[key] = { hours: 0, minutes: 0, seconds: 0 };
      else delete node[key];
      if (onChange) onChange();
    },
  });
  if (enabled) box.checked = true;
  const head = el('label', { class: 'checkbox-field' }, box, el('span', {}, labelText));
  const children = [head];
  if (enabled) children.push(createDurationInput(node[key]));
  return el('div', { class: 'opt-duration' }, ...children);
}
