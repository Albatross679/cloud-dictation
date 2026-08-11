/// Temporary diagnostic: does Nova-3 keyterm prompting change the transcript?
/// A/B on the same audio, terms supplied via the `terms` query parameter.
export async function handleProbe(request, env) {
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (!bytes.length) return Response.json({ error: 'post audio bytes' }, { status: 400 });

  const url = new URL(request.url);
  const contentType = request.headers.get('content-type') || 'audio/wav';
  const terms = (url.searchParams.get('terms') || '')
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);

  const run = async (keyterm) => {
    const started = Date.now();
    const out = await env.AI.run('@cf/deepgram/nova-3', {
      audio: { body: new Response(bytes).body, contentType },
      punctuate: true,
      smart_format: true,
      ...(keyterm.length ? { keyterm } : {}),
    });
    return {
      ms: Date.now() - started,
      text: out?.results?.channels?.[0]?.alternatives?.[0]?.transcript,
    };
  };

  const without = await run([]);
  const with_ = await run(terms);

  return Response.json({
    terms,
    without_keyterm: without,
    with_keyterm: with_,
    changed: without.text !== with_.text,
  });
}
