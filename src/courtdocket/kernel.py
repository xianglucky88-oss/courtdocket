
"""Hearing-event interpretation for docket records.

Every source record is classified into a hearing event — 开庭通知
(scheduled), 延期 (postponed), 改期 (rescheduled), 撤诉 (withdrawn), 结案
(closed), 作废 (archived), 新收 (filed) or 未知 (unknown).  Classification
is a pure function of the single record, so a record's identity and
annotations never depend on which batch it arrived in or how often it was
re-reported.

The per-case 庭期结果 is computed on demand by :func:`schedule` from any
consistent set of stored notices, as of any point in time:

* notices are replayed in notice-time order, so an earlier-issued notice can
  never displace a later-issued one, no matter when it arrives (排期不倒退);
* terminal events (撤诉/结案/作废) are sticky — later scheduling notices are
  held and flagged instead of silently reviving the case;
* unknown or invalid notices are stored and flagged, never dropped, and
  never rewrite a known schedule;
* a missing record is just missing — absence is never read as 撤诉.
"""
import hashlib
import json

# raw status -> normalized lifecycle state (original values kept, wider vocabulary)
TRANS = {
    "new": "pending", "draft": "pending", "filed": "pending",
    "active": "active", "scheduled": "active", "hearing": "active",
    "pending": "pending", "postponed": "pending", "continued": "pending",
    "adjourned": "pending", "rescheduled": "pending",
    "sold": "closed", "closed": "closed", "decided": "closed",
    "withdrawn": "closed", "dismissed": "closed",
    "deleted": "archived", "archived": "archived", "vacated": "archived",
}

# normalized state -> progression rank (kept for backward compatibility)
ORDER = {"unknown": 0, "pending": 1, "active": 2, "closed": 3, "archived": 4}

# raw status -> hearing event type (庭期事件)
EVENT = {
    "new": "filed", "draft": "filed", "filed": "filed",
    "active": "scheduled", "scheduled": "scheduled", "hearing": "scheduled",
    "pending": "postponed", "postponed": "postponed", "continued": "postponed",
    "adjourned": "postponed",
    "rescheduled": "rescheduled",
    "sold": "closed", "closed": "closed", "decided": "closed",
    "withdrawn": "withdrawn", "dismissed": "withdrawn",
    "deleted": "archived", "archived": "archived", "vacated": "archived",
}

# hearing event -> progression rank (informational; the fold uses tiers)
EVENT_ORDER = {
    "unknown": 0,
    "filed": 1, "scheduled": 2, "postponed": 3, "rescheduled": 3,
    "withdrawn": 4, "closed": 5, "archived": 6,
}

_TERMINAL_ORDER = EVENT_ORDER["withdrawn"]

# Keys derived by :func:`process` (plus the legacy ``regression`` flag).
# They are annotations: they never take part in record identity.
ANNOTATION_KEYS = frozenset({
    "state_norm", "invalid_status", "event", "event_order",
    "valid_notice", "invalid_reason", "regression",
})


def canonical(d):
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def content_hash(d):
    """Stable identity of a notice: its source fields only."""
    raw = {k: v for k, v in d.items() if k not in ANNOTATION_KEYS}
    return hashlib.sha256(canonical(raw).encode()).hexdigest()[:24]


def _status_of(record):
    return str(record.get("status", "") or "").strip().lower()


def _ts_of(record):
    return str(record.get("ts", "") or "").strip()


def process(record):
    """Classify one notice; pure per-record, safe to recompute any time."""
    s = _status_of(record)
    reasons = []
    if not str(record.get("id", "") or "").strip():
        reasons.append("missing_id")
    if not _ts_of(record):
        reasons.append("missing_ts")
    if s not in TRANS:
        reasons.append("unknown_status")
    event = EVENT.get(s, "unknown")
    return {
        "state_norm": TRANS.get(s, "unknown"),
        "invalid_status": int(s not in TRANS),
        "event": event,
        "event_order": EVENT_ORDER.get(event, 0),
        "valid_notice": int(not reasons),
        "invalid_reason": ",".join(reasons),
    }


