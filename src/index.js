import { authorize } from './api/auth.js';
import { handleTranscribe } from './api/transcribe.js';
import { catalog } from './core/models.js';
import { CLEANUP_MODELS, DEFAULT_CLEANUP_MODEL } from './core/cleanup.js';

export default {
  async fetch(request, env) {
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
      });
    }

    if (url.pathname === '/transcribe' && request.method === 'POST') {
      return handleTranscribe(request, env);
    }

    return Response.json({ error: 'not found' }, { status: 404 });
  },
};
