export const CLEANUP_MODELS = {
  'llama-8b': '@cf/meta/llama-3.1-8b-instruct-fp8',
  'llama-3b': '@cf/meta/llama-3.2-3b-instruct',
  'granite-micro': '@cf/ibm-granite/granite-4.0-h-micro',
  'mistral-24b': '@cf/mistralai/mistral-small-3.1-24b-instruct',
};

export const DEFAULT_CLEANUP_MODEL = 'llama-8b';

const SYSTEM = `You clean up dictated speech into written text.

Rules:
- Remove filler words: um, uh, like, you know, I mean, sort of.
- Remove false starts and self-corrections. Keep only what the speaker settled on.
- Fix grammar and add punctuation, paragraph breaks, and capitalization.
- Keep the speaker's own words, tone, and meaning. Do not summarize, expand, or add ideas.
- Never substitute a word you did not hear. If a term looks like a garbled proper noun, product name, or identifier, leave it exactly as transcribed. Never guess what it "should" have been.
- Keep technical terms, product names, and code identifiers exactly as transcribed, including spacing oddities.
- Never use em dashes. Use commas, parentheses, colons, or separate sentences.
- Output only the cleaned text. No preamble, no quotes, no commentary.`;

export function resolveCleanupModel(name) {
  return CLEANUP_MODELS[name] || CLEANUP_MODELS[DEFAULT_CLEANUP_MODEL];
}

export async function cleanupText(ai, text, { instruction, model, terms = [] } = {}) {
  if (!text || !text.trim()) return { text, cleaned: false };

  let system = SYSTEM;
  if (terms.length) {
    system += `\n\nThese terms are spelled correctly. Only correct a word to one of them when it is clearly the same word misheard: ${terms.join(', ')}.`;
  }
  if (instruction) {
    system += `\n\nAdditional instruction: ${instruction}`;
  }

  const messages = [
    { role: 'system', content: system },
    { role: 'user', content: text },
  ];

  const out = await ai.run(resolveCleanupModel(model), {
    messages,
    temperature: 0.1,
    max_tokens: 2048,
  });

  const result = (out?.choices?.[0]?.message?.content ?? out?.response ?? '').trim();
  return result ? { text: result, cleaned: true } : { text, cleaned: false };
}
