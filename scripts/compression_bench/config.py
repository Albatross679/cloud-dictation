"""Settled configuration for the audio compression benchmark.

Every stage reads its parameters from here, so the experiment's scope lives in
one file and the run is reproducible from it.
"""

import os
from pathlib import Path

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

# Model keys as the worker's /transcribe endpoint accepts them, with the
# published neuron rate per audio minute used for the cost arithmetic and the
# Cloudflare model id that billing analytics reports under.
MODELS = {
    "nova-3": {
        "label": "Deepgram Nova-3",
        "model_id": "@cf/deepgram/nova-3",
        "neurons_per_audio_minute": 472.73,
        "honors_language": True,
    },
    "whisper-turbo": {
        "label": "Whisper large-v3-turbo",
        "model_id": "@cf/openai/whisper-large-v3-turbo",
        "neurons_per_audio_minute": 46.63,
        "honors_language": True,
    },
    "whisper": {
        "label": "Whisper (base)",
        "model_id": "@cf/openai/whisper",
        "neurons_per_audio_minute": 41.14,
        "honors_language": False,
    },
    "whisper-tiny-en": {
        "label": "Whisper tiny (English)",
        "model_id": "@cf/openai/whisper-tiny-en",
        "neurons_per_audio_minute": 0.604,
        "honors_language": False,
    },
}

MODEL_BY_ID = {model["model_id"]: key for key, model in MODELS.items()}

USD_PER_1000_NEURONS = 0.011
FREE_NEURONS_PER_DAY = 10_000

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


def usd_per_hour(model_key: str, speed: float) -> float:
    """Cost of one hour of real speech at a compression factor."""
    rate = MODELS[model_key]["neurons_per_audio_minute"]
    return (rate / speed) * 60 * USD_PER_1000_NEURONS / 1000


def free_minutes_per_day(model_key: str, speed: float) -> float:
    """Minutes of real speech per day that stay inside the free allowance."""
    rate = MODELS[model_key]["neurons_per_audio_minute"]
    return FREE_NEURONS_PER_DAY / rate * speed


def worker_url() -> str:
    """Worker base URL from CLOUD_DICTATION_WORKER."""
    url = os.environ.get("CLOUD_DICTATION_WORKER", "").strip().rstrip("/")
    if not url:
        raise SystemExit("set CLOUD_DICTATION_WORKER to the deployed worker URL")
    return url


def auth_token() -> str:
    """Auth token from CLOUD_DICTATION_TOKEN, falling back to .auth-token.local."""
    token = os.environ.get("CLOUD_DICTATION_TOKEN", "").strip()
    if token:
        return token
    local = REPO / ".auth-token.local"
    if local.exists():
        return local.read_text().strip()
    raise SystemExit("set CLOUD_DICTATION_TOKEN or create .auth-token.local")


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
