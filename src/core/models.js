import { termsAsWhisperPrompt } from './terms.js';

const AUDIO_STREAM = 'stream';
const AUDIO_BASE64 = 'base64';
const AUDIO_BYTE_ARRAY = 'byteArray';

export const MODELS = {
  'nova-3': {
    id: '@cf/deepgram/nova-3',
    audio: AUDIO_STREAM,
    usdPerAudioMinute: 0.0052,
    freeAudioMinutesPerDay: 21,
    label: 'Deepgram Nova-3',
    notes: 'Lowest latency, punctuation and casing, accurate on long audio. Boosts the vocabulary list, but only when a language is pinned.',
    options: ({ language, dictation, terms }) => ({
      punctuate: true,
      smart_format: true,
      numerals: true,
      language: language === 'auto' ? undefined : language,
      detect_language: language === 'auto' || undefined,
      dictation: dictation || undefined,
      // Language detection routes to the multilingual Nova-3, which rejects
      // keyterm outright, so boosting requires a pinned language.
      keyterm: language !== 'auto' && terms?.length ? terms : undefined,
    }),
    supportsVocabulary: true,
    supportsLanguage: true,
    // Probed against the live worker: every other code returns "No such
    // model/language/tier combination found", so offering one is a hard error
    // rather than a silent fallback.
    languages: ['en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'hi', 'ru', 'ja'],
    readText: (r) => r?.results?.channels?.[0]?.alternatives?.[0]?.transcript,
  },

  'whisper-turbo': {
    id: '@cf/openai/whisper-large-v3-turbo',
    audio: AUDIO_BASE64,
    usdPerAudioMinute: 0.000513,
    freeAudioMinutesPerDay: 214,
    label: 'Whisper large-v3-turbo',
    notes: 'Cheapest. Takes the vocabulary list as a decoder prompt. Drops content on audio longer than roughly a minute.',
    options: ({ language, terms }) => ({
      task: 'transcribe',
      language: language === 'auto' ? undefined : language,
      vad_filter: true,
      initial_prompt: termsAsWhisperPrompt(terms),
    }),
    supportsVocabulary: true,
    supportsLanguage: true,
    // Whisper's full multilingual range; null means the client may offer
    // everything it can display.
    languages: null,
    readText: (r) => r?.text ?? r?.transcription_info?.text,
  },

  whisper: {
    id: '@cf/openai/whisper',
    audio: AUDIO_BYTE_ARRAY,
    usdPerAudioMinute: 0.000453,
    freeAudioMinutesPerDay: 243,
    label: 'Whisper (base)',
    notes: 'Ignores the vocabulary list. Weakest on proper nouns. Cannot be pinned to a language: it accepts the language parameter and discards it, so short or noisy audio can come back in the wrong language.',
    options: () => ({}),
    supportsVocabulary: false,
    supportsLanguage: false,
    // Accepts the parameter and discards it, so auto is the only honest offer.
    languages: [],
    readText: (r) => r?.text,
  },

  'whisper-tiny-en': {
    id: '@cf/openai/whisper-tiny-en',
    audio: AUDIO_BYTE_ARRAY,
    // Cloudflare lists no price. Derived from a measured 0.604 neurons per
    // audio minute at $0.011 per 1000 neurons.
    usdPerAudioMinute: 0.0000066,
    freeAudioMinutesPerDay: 16556,
    label: 'Whisper tiny (English)',
    notes: 'Ignores the vocabulary list. Smallest, fastest and by far the cheapest: 0.604 neurons per audio minute, so the daily free tier covers over 270 hours. English only by construction.',
    options: () => ({}),
    supportsVocabulary: false,
    supportsLanguage: 'en-only',
    languages: ['en'],
    readText: (r) => r?.text,
  },
};

export const DEFAULT_MODEL = 'nova-3';

export function resolveModel(name) {
  return MODELS[name] || MODELS[DEFAULT_MODEL];
}

export async function buildAudioInput(model, bytes, contentType) {
  if (model.audio === AUDIO_STREAM) {
    return { audio: { body: new Response(bytes).body, contentType } };
  }
  if (model.audio === AUDIO_BYTE_ARRAY) {
    return { audio: [...bytes] };
  }
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return { audio: btoa(binary) };
}

export function catalog() {
  return Object.entries(MODELS).map(([key, m]) => ({
    key,
    label: m.label,
    model: m.id,
    usdPerAudioMinute: m.usdPerAudioMinute,
    freeAudioMinutesPerDay: m.freeAudioMinutesPerDay,
    notes: m.notes,
    supportsVocabulary: m.supportsVocabulary ?? false,
    supportsLanguage: m.supportsLanguage ?? false,
    languages: m.languages ?? null,
    default: key === DEFAULT_MODEL,
  }));
}
