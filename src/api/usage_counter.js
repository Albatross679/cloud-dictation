import { FREE_NEURONS_PER_DAY, usdFor, utcDay } from '../core/usage.js';

/// Single-instance tally of what this worker has spent. A Durable Object
/// rather than KV so concurrent dictations cannot lose an increment.
export class UsageCounter {
  constructor(state) {
    this.state = state;
    this.sql = state.storage.sql;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS usage (
        day TEXT NOT NULL,
        model TEXT NOT NULL,
        requests INTEGER NOT NULL DEFAULT 0,
        seconds REAL NOT NULL DEFAULT 0,
        neurons REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (day, model)
      )
    `);
  }

  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === '/record' && request.method === 'POST') {
      const { day, model, seconds, neurons } = await request.json();
      this.sql.exec(
        `INSERT INTO usage (day, model, requests, seconds, neurons)
         VALUES (?, ?, 1, ?, ?)
         ON CONFLICT (day, model) DO UPDATE SET
           requests = requests + 1,
           seconds = seconds + excluded.seconds,
           neurons = neurons + excluded.neurons`,
        day,
        model,
        seconds,
        neurons,
      );
      return Response.json({ ok: true });
    }

    if (url.pathname === '/summary') {
      const today = url.searchParams.get('day') || utcDay(Date.now());
      return Response.json(this.summary(today));
    }

    if (url.pathname === '/reset' && request.method === 'POST') {
      this.sql.exec('DELETE FROM usage');
      return Response.json({ ok: true });
    }

    return new Response('not found', { status: 404 });
  }

  summary(today) {
    const rows = [...this.sql.exec('SELECT * FROM usage ORDER BY day DESC').raw()];
    const cols = ['day', 'model', 'requests', 'seconds', 'neurons'];
    const all = rows.map((r) => Object.fromEntries(cols.map((c, i) => [c, r[i]])));

    const todayRows = all.filter((r) => r.day === today);
    const sum = (list, key) => list.reduce((a, r) => a + (r[key] || 0), 0);

    const todayNeurons = sum(todayRows, 'neurons');
    const byDay = {};
    for (const r of all) {
      byDay[r.day] ??= { day: r.day, requests: 0, seconds: 0, neurons: 0 };
      byDay[r.day].requests += r.requests;
      byDay[r.day].seconds += r.seconds;
      byDay[r.day].neurons += r.neurons;
    }

    return {
      day: today,
      free_neurons_per_day: FREE_NEURONS_PER_DAY,
      today: {
        requests: sum(todayRows, 'requests'),
        audio_seconds: sum(todayRows, 'seconds'),
        neurons: todayNeurons,
        free_used_fraction: Math.min(1, todayNeurons / FREE_NEURONS_PER_DAY),
        neurons_remaining: Math.max(0, FREE_NEURONS_PER_DAY - todayNeurons),
        billable_usd: usdFor(Math.max(0, todayNeurons - FREE_NEURONS_PER_DAY)),
        by_model: todayRows.map((r) => ({
          model: r.model,
          requests: r.requests,
          audio_seconds: r.seconds,
          neurons: r.neurons,
        })),
      },
      history: Object.values(byDay)
        .sort((a, b) => (a.day < b.day ? 1 : -1))
        .slice(0, 30),
      all_time: {
        requests: sum(all, 'requests'),
        audio_seconds: sum(all, 'seconds'),
        neurons: sum(all, 'neurons'),
      },
    };
  }
}
