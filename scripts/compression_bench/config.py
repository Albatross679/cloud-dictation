"""Settled configuration for the audio compression benchmark.

Every stage reads its parameters from here, so the experiment's scope lives in
one file and the run is reproducible from it. The billing figures are the
exception: cost, free-tier reach and analytics ids are read from the deployed
worker's catalogue at run time, through catalogue().
"""

import os
from pathlib import Path

import catalogue as catalogue_source

REPO = Path(__file__).resolve().parents[2]
RUN_DIR = REPO / "runs" / "compression-bench"

CORPUS_DIR = RUN_DIR / "corpus"
AUDIO_DIR = RUN_DIR / "audio"
PROBE_DIR = RUN_DIR / "probes"
MANIFEST = CORPUS_DIR / "manifest.jsonl"
VARIANTS = AUDIO_DIR / "variants.jsonl"
# Stages 3 to 5 write one set of artifacts per mode. A dry run and a live run
# name different files, so a resume log, a results file and a report each hold
# one kind of response and the two can never be blended.
RESPONSES = RUN_DIR / "responses.jsonl"
RESULTS = RUN_DIR / "results.json"
REPORT = RUN_DIR / "report.html"
DRY_RUN_RESPONSES = RUN_DIR / "responses.dry-run.jsonl"
DRY_RUN_RESULTS = RUN_DIR / "results.dry-run.json"
DRY_RUN_REPORT = RUN_DIR / "report.dry-run.html"
BILLING_PROBE_RESULT = PROBE_DIR / "billing.json"
SILENCE_PROBE_RESULT = PROBE_DIR / "silence.json"
# The worker's model catalogue as last fetched, so a run works while the worker
# is unreachable and can say how old the rates it used are.
CATALOGUE_CACHE = RUN_DIR / "models-catalogue.json"

# LibriSpeech test-clean. Read audiobook speech, studio-clean, 40 speakers.
CORPUS_URL = "https://www.openslr.org/resources/12/test-clean.tar.gz"
CORPUS_ARCHIVE_MB = 346
CORPUS_ROOT = CORPUS_DIR / "LibriSpeech" / "test-clean"

SAMPLE_SIZE = 350
SAMPLE_SEED = 20260811
MAX_UTTERANCE_SECONDS = 30.0

SAMPLE_RATE = 16000

# Compression factors. 1.0 is the baseline every other speed is measured against.
SPEEDS = [1.0, 1.5, 2.0, 2.5, 3.0]
BASELINE_SPEED = 1.0

# Model keys as the worker's /transcribe endpoint accepts them, with the two
# facts the benchmark holds about each one: how to name it in the report, and
# whether pinning a language changes what it does. Every billing figure comes
# from the worker's catalogue instead, through catalogue() below.
MODELS = {
    "nova-3": {
        "label": "Deepgram Nova-3",
        "honors_language": True,
    },
    "whisper-turbo": {
        "label": "Whisper large-v3-turbo",
        "honors_language": True,
    },
    "whisper": {
        "label": "Whisper (base)",
        "honors_language": False,
    },
    "whisper-tiny-en": {
        "label": "Whisper tiny (English)",
        "honors_language": False,
    },
}

LANGUAGE = "en"
CLEANUP = 0
VOCABULARY = ""

# Request pacing against the worker.
CONCURRENCY = 6
REQUEST_TIMEOUT_S = 300
MAX_ATTEMPTS = 4
RETRY_BACKOFF_S = 2.0

# Acceptance rule. A speed is recommended for a model when its error increase
# stays inside DELTA_WER_BUDGET, the upper bound of the interval stays under
# DELTA_WER_CI_CEILING, and the catastrophic rate rises by at most
# CATASTROPHIC_BUDGET over that model's own baseline.
DELTA_WER_BUDGET = 1.0
DELTA_WER_CI_CEILING = 2.0
CATASTROPHIC_BUDGET = 0.5

# An utterance counts as catastrophic above this error rate, or when the
# transcript collapses into a repeating fragment.
CATASTROPHIC_WER = 0.50
REPETITION_MIN_REPEATS = 3
REPETITION_NGRAM = 3

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20260811

# Transcripts kept per model for the failure gallery, worst error first.
GALLERY_EXAMPLES = 3

