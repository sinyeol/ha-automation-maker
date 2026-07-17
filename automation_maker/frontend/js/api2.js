// api/v2/* fetch 래퍼. 변이 요청은 api.js(토스트 포함)를 재사용하고,
// 상태 폴링만 조용히(토스트 없이) 실패하도록 별도 처리한다.
import { get, post, put, del } from './api.js';

const P = 'v2/';

// 30초 폴링용: 실패해도 토스트를 띄우지 않고 null 반환.
export async function getStatusQuiet() {
  try {
    const res = await fetch('api/' + P + 'status');
    if (!res.ok) return null;
    return await res.json();
  } catch (_) {
    return null;
  }
}

export const parseSentence = (sentence, pins) =>
  post(P + 'parse', { sentence, pins: pins || {} });

export const listRules = () => get(P + 'rules');
export const createRule = (body) => post(P + 'rules', body);
export const updateRule = (id, body) => put(P + 'rules/' + encodeURIComponent(id), body);
export const deleteRule = (id) => del(P + 'rules/' + encodeURIComponent(id));
export const toggleRule = (id, on) => post(P + 'rules/' + encodeURIComponent(id) + '/toggle', { on });
export const runRule = (id) => post(P + 'rules/' + encodeURIComponent(id) + '/run', {});

export const getRunlog = () => get(P + 'runlog');
export const getSettings = () => get(P + 'settings');
export const putSettings = (body) => put(P + 'settings', body);

// v3 — 모드 상태
export const getModes = () => get(P + 'modes');
export const toggleMode = (name, on) =>
  post(P + 'modes/' + encodeURIComponent(name), { on });

// v3 — 수동 단어 매핑
export const tokenizeSentence = (sentence) => post(P + 'tokenize', { sentence });
export const buildFromTokens = (sentence, assignments) =>
  post(P + 'build', { sentence, assignments });

// Phase 3C — AI 학습(미해석 문장 → 정규화 → 학습)
export const learnSentence = (sentence) => post(P + 'learn', { sentence });
export const confirmLearn = (sentence, normalized, model) =>
  post(P + 'learn/confirm', { sentence, normalized, model });
export const getLearned = () => get(P + 'learned');
export const deleteLearned = (id) => del(P + 'learned/' + encodeURIComponent(id));