def reduce(records):
    """Batch hook kept for pipeline compatibility.

    Per-case folding now lives in :func:`schedule`; this hook deliberately
    avoids batch-dependent per-record mutation so that record identity stays
    stable across full, incremental and resumed runs.
    """
    return records


def _tier(event_order):
    if event_order <= 0:
        return 0
    if event_order >= _TERMINAL_ORDER:
        return 2
    return 1


def _event_view(rec, ann, effect):
    return {
        "content_hash": content_hash(rec),
        "ts": _ts_of(rec) or None,
        "status": rec.get("status"),
        "event": ann["event"],
        "state_norm": ann["state_norm"],
        "effect": effect,
        "valid_notice": ann["valid_notice"],
        "invalid_reason": ann["invalid_reason"],
    }


def _fold_case(rid, items, as_of):
    """Replay one case's notices in notice-time order with non-regression."""
    placeable, invalid = [], []
    for rec, ann in items:
        ts = _ts_of(rec)
        if not ts:
            invalid.append(_event_view(rec, ann, "invalid"))
            continue
        if as_of is not None and ts > as_of:
            continue  # not yet known at this point in time
        placeable.append((ts, rec, ann))
    placeable.sort(key=lambda item: (item[0], canonical(item[1])))

    history, best_tier, effective_idx = [], -1, -1
    for i, (_ts, rec, ann) in enumerate(placeable):
        tier = _tier(ann["event_order"])
        if (best_tier < 0 or tier == 2
                or (tier == 1 and best_tier <= 1)
                or (tier == 0 and best_tier == 0)):
            # Later notice time advances the schedule; terminal events are
            # sticky; unknown notices only fill an otherwise empty docket.
            best_tier = tier
            effective_idx = i
            effect = "advanced"
        else:
            # Held back: an earlier-issued or uninterpretable notice must
            # not move the schedule backwards (排期不倒退), but stays visible.
            effect = "regressed"
        history.append((rec, ann, effect))

    views = []
    for i, (rec, ann, effect) in enumerate(history):
        if effect == "advanced":
            effect = "effective" if i == effective_idx else "superseded"
        views.append(_event_view(rec, ann, effect))

    effective = views[effective_idx] if 0 <= effective_idx < len(views) else None
    return {
        "id": rid,
        "state": effective["state_norm"] if effective else "unknown",
        "event": effective["event"] if effective else "unknown",
        "status": effective["status"] if effective else None,
        "ts": effective["ts"] if effective else None,
        "hearing": effective,
        "history": views,
        "invalid": invalid,
        "counts": {
            "total": len(views) + len(invalid),
            "regressed": sum(1 for v in views if v["effect"] == "regressed"),
            "superseded": sum(1 for v in views if v["effect"] == "superseded"),
            "invalid": len(invalid),
        },
    }


def schedule(records, as_of=None):
    """Fold notices into per-case 庭期结果 as of ``as_of`` (inclusive).

    ``as_of=None`` answers "今天的排期"; an ISO timestamp answers "回看某个
    历史时点".  The result is a pure function of the stored notices, so full
    collection, continuation runs and historical recovery never disagree.
    """
    as_of = None if as_of in (None, "") else str(as_of)
    grouped, unattributed = {}, []
    for rec in records:
        ann = process(rec)
        rid = str(rec.get("id", "") or "").strip()
        if not rid:
            unattributed.append(_event_view(rec, ann, "invalid"))
            continue
        grouped.setdefault(rid, []).append((rec, ann))
    cases = {}
    for rid in sorted(grouped):
        folded = _fold_case(rid, grouped[rid], as_of)
        if folded["history"] or folded["invalid"]:
            cases[rid] = folded
    return {"as_of": as_of, "cases": cases,
            "unattributed": unattributed, "count": len(cases)}