# Cloudflare billing analytics. Both probes read the account-level
# aiInferenceAdaptiveGroups dataset at minute resolution, and totalAudioSeconds
# is what they measure: it is the billed quantity itself rather than a figure
# derived from neurons.
#
# The window is isolated by three filters together. Traffic arriving through a
# Worker's AI binding, which is how cloud-dictation transcribes, reports
# requestSource "unknown", while direct REST calls report "rest api". Combined
# with the four speech model ids and the minute range, that separates a batch
# from the rest of the account's AI traffic. The tag dimension is empty on every
# record in this account and cannot be used for this.
GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
REQUEST_SOURCE = "unknown"
ANALYTICS_QUERY = """
query BilledInference($account: String!, $start: Time!, $end: Time!,
                      $models: [string!], $source: string) {
  viewer {
    accounts(filter: {accountTag: $account}) {
      aiInferenceAdaptiveGroups(
        limit: 1000
        filter: {
          datetimeMinute_geq: $start
          datetimeMinute_lt: $end
          modelId_in: $models
          requestSource: $source
        }
        orderBy: [modelId_ASC]
      ) {
        count
        dimensions { modelId }
        sum { totalAudioSeconds totalNeurons totalInferenceTimeMs }
      }
    }
  }
}
"""

# Analytics settle after a lag that is measured rather than assumed: a window is
# polled at minute granularity until consecutive reads agree.
ANALYTICS_POLL_INTERVAL_S = 60
ANALYTICS_SETTLE_AGREEMENTS = 2
ANALYTICS_SETTLE_TIMEOUT_S = 1800

# Probe P1 asks whether billed audio seconds fall proportionally when the same
# speech is time-compressed. One batch per window: per-clip deltas are
# unreadable against a settling lag of minutes, so the batch is the delta. Every
# clip is under MAX_UTTERANCE_SECONDS, which keeps the test on the short-clip
# stratum the benchmark itself runs on.
BILLING_PROBE_UTTERANCES = 200
BILLING_PROBE_REPLICATES = 3
BILLING_PROBE_SPEEDS = [1.0, 3.0]

# Probe P2 holds speech content fixed and varies the silence around it.
SILENCE_PROBE_PADDING_S = [0, 2, 4, 8, 16]
SILENCE_PROBE_SPEECH_S = 62.0
SILENCE_PROBE_REPEATS = 3


def responses_path(dry_run: bool) -> Path:
    """Resume log for a mode."""
    return DRY_RUN_RESPONSES if dry_run else RESPONSES


def results_path(dry_run: bool) -> Path:
    """Scored results for a mode."""
    return DRY_RUN_RESULTS if dry_run else RESULTS


def report_path(dry_run: bool) -> Path:
    """Rendered report for a mode."""
    return DRY_RUN_REPORT if dry_run else REPORT


_catalogue = None


def catalogue() -> catalogue_source.Catalogue:
    """The worker's model catalogue, fetched once per process and announced once.

    Every stage that costs anything goes through here, so a run states where its
    rates came from before it prints a number derived from them.
    """
    global _catalogue
    if _catalogue is None:
        _catalogue = catalogue_source.load(
            os.environ.get("CLOUD_DICTATION_WORKER", "").strip().rstrip("/"),
            os.environ.get("CLOUD_DICTATION_TOKEN", "").strip(),
            CATALOGUE_CACHE,
        )
        print(_catalogue.provenance())
    return _catalogue


def usd_per_audio_minute(model_key: str) -> float:
    """Published cost of one minute of audio as the worker receives it."""
    return catalogue().usd_per_audio_minute(model_key)


def model_id(model_key: str) -> str:
    """The `@cf/...` id billing analytics reports a model key under."""
    return catalogue().model_id(model_key)


def model_key_for_id(analytics_id: str) -> str:
    """The worker's short key for an analytics id, or the id when it is unknown."""
    return catalogue().key_for_model_id(analytics_id)


def usd_per_hour(model_key: str, speed: float) -> float:
    """Cost of one hour of real speech at a compression factor."""
    return usd_per_audio_minute(model_key) / speed * 60


def free_minutes_per_day(model_key: str, speed: float) -> float:
    """Minutes of real speech per day that stay inside the free allowance."""
    return catalogue().free_audio_minutes_per_day(model_key) * speed


def worker_url() -> str:
    """Worker base URL from CLOUD_DICTATION_WORKER."""
    url = os.environ.get("CLOUD_DICTATION_WORKER", "").strip().rstrip("/")
    if not url:
        raise SystemExit("set CLOUD_DICTATION_WORKER to the deployed worker URL")
    return url


def auth_token() -> str:
    """Auth token from CLOUD_DICTATION_TOKEN, the only place it is read from."""
    token = os.environ.get("CLOUD_DICTATION_TOKEN", "").strip()
    if not token:
        raise SystemExit("set CLOUD_DICTATION_TOKEN to the worker's auth token")
    return token


def cloudflare_account() -> str:
    """Account tag the billing analytics query is scoped to."""
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CF_ACCOUNT_ID"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise SystemExit("set CLOUDFLARE_ACCOUNT_ID to the account that owns the worker")


def cloudflare_token() -> str:
    """API token with Account Analytics read, used only for the GraphQL query."""
    for name in ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise SystemExit("set CLOUDFLARE_API_TOKEN to a token that can read account analytics")
