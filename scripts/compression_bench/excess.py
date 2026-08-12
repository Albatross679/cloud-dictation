"""What a window was billed for, against what that window sent.

Cloudflare bills more Workers AI inferences than the client issues. Measured on
2026-08-12 against this account and the deployed worker: 10 requests billed 10,
50 requests billed 52, and 50 requests sent alongside 150 others across four
models billed 59. Every request returned 200, the worker makes exactly one
`env.AI.run` call per request, and neither the probes nor the controlled tests
retry, so the excess is a property of the platform and it grows with load.

Both probes therefore settle on the bill having finished arriving rather than on
the two counts being equal, which is not reachable. The excess each window
carries is a measured property and is recorded per model in that window's result.

A window whose excess is far above what the platform produces is read as foreign
traffic having entered the window rather than as a larger platform excess, and is
marked for re-measurement instead of being averaged in.

The excess is compared across compression speeds too. P1's whole result is the
ratio of billed seconds at one speed to another, so an excess rate that differs
between speeds biases that ratio directly, and the ratio may not be published
until the rates are shown to be close.
"""

import config as cfg


def rows(sent, billed):
    """One row per model: what it sent, what it was billed, and the difference."""
    return [
        {"model": model_key,
         "requests_sent": sent[model_key],
         "requests_billed": billed.get(model_key, 0),
         "delta": billed.get(model_key, 0) - sent[model_key]}
        for model_key in sorted(sent)
    ]


def undercounts(model_rows):
    """Rows billed for less than they sent, which means the bill is still arriving."""
    return [row for row in model_rows if row["delta"] < 0]


def totals(model_rows):
    """Requests sent and requests billed, summed over the models."""
    return (sum(row["requests_sent"] for row in model_rows),
            sum(row["requests_billed"] for row in model_rows))


def rate(model_rows):
    """Billed minus sent as a share of sent, or None when nothing was sent."""
    sent, billed = totals(model_rows)
    if not sent:
        return None
    return (billed - sent) / sent


def model_rate(row):
    """One model's excess as a share of what it sent, or None when it sent nothing."""
    if not row["requests_sent"]:
        return None
    return row["delta"] / row["requests_sent"]


def implausible(model_rows, limit=None):
    """Rows whose excess is above what the platform has been measured to produce.

    The limit is a share of what the model sent. A row over it is treated as
    another source's traffic inside this window, which is what the quiet-window
    rule exists to keep out, rather than as a platform excess.
    """
    ceiling = cfg.EXCESS_IMPLAUSIBLE_ABOVE if limit is None else limit
    flagged = []
    for row in model_rows:
        share = model_rate(row)
        if share is not None and share > ceiling:
            flagged.append(row)
    return flagged


def describe(model_rows):
    """Per-model counts and difference, as one line the operator can read."""
    return ", ".join(
        f"{row['model']} sent {row['requests_sent']} billed {row['requests_billed']} "
        f"({row['delta']:+d})"
        for row in model_rows
    )


def describe_rate(model_rows):
    """A window's excess as a count and a share, for the settle's own output."""
    sent, billed = totals(model_rows)
    share = rate(model_rows)
    if share is None:
        return "nothing was sent"
    return f"billed {billed} for {sent} sent, {share:+.1%}"


def record_rows(record):
    """The per-model rows of a checkpoint, in this module's shape.

    Read from the counts the record itself carries, so a window written before the
    excess was recorded is classified the same way as one written after.
    """
    model_rows = []
    for row in record.get("models") or ():
        sent = row.get("requests_sent")
        billed = row.get("requests_billed")
        if sent is None or billed is None:
            continue
        model_rows.append({"model": row.get("model"), "requests_sent": sent,
                           "requests_billed": billed, "delta": billed - sent})
    return model_rows


def by_speed(windows, baseline=None):
    """Excess totals per compression speed, over windows that carry a speed.

    Keyed by the speed formatted the way the result files format one, so the
    numbers survive the trip through JSON.
    """
    grouped = {}
    for window in windows:
        speed = window.get("speed")
        if speed is None:
            continue
        key = f"{speed:g}"
        bucket = grouped.setdefault(key, {"speed": speed, "requests_sent": 0,
                                          "requests_billed": 0, "windows": 0})
        for row in record_rows(window):
            bucket["requests_sent"] += row["requests_sent"]
            bucket["requests_billed"] += row["requests_billed"]
        bucket["windows"] += 1
    for bucket in grouped.values():
        sent = bucket["requests_sent"]
        bucket["excess"] = bucket["requests_billed"] - sent
        bucket["excess_rate"] = (bucket["excess"] / sent) if sent else None
    return grouped


def compare_speeds(windows, baseline, budget=None):
    """Whether the excess rate is close enough between speeds for the ratio to mean anything.

    P1 reports billed seconds at one speed as a share of billed seconds at the
    baseline. Requests the client never issued are billed with their own seconds
    attached, so an excess rate that differs between the two speeds moves that
    share by about the difference. The budget is therefore the same figure the
    proportionality verdict is held to, and a spread above it makes the ratio
    untrustworthy rather than merely noisy.
    """
    allowed = cfg.EXCESS_SPREAD_BUDGET if budget is None else budget
    per_speed = by_speed(windows)
    base_key = f"{baseline:g}"
    base = per_speed.get(base_key)
    spreads = {}
    for key, bucket in per_speed.items():
        if key == base_key or base is None:
            continue
        if bucket["excess_rate"] is None or base["excess_rate"] is None:
            spreads[key] = None
            continue
        spreads[key] = bucket["excess_rate"] - base["excess_rate"]
    comparable = (base is not None and base["excess_rate"] is not None
                  and bool(spreads)
                  and all(s is not None and abs(s) <= allowed for s in spreads.values()))
    return {
        "baseline_speed": baseline,
        "budget": allowed,
        "per_speed": per_speed,
        "spread_against_baseline": spreads,
        "comparable": comparable,
        "statement": compare_statement(baseline, per_speed, spreads, comparable, allowed),
    }


def compare_statement(baseline, per_speed, spreads, comparable, allowed):
    """The comparison in one sentence, for the probe's output and the report."""
    base_key = f"{baseline:g}"
    base = per_speed.get(base_key)
    if base is None or base["excess_rate"] is None or not spreads:
        return ("The excess rate could not be compared across speeds, so the billing ratio "
                "is not trustworthy.")
    parts = ", ".join(
        f"{key}x {per_speed[key]['excess_rate']:+.1%}"
        for key in sorted(spreads, key=float)
        if per_speed[key]["excess_rate"] is not None
    )
    worst = max((abs(s) for s in spreads.values() if s is not None), default=None)
    head = f"Billing excess by speed: {base_key}x {base['excess_rate']:+.1%}, {parts}."
    if worst is None:
        return f"{head} The rates could not be compared, so the billing ratio is not trustworthy."
    if comparable:
        return (f"{head} They differ by at most {worst:.1%}, inside the {allowed:.0%} the ratio "
                f"is held to, so the ratio between speeds is not biased by the excess.")
    return (f"{head} They differ by {worst:.1%}, above the {allowed:.0%} the ratio is held to, "
            f"so the excess biases the ratio between speeds and no ratio is published from "
            f"these windows.")
