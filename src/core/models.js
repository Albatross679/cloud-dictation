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
    notes: 'Lowest latency, punctuation and diarization, accurate on long audio. No ASR-level vocabulary boosting on Cloudflare.',
    options: ({ language, diarize, dictation, entities, keyterms }) => ({
      punctuate: true,
      smart_format: true,
      numerals: true,
      language: language === 'auto' ? undefined : language,
      detect_language: language === 'auto' || undefined,
      diarize: diarize || undefined,
      dictation: dictation || undefined,
      detect_entities: entities || undefined,
    }),
    supportsVocabulary: false,
    readText: (r) => r?.results?.channels?.[0]?.alternatives?.[0]?.transcript,
  },

  'whisper-turbo': {
    id: '@cf/openai/whisper-large-v3-turbo',
    audio: AUDIO_BASE64,
    usdPerAudioMinute: 0.000513,
    freeAudioMinutesPerDay: 214,
    label: 'Whisper large-v3-turbo',
    notes: 'Cheapest. Drops content on audio longer than roughly a minute.',
    options: ({ language, keyterms }) => ({
      task: 'transcribe',
      language: language === 'auto' ? undefined : language,
      vad_filter: true,
      initial_prompt: keyterms.length ? `Vocabulary: ${keyterms.join(', ')}.` : undefined,
    }),
    supportsVocabulary: true,
    readText: (r) => r?.text ?? r?.transcription_info?.text,
  },

  whisper: {
    id: '@cf/openai/whisper',
    audio: AUDIO_BYTE_ARRAY,
    usdPerAudioMinute: 0.000453,
    freeAudioMinutesPerDay: 243,
    label: 'Whisper (base)',
    notes: 'Weakest on proper nouns and technical terms.',
    options: () => ({}),
    supportsVocabulary: false,
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
    supportsVocabulary: Boolean(m.supportsVocabulary),
    default: key === DEFAULT_MODEL,
  }));
}
