export const FREE_NEURONS_PER_DAY = 10000;
export const USD_PER_1K_NEURONS = 0.011;

/// Neurons per audio minute, every value read back from billing analytics
/// rather than taken from the docs. Cloudflare publishes no price for
/// whisper-tiny-en; 0.604 is measured over 10.5 minutes of audio.
const NEURONS_PER_AUDIO_MINUTE = {
  'nova-3': 472.73,
  'whisper-turbo': 46.63,
  whisper: 41.14,
  'whisper-tiny-en': 0.604,
};

/// Duration in seconds, preferring what the model reports, then the last word
/// timestamp, then the WAV header. The desktop client always uploads 16 kHz PCM
/// WAV, so one of the three always resolves. Returns 0 for a compressed file
/// whose model reported no timings, which undercounts rather than guessing.
export function audioSeconds(raw, bytes) {
  const reported =
    raw?.transcription_info?.duration ??
    raw?.metadata?.duration ??
    raw?.results?.metadata?.duration;
  if (typeof reported === 'number' && reported > 0) return reported;

  // Whisper base and tiny report no duration, but their word timings end at
  // the last spoken word, which is within a breath of the real length.
  const lastWord = raw?.words?.[raw.words.length - 1]?.end;
  if (typeof lastWord === 'number' && lastWord > 0) return lastWord;

  const fromHeader = wavSeconds(bytes);
  if (fromHeader) return fromHeader;

  return 0;
}

function wavSeconds(bytes) {
  if (bytes.length < 44) return 0;
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (dv.getUint32(0, false) !== 0x52494646) return 0; // "RIFF"
  if (dv.getUint32(8, false) !== 0x57415645) return 0; // "WAVE"

  let offset = 12;
  let byteRate = 0;
  while (offset + 8 <= bytes.length) {
    const id = dv.getUint32(offset, false);
    const size = dv.getUint32(offset + 4, true);
    if (id === 0x666d7420 && offset + 8 + 16 <= bytes.length) {
      byteRate = dv.getUint32(offset + 16, true); // "fmt "
    } else if (id === 0x64617461) {
      return byteRate > 0 ? size / byteRate : 0; // "data"
    }
    offset += 8 + size + (size % 2);
  }
  return 0;
}

export function neuronsFor(modelKey, seconds) {
  const rate = NEURONS_PER_AUDIO_MINUTE[modelKey];
  if (!rate || !seconds) return 0;
  return (seconds / 60) * rate;
}

export function usdFor(neurons) {
  return (neurons / 1000) * USD_PER_1K_NEURONS;
}


export function utcDay(nowMs) {
  return new Date(nowMs).toISOString().slice(0, 10);
}
