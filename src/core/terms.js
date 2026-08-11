/// The vocabulary field is a list, not prose: one term per comma or line.
/// Every entry is taken verbatim, so `kubectl` stays lowercase and `R2` keeps
/// its digit. Multi-word entries survive intact, which matters because
/// Deepgram boosts phrases like `Workers AI` as a unit.
///
/// Deepgram caps a request at 100 terms and 500 tokens.
const MAX_TERMS = 100;

export function parseTerms(vocabulary) {
  if (!vocabulary) return [];

  const seen = new Set();
  const terms = [];

  for (const entry of vocabulary.split(/[,;\n\r]+/)) {
    // Trailing sentence punctuation is a typo here, not part of the term.
    const term = entry.trim().replace(/^[^\p{L}\p{N}]+|[.!?]+$/gu, '').trim();
    if (!term || term.length > 60) continue;
    if (!/[\p{L}\p{N}]/u.test(term)) continue;

    const key = term.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    terms.push(term);

    if (terms.length >= MAX_TERMS) break;
  }

  return terms;
}

/// Whisper takes free text rather than a term list, and a comma separated
/// glossary is the documented way to bias its decoder toward a vocabulary.
export function termsAsWhisperPrompt(terms) {
  return terms.length ? `Glossary: ${terms.join(', ')}.` : undefined;
}
