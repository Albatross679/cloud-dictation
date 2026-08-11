import { authorize } from './api/auth.js';
import { handleTranscribe } from './api/transcribe.js';
import { catalog } from './core/models.js';
import { CLEANUP_MODELS, DEFAULT_CLEANUP_MODEL } from './core/cleanup.js';
import { UsageCounter } from './api/usage_counter.js';
import { rateCard } from './core/usage.js';

export { UsageCounter };

function usageStub(env) {
  return env.USAGE.get(env.USAGE.idFromName('global'));
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === '/health') {
      return Response.json({ ok: true });
    }

    const denied = authorize(request, env);
    if (denied) return denied;

    if (url.pathname === '/models') {
      return Response.json({
        models: catalog(),
        cleanup_models: Object.keys(CLEANUP_MODELS),
        default_cleanup_model: DEFAULT_CLEANUP_MODEL,
        rates: rateCard(),
      });
    }

    if (url.pathname === '/usage') {
      return usageStub(env).fetch('https://usage/summary');
    }

    if (url.pathname === '/usage/reset' && request.method === 'POST') {
      return usageStub(env).fetch('https://usage/reset', { method: 'POST' });
    }

    if (url.pathname === '/transcribe' && request.method === 'POST') {
      return handleTranscribe(request, env, ctx);
    }

    return Response.json({ error: 'not found' }, { status: 404 });
  },
};
