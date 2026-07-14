// 부트스트랩 캐시 + 엔티티 인덱스 + 검색.
import { get } from './api.js';
import { categorize } from './taxonomy.js';
import { matchKorean } from './hangul.js';

export const store = {
  state: { bootstrap: null, mode: 'dev', automations: [] },
  _byId: new Map(),
  _areaById: new Map(),

  async loadBootstrap() {
    const data = await get('bootstrap');
    this.state.bootstrap = data;
    this.state.mode = data.mode || 'dev';
    this.state.automations = data.automations || [];
    this._index();
    return data;
  },

  _index() {
    this._byId.clear();
    this._areaById.clear();
    for (const a of this.areas) this._areaById.set(a.area_id, a);
    for (const e of this.entities) {
      e.category = categorize(e);
      this._byId.set(e.entity_id, e);
    }
  },

  get entities() { return (this.state.bootstrap && this.state.bootstrap.entities) || []; },
  get areas() { return (this.state.bootstrap && this.state.bootstrap.areas) || []; },
  get services() { return (this.state.bootstrap && this.state.bootstrap.services) || {}; },
  get zones() { return (this.state.bootstrap && this.state.bootstrap.zones) || []; },

  getEntity(id) { return this._byId.get(id); },
  getArea(id) { return this._areaById.get(id); },

  entityName(id) {
    const e = this._byId.get(id);
    if (e) return e.name;
    const z = this.zones.find(z => z.entity_id === id);
    return z ? z.name : id;
  },

  entityNameMap() {
    const m = {};
    for (const e of this.entities) m[e.entity_id] = e.name;
    return m;
  },

  entitiesInArea(areaId) {
    return this.entities.filter(e => (e.area_id || null) === (areaId || null));
  },

  // 이름 / entity_id / 방 이름 전역 검색(부분일치 + 초성).
  matchesSearch(entity, query) {
    return matchKorean(entity.name, query) ||
      matchKorean(entity.entity_id, query) ||
      matchKorean(entity.area_name || '', query);
  },

  servicesFor(domain) {
    return this.services[domain] || [];
  },

  async refreshAutomations() {
    const data = await get('automations');
    this.state.automations = (data && data.automations) || [];
    return this.state.automations;
  },
};
