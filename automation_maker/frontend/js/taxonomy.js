// §3.3 카테고리 분류. 정렬 순서는 이 배열 순서를 따른다.
export const CATEGORIES = [
  { id: 'lighting', label: '조명', icon: '💡' },
  { id: 'switch', label: '스위치/콘센트', icon: '🔌' },
  { id: 'safety', label: '안전', icon: '⚠️' },
  { id: 'detect', label: '감지기', icon: '📡' },
  { id: 'sensor', label: '환경 센서', icon: '📊' },
  { id: 'climate', label: '난방/공조', icon: '🌡️' },
  { id: 'fan', label: '환기/팬', icon: '🌀' },
  { id: 'cover', label: '커튼/블라인드', icon: '🪟' },
  { id: 'media', label: '미디어', icon: '📺' },
  { id: 'lock', label: '잠금', icon: '🔒' },
  { id: 'presence', label: '사람/위치', icon: '🚶' },
  { id: 'etc', label: '기타', icon: '⚙️' },
];

const META = {};
CATEGORIES.forEach((c, i) => { META[c.id] = Object.assign({ order: i }, c); });

const SAFETY_CLASSES = ['gas', 'smoke', 'carbon_monoxide', 'moisture'];

export function categorize(entity) {
  const d = entity.domain;
  const dc = entity.device_class || null;
  const name = entity.name || '';

  if (d === 'valve') return 'safety';
  if ((d === 'switch' || d === 'binary_sensor') &&
      (SAFETY_CLASSES.includes(dc) || name.includes('가스'))) return 'safety';
  if (d === 'light') return 'lighting';
  if (d === 'switch') return 'switch';
  if (d === 'binary_sensor') return 'detect';
  if (d === 'sensor') return 'sensor';
  if (d === 'climate') return 'climate';
  if (d === 'fan') return 'fan';
  if (d === 'cover') return 'cover';
  if (d === 'media_player') return 'media';
  if (d === 'lock') return 'lock';
  if (d === 'person' || d === 'device_tracker') return 'presence';
  return 'etc';
}

export function categoryMeta(id) {
  return META[id] || META.etc;
}

export function categoryOrder(id) {
  return META[id] ? META[id].order : CATEGORIES.length;
}
