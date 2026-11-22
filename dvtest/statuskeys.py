# SPDX-License-Identifier: MIT
"""Names of the status JSON that DV-Router renamed (NODE1e S1-S3, DV-Router dcbbb205, image wip-f1f3777 and later), read on both sides.

  public state `GET /api/v1/public/state` and admin status `GET /api/v1/admin/status`:  block `live` -> `summary`
  inside that block:                                                                    `rooms_live` -> `rooms_active`
  (the other renames of the NODE1e note, `live_epoch` -> `active_epoch` and the ids sync reply `live` -> `available`, are not read by a case)

The older images keep the old names, so every reader goes through these functions and never names a key itself.
"""

BLOCK_NEW, BLOCK_OLD = "summary", "live"


def block_key(doc):
    """The key of the summary block in a parsed public state or admin status: `summary` when present, else `live`."""
    if isinstance(doc, dict) and BLOCK_NEW in doc:
        return BLOCK_NEW
    return BLOCK_OLD


def block(doc):
    """The summary block (a dict) of a parsed public state or admin status, whichever name the image uses; None when there is none."""
    if not isinstance(doc, dict):
        return None
    value = doc.get(block_key(doc))
    return value if isinstance(value, dict) else None


def rooms_active(summary):
    """The room list of a summary block: `rooms_active`, else the older `rooms_live`; None when neither is there."""
    if not isinstance(summary, dict):
        return None
    return summary["rooms_active"] if "rooms_active" in summary else summary.get("rooms_live")


def public_state_keys():
    """The keys the public state must have on both sides: the block is `summary` or `live`, so the check takes one of them."""
    return {"clients", "info", "now_ms", "reflector", "rooms", "version"}
