// fetch 래퍼: 모든 경로는 상대("api/...")로만 요청한다(ingress 대응).
import { showToast } from './components/toast.js';

const BASE = 'api/';

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(BASE + path, opts);
  } catch (err) {
    showToast('서버에 연결할 수 없어요.', 'error');
    console.error('네트워크 오류', BASE + path, err);
    throw err;
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = text; }
  }
  if (!res.ok) {
    const msg = (data && data.error && data.error.message) || `요청에 실패했어요 (${res.status})`;
    showToast(msg, 'error');
    console.error('API 오류', method, path, res.status, data);
    const err = new Error(msg);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const get = (path) => request('GET', path);
export const post = (path, body) => request('POST', path, body);
export const put = (path, body) => request('PUT', path, body);
export const del = (path) => request('DELETE', path);
