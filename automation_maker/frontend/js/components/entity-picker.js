// 3단 엔티티 피커: 방 그리드 → 카테고리 칩 → 엔티티 리스트. 상단 전역 검색.
import { el } from '../app.js';
import { store } from '../store.js';
import { CATEGORIES, categoryMeta, categoryOrder } from '../taxonomy.js';
import { openModal } from './modal.js';

const UNASSIGNED = '__unassigned__';

// openEntityPicker({ filter?, title? }) → Promise<entity_id | null>
export function openEntityPicker(options = {}) {
  return new Promise(resolve => {
    const filter = options.filter || (() => true);
    const candidates = store.entities.filter(filter);

    let stage = 'areas';   // 'areas' | 'categories' | 'entities'
    let curArea = null;    // area_id 또는 UNASSIGNED
    let curCat = null;
    let query = '';
    let resolved = false;

    const search = el('input', {
      class: 'input picker-search', type: 'search',
      placeholder: '전체 검색 (초성 가능)', autocomplete: 'off',
    });
    search.addEventListener('input', () => { query = search.value; render(); });

    const breadcrumb = el('div', { class: 'picker-crumb' });
    const content = el('div', { class: 'picker-content' });
    const body = el('div', { class: 'picker' }, search, breadcrumb, content);

    const handle = openModal({
      title: options.title || '엔티티 선택',
      body, size: 'sheet',
      onClose: () => { if (!resolved) { resolved = true; resolve(null); } },
    });

    function pick(id) {
      resolved = true;
      resolve(id);
      handle.close();
    }

    function areaLabel(areaId) {
      if (areaId === UNASSIGNED) return '미배정';
      const a = store.getArea(areaId);
      return a ? a.name : areaId;
    }

    function render() {
      content.textContent = '';
      breadcrumb.textContent = '';

      if (query.trim()) {
        const results = candidates.filter(e => store.matchesSearch(e, query));
        breadcrumb.appendChild(el('span', { class: 'crumb-hint' }, `검색 결과 ${results.length}개`));
        renderEntities(results);
        return;
      }

      if (stage === 'areas') {
        renderAreas();
      } else if (stage === 'categories') {
        breadcrumb.append(
          crumbBtn('방 선택', () => { stage = 'areas'; curArea = null; render(); }),
          el('span', { class: 'crumb-sep' }, '›'),
          el('span', { class: 'crumb-cur' }, areaLabel(curArea)));
        renderCategories();
      } else {
        const meta = categoryMeta(curCat);
        breadcrumb.append(
          crumbBtn('방 선택', () => { stage = 'areas'; curArea = null; render(); }),
          el('span', { class: 'crumb-sep' }, '›'),
          crumbBtn(areaLabel(curArea), () => { stage = 'categories'; render(); }),
          el('span', { class: 'crumb-sep' }, '›'),
          el('span', { class: 'crumb-cur' }, `${meta.icon} ${meta.label}`));
        const list = candidates.filter(e =>
          (e.area_id || UNASSIGNED) === curArea && e.category === curCat);
        renderEntities(list);
      }
    }

    function crumbBtn(text, onClick) {
      return el('button', { class: 'crumb-link', onClick }, text);
    }

    function renderAreas() {
      // 후보 엔티티 기준 방별 개수 집계
      const counts = new Map();
      for (const e of candidates) {
        const k = e.area_id || UNASSIGNED;
        counts.set(k, (counts.get(k) || 0) + 1);
      }
      const grid = el('div', { class: 'area-grid' });
      const ordered = store.areas.filter(a => counts.has(a.area_id));
      for (const a of ordered) grid.appendChild(areaCard(a.area_id, a.name, counts.get(a.area_id)));
      if (counts.has(UNASSIGNED)) grid.appendChild(areaCard(UNASSIGNED, '미배정', counts.get(UNASSIGNED)));
      if (!grid.children.length) {
        content.appendChild(el('div', { class: 'empty-hint' }, '선택할 수 있는 엔티티가 없어요.'));
      } else {
        content.appendChild(grid);
      }
    }

    function areaCard(areaId, name, count) {
      return el('button', {
        class: 'area-card',
        onClick: () => { curArea = areaId; stage = 'categories'; render(); },
      }, el('span', { class: 'area-name' }, name), el('span', { class: 'area-count' }, `${count}개`));
    }

    function renderCategories() {
      const inArea = candidates.filter(e => (e.area_id || UNASSIGNED) === curArea);
      const present = new Set(inArea.map(e => e.category));
      const chips = el('div', { class: 'chip-row' });
      for (const c of CATEGORIES) {
        if (!present.has(c.id)) continue;
        const n = inArea.filter(e => e.category === c.id).length;
        chips.appendChild(el('button', {
          class: 'chip',
          onClick: () => { curCat = c.id; stage = 'entities'; render(); },
        }, `${c.icon} ${c.label} ${n}`));
      }
      content.appendChild(chips);
    }

    function renderEntities(list) {
      const sorted = list.slice().sort((a, b) => {
        const ca = categoryOrder(a.category) - categoryOrder(b.category);
        if (ca !== 0) return ca;
        return (a.name || '').localeCompare(b.name || '', 'ko');
      });
      if (!sorted.length) {
        content.appendChild(el('div', { class: 'empty-hint' }, '엔티티가 없어요.'));
        return;
      }
      const listEl = el('div', { class: 'entity-list' });
      for (const e of sorted) {
        const secondary = [e.area_name || '미배정', e.device_name].filter(Boolean).join(' · ');
        const stateText = e.state == null ? '상태 없음' : String(e.state) + (e.unit ? ' ' + e.unit : '');
        listEl.appendChild(el('button', {
          class: 'entity-row', onClick: () => pick(e.entity_id),
        },
          el('span', { class: 'entity-ic' }, categoryMeta(e.category).icon),
          el('span', { class: 'entity-main' },
            el('span', { class: 'entity-name' }, e.name),
            el('span', { class: 'entity-sub' }, secondary)),
          el('span', { class: 'entity-state' }, stateText)));
      }
      content.appendChild(listEl);
    }

    render();
    setTimeout(() => search.focus(), 50);
  });
}

// 엔티티 선택 버튼(node[key]에 저장). onChange는 구조 변경 콜백.
export function entityField(node, key, filter, onChange, placeholder = '엔티티 선택') {
  const current = node[key];
  const label = current ? store.entityName(current) : placeholder;
  return el('button', {
    class: 'entity-select' + (current ? '' : ' empty'),
    onClick: async () => {
      const id = await openEntityPicker({ filter });
      if (id) { node[key] = id; onChange(); }
    },
  }, label);
}
