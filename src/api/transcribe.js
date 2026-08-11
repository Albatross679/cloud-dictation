import { resolveModel, buildAudioInput, DEFAULT_MODEL } from '../core/models.js';
import { cleanupText } from '../core/cleanup.js';

const MAX_BYTES = 25 * 1024 * 1024;

export async function handleTranscribe(request, env) {
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

  const keyterms = [url.searchParams.get('keyterms'), env.KEYTERMS]
    .filter(Boolean)
    .flatMap((s) => s.split(','))
    .map((s) => s.trim())
    .filter(Boolean);

  let raw;
  try {
    raw = await env.AI.run(model.id, {
      ...(await buildAudioInput(model, bytes, contentType)),
      ...model.options({
        language: url.searchParams.get('language') || 'auto',
        diarize: url.searchParams.get('diarize') === '1',
        dictation: url.searchParams.get('dictation') === '1',
        entities: url.searchParams.get('entities') === '1',
        keyterms,
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
        keyterms,
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
    transcribe_ms: transcribeMs,
    cleanup_ms: cleanupMs || undefined,
    cleanup_error: cleanupError,
  });
}
