import { resolveModel, buildAudioInput, DEFAULT_MODEL } from '../core/models.js';
import { cleanupText } from '../core/cleanup.js';
import { audioSeconds, neuronsFor, utcDay } from '../core/usage.js';
import { parseTerms } from '../core/terms.js';
import { languageMismatch } from '../core/language.js';

const MAX_BYTES = 25 * 1024 * 1024;

export async function handleTranscribe(request, env, ctx) {
  const url = new URL(request.url);
  const modelKey = url.searchParams.get('model') || env.DEFAULT_MODEL || DEFAULT_MODEL;
  const model = resolveModel(modelKey);

  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.length === 0) {
    return Response.json({ error: 'empty audio body' }, { status: 400 });
  }
  if (bytes.length > MAX_BYTES) {
    return Response.json(
      { error: `audio too large: ${bytes.length} bytes, limit ${MAX_BYTES}` },
      { status: 413 },
    );
  }

  const contentType = request.headers.get('content-type') || 'audio/wav';
  const started = Date.now();

  const vocabulary = (url.searchParams.get('vocabulary') || url.searchParams.get('initial_prompt') || env.VOCABULARY || '').trim();
  const language = url.searchParams.get('language') || 'auto';
  const terms = parseTerms(vocabulary);

  if (language !== 'auto' && Array.isArray(model.languages) && !model.languages.includes(language)) {
    return Response.json(
      {
        error: `${modelKey} does not support language "${language}"`,
        supported_languages: model.languages,
        model: modelKey,
      },
      { status: 400 },
    );
  }

  let raw;
  try {
    raw = await env.AI.run(model.id, {
      ...(await buildAudioInput(model, bytes, contentType)),
      ...model.options({
        language,
        dictation: url.searchParams.get('dictation') === '1',
        terms,
      }),
    });
  } catch (err) {
    return Response.json(
      { error: `transcription failed: ${err}`, model: modelKey, ms: Date.now() - started },
      { status: 502 },
    );
  }

  const transcript = (model.readText(raw) || '').trim();
  const transcribeMs = Date.now() - started;

  const seconds = audioSeconds(raw, bytes);
  const neurons = neuronsFor(modelKey, seconds);
  const record = env.USAGE.get(env.USAGE.idFromName('global')).fetch('https://usage/record', {
    method: 'POST',
    body: JSON.stringify({ day: utcDay(Date.now()), model: modelKey, seconds, neurons }),
  });
  if (ctx) ctx.waitUntil(record);
  else await record;

  let text = transcript;
  let cleaned = false;
  let cleanupMs = 0;
  let cleanupError;

  if (url.searchParams.get('cleanup') === '1' && transcript) {
    const t0 = Date.now();
    try {
      const r = await cleanupText(env.AI, transcript, {
        instruction: url.searchParams.get('instruction'),
        model: url.searchParams.get('cleanup_model') || env.DEFAULT_CLEANUP_MODEL,
        terms,
      });
      text = r.text;
      cleaned = r.cleaned;
    } catch (err) {
      cleanupError = String(err);
    }
    cleanupMs = Date.now() - t0;
  }

  return Response.json({
    text,
    raw_transcript: cleaned ? transcript : undefined,
    model: modelKey,
    cleaned,
    bytes: bytes.length,
    language_mismatch: languageMismatch(text, language) || undefined,
    model_honors_language: model.supportsLanguage ?? false,
    terms_applied: model.supportsVocabulary && !(modelKey === 'nova-3' && language === 'auto') ? terms : [],
    terms_skipped_reason:
      terms.length && modelKey === 'nova-3' && language === 'auto'
        ? 'Nova-3 rejects keyterm when the language is auto-detected. Pin a language to boost vocabulary.'
        : undefined,
    audio_seconds: seconds,
    neurons: neurons,
    transcribe_ms: transcribeMs,
    cleanup_ms: cleanupMs || undefined,
    cleanup_error: cleanupError,
  });
}
