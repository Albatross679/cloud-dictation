function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function authorize(request, env) {
  const expected = env.AUTH_TOKEN;
  if (!expected) {
    return Response.json(
      { error: 'server misconfigured: AUTH_TOKEN secret is not set' },
      { status: 500 },
    );
  }

  const header = request.headers.get('authorization') || '';
  const presented = header.startsWith('Bearer ') ? header.slice(7) : '';

  if (!presented || !timingSafeEqual(presented, expected)) {
    return Response.json({ error: 'unauthorized' }, { status: 401 });
  }

  return null;
}
