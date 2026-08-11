const SCRIPT_RANGES = [
  ['han', /[㐀-䶿一-鿿豈-﫿]/],
  ['kana', /[぀-ヿ]/],
  ['hangul', /[가-힯ᄀ-ᇿ]/],
  ['cyrillic', /[Ѐ-ӿ]/],
  ['arabic', /[؀-ۿ]/],
  ['hebrew', /[֐-׿]/],
  ['devanagari', /[ऀ-ॿ]/],
  ['malayalam', /[ഀ-ൿ]/],
  ['latin', /[A-Za-zÀ-ɏ]/],
];

const EXPECTED_SCRIPT = {
  zh: 'han',
  ja: 'kana',
  ko: 'hangul',
  ru: 'cyrillic',
  uk: 'cyrillic',
  ar: 'arabic',
  he: 'hebrew',
  hi: 'devanagari',
  ml: 'malayalam',
};

/// Whisper picks a language per clip. On short or noisy audio it sometimes
/// picks the wrong one and returns fluent text in a script the speaker never
/// used. Pasting that into the focused app is worse than reporting nothing, so
/// the caller is given a chance to reject it.
///
/// The threshold is deliberately high: a single foreign name or quoted term in
/// otherwise English dictation must not trip this.
export function languageMismatch(text, requested, threshold = 0.5) {
  if (!text || !requested || requested === 'auto') return null;

  const expected = EXPECTED_SCRIPT[requested] || 'latin';

  const counts = {};
  let total = 0;
  for (const char of text) {
    for (const [name, pattern] of SCRIPT_RANGES) {
      if (pattern.test(char)) {
        counts[name] = (counts[name] || 0) + 1;
        total += 1;
        break;
      }
    }
  }
  if (total < 8) return null;

  const [dominant, count] = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
  if (dominant === expected) return null;

  const share = count / total;
  if (share < threshold) return null;

  return { requested, expected_script: expected, detected_script: dominant, share };
}
