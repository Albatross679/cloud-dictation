"""The model catalogue the deployed worker publishes at GET /models.

Every per-model figure the cost arithmetic needs comes from that response: the
price per audio minute, the free minutes a day, and the `@cf/...` id the billing
analytics reports under. Nothing here is stored in the harness, so the numbers
cannot drift away from the app that serves them.

The endpoint is free and sends no audio. A run fetches it once, dry or live, and
writes the response to a cache under runs/ with the time it was fetched. When
the worker cannot be reached the cache is used and the run says so, in its own
output and in the report. When there is no cache either, the run stops: a
built-in default is the staleness this reads around.
"""

import json
from datetime import datetime, timezone

import requests

FETCH_TIMEOUT_S = 30

# Fields a catalogue entry has to carry for the harness to cost a model.
REQUIRED_FIELDS = ("key", "model", "usdPerAudioMinute", "freeAudioMinutesPerDay")


class Catalogue:
    """One /models response, with where it came from and when it was fetched."""

    def __init__(self, models, fetched_at, worker, from_cache=False, cache_path=None,
                 unreachable=None):
        self.models = models
        self.fetched_at = fetched_at
        self.worker = worker
        self.from_cache = from_cache
        self.cache_path = cache_path
        self.unreachable = unreachable

    def entry(self, model_key):
        if model_key not in self.models:
            raise SystemExit(
                f"the worker's catalogue has no model {model_key!r}; "
                f"it publishes {', '.join(sorted(self.models))}"
            )
        return self.models[model_key]

    def usd_per_audio_minute(self, model_key):
        return float(self.entry(model_key)["usdPerAudioMinute"])

    def free_audio_minutes_per_day(self, model_key):
        return float(self.entry(model_key)["freeAudioMinutesPerDay"])

    def model_id(self, model_key):
        """The `@cf/...` id billing analytics reports this model under."""
        return self.entry(model_key)["model"]

    def key_for_model_id(self, model_id):
        """The worker's short key for an analytics id, or the id when it is unknown."""
        for key, entry in self.models.items():
            if entry["model"] == model_id:
                return key
        return model_id

    def provenance(self):
        """One line naming the source of the rates and how old they are."""
        if not self.from_cache:
            return f"model catalogue fetched from {self.worker}/models at {self.fetched_at}"
        return (f"STALE: model catalogue read from the cache {self.cache_path}, "
                f"fetched {self.fetched_at}, because the worker could not be reached "
                f"({self.unreachable}). Every rate below is as old as that timestamp.")


def fetch(worker, token, timeout=FETCH_TIMEOUT_S):
    """The catalogue as the worker publishes it right now, keyed by model key."""
    response = requests.get(
        f"{worker}/models",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse(response.json())


def parse(body):
    """A /models body folded onto model keys, refusing an entry missing a figure."""
    published = body.get("models")
    if not published:
        raise ValueError("the worker's /models response carries no models")
    models = {}
    for entry in published:
        missing = [f for f in REQUIRED_FIELDS if entry.get(f) is None]
        if missing:
            raise ValueError(
                f"the worker's catalogue entry {entry.get('key', entry)!r} is missing "
                f"{', '.join(missing)}"
            )
        models[entry["key"]] = entry
    return models


def write_cache(path, models, worker, fetched_at):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fetched_at": fetched_at,
        "worker": worker,
        "models": list(models.values()),
    }, indent=2))


def read_cache(path):
    """The cached catalogue and the time it was fetched, or None when there is none."""
    if not path.exists():
        return None
    body = json.loads(path.read_text())
    return parse(body), body.get("fetched_at"), body.get("worker")


def reason(err):
    """A short printable reason a fetch failed, for a run's output and the report."""
    text = " ".join(str(err).split())
    if len(text) > 120:
        text = text[:117] + "..."
    return f"{type(err).__name__}: {text}"


def load(worker, token, cache_path, timeout=FETCH_TIMEOUT_S):
    """The catalogue, from the worker when it answers and from the cache when it does not."""
    if worker and token:
        try:
            models = fetch(worker, token, timeout)
        except Exception as err:
            unreachable = reason(err)
        else:
            fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            write_cache(cache_path, models, worker, fetched_at)
            return Catalogue(models, fetched_at, worker)
    else:
        unreachable = ("CLOUD_DICTATION_WORKER and CLOUD_DICTATION_TOKEN are not both set, "
                       "so the worker was not asked")

    cached = read_cache(cache_path)
    if cached is None:
        raise SystemExit(
            "cannot read the model catalogue: the worker was not reachable "
            f"({unreachable}) and there is no cache at {cache_path}. "
            "Set CLOUD_DICTATION_WORKER to the deployed worker's base URL and "
            "CLOUD_DICTATION_TOKEN to its auth token, then run again. "
            "There are no built-in rates to fall back on."
        )
    models, fetched_at, cached_worker = cached
    return Catalogue(models, fetched_at, worker or cached_worker, from_cache=True,
                     cache_path=cache_path, unreachable=unreachable)
