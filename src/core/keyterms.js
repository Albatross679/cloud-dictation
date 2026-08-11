/// Nova-3 boosts a list of discrete terms, not free text, so the initial
/// prompt is mined for the words worth boosting: proper nouns, acronyms, and
/// identifiers carrying digits or internal capitals.
///
/// Deepgram caps a request at 100 terms and 500 tokens.
const MAX_TERMS = 100;

// A colon introduces a list, so the word after it is not sentence-initial
// grammar and stays eligible.
const SENTENCE_END = /[.!?\n]$/;

export function keytermsFrom(initialPrompt) {
  if (!initialPrompt) return [];

  const terms = new Set();
  let atSentenceStart = true;

  for (const rawToken of initialPrompt.split(/[\s,()[\]{}"']+/)) {
    if (!rawToken) continue;

    const endsSentence = SENTENCE_END.test(rawToken);
    const token = rawToken.replace(/^[^\w]+|[^\w+#-]+$/g, '');
    const wasSentenceStart = atSentenceStart;
    atSentenceStart = endsSentence;

    if (token.length < 2 || token.length > 40) continue;
    if (!/[a-zA-Z]/.test(token)) continue;

    const hasDigit = /\d/.test(token);
    const internalCapital = /^.[a-z0-9]*[A-Z]/.test(token);
    const allCaps = token === token.toUpperCase();
    const capitalized = /^[A-Z][a-z]+$/.test(token);

    // A capitalized word opening a sentence is probably just grammar.
    if (hasDigit || internalCapital || allCaps || (capitalized && !wasSentenceStart)) {
      terms.add(token);
    }
  }

  return [...terms].slice(0, MAX_TERMS);
}
