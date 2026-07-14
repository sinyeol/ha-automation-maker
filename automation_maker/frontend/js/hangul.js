// 한글 초성 검색: 유니코드 가(0xAC00) 기반 분해.
const CHOSEONG = [
  'ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ', 'ㅆ',
  'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ',
];

const HANGUL_BASE = 0xac00;
const HANGUL_LAST = 0xd7a3;

// 문자열의 각 음절을 초성으로 치환. 한글이 아니면 그대로 둔다.
export function getChoseong(str) {
  let out = '';
  for (const ch of String(str)) {
    const code = ch.charCodeAt(0);
    if (code >= HANGUL_BASE && code <= HANGUL_LAST) {
      out += CHOSEONG[Math.floor((code - HANGUL_BASE) / 588)];
    } else {
      out += ch;
    }
  }
  return out;
}

// (1) 소문자 부분일치 (2) 초성열 매칭 둘 다 지원.
export function matchKorean(text, query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return true;
  const t = String(text || '');
  if (t.toLowerCase().includes(q)) return true;
  return getChoseong(t).toLowerCase().includes(q);
}
