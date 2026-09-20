# AI Trader — system design

The product is one loop: **agents research continuously → one decision is proposed →
the user approves or skips → the outcome is scored and fed back.** Everything below
serves that loop. The design constraint that shapes every component is that the
system is never allowed to act on its own.

---

## 1. Components

### Ingestion
Pulls raw material on a schedule and normalizes it into evidence rows
(`symbol`, `source`, `title`, `url`, `observed_at`).

| Source | Cadence | Notes |
|---|---|---|
| Price + volume | daily EOD, intraday on demand | Yahoo chart API today |
| Universe screen | daily pre-open | most-actives; defines what gets researched |
| Wire news / filings | continuous | highest trust weight |
| Reddit / X | continuous | lowest trust weight, deduplicated hard |

Dedup is the important part: one story repeated by forty accounts is one piece of
evidence, not forty. Cluster by claim, not by post.

### Research agents
A pool of agents, each scoped to one question rather than one ticker — supply chain,
earnings quality, momentum divergence, sentiment-vs-price gap. Each returns a scored
claim with citations. Agents never see each other's output, so agreement between them
is real signal instead of an echo.

### Ranker
Collapses claims into one ranked list per day. Weighted by source trust and evidence
independence, not mention volume. Output is `[{symbol, score, reason, evidence_ids}]`.

### Decision service
Turns the ranking plus the user's current account state into at most **one** proposal:
ticker, dollar amount, reasoning, confidence, evidence. Sizing respects the budget
ceiling, position cap, and cash reserve. If nothing clears the bar it proposes nothing —
silence is a valid output and the common one.

### Notification + approval
Pushes the proposal to the phone as a decision card. The card carries everything needed
to decide without opening anything else. Two actions: approve, skip. A proposal expires
if not answered before the window closes, and an expired proposal is a skip.

### Execution
Only ever invoked by an approval. Reviews the order with the broker, places it, and
records the fill. Long-only equities. If the broker rejects or the price moved beyond
tolerance, it aborts and reports rather than retrying at a worse price.

### Ledger + evaluation
Every proposal is written down with the evidence that produced it, the user's answer,
and the fill. Forward returns are attached at the next session close. That record is
what lets the ranker be scored offline and improved — and what lets the user ask
"why did you suggest that" months later.

---

## 2. The decision record

One immutable row per proposal. This is the spine of the system.

```json
{
  "decision_id": "dec_2026_09_20_01",
  "created_at": "2026-09-20T06:02:11Z",
  "expires_at": "2026-09-20T15:25:00Z",
  "symbol": "NVDA",
  "side": "buy",
  "amount_usd": "25.00",
  "confidence": 0.81,
  "reason": "Three independent supply-chain checks moved the same direction.",
  "evidence": ["ev_8812", "ev_8830", "ev_8841"],
  "account_snapshot_id": "snap_9912",
  "policy": {"max_order_usd": "25.00", "long_only": true},
  "user_response": "approved",
  "responded_at": "2026-09-20T13:41:52Z",
  "execution": {"status": "filled", "order_id": "...", "fill_price": "..."},
  "label": {"horizon_days": 1, "forward_return": 0.0214}
}
```

Nothing downstream may mutate a decision record. Corrections are new rows.

---

## 3. Safety model

The approval gate is the product, so it is enforced structurally rather than by policy:

1. **Execution has one entry point**, and it requires a decision record whose
   `user_response` is `approved`. There is no other path to the broker.
2. **The default is inaction.** No response means no trade. A dropped notification,
   a dead phone, or a server outage all fail closed.
3. **The budget ceiling is read from config, never from the model.** No agent output
   can widen it, and sizing is clamped after the model returns, not asked of it.
4. **The instrument set is a whitelist.** Long equities only; options, crypto, margin,
   shorts and OTC are absent rather than discouraged.
5. **One open proposal at a time.** Prevents an agent loop from queueing up many
   approvals a distracted user taps through.
6. **Every order is attributable** to the evidence that caused it.

---

## 4. Failure modes worth designing for

| Failure | Response |
|---|---|
| Model returns unparseable output | Fall back to a neutral ranking; never fabricate a proposal |
| Data source down | Proceed with reduced evidence and lower confidence; do not silently fill the gap |
| Price moves between proposal and approval | Re-check at execution; abort past tolerance |
| User approves an expired card | Reject; propose again next cycle |
| Agents all agree because they read one syndicated story | Dedup by claim upstream, so this cannot reach the ranker |

---

## 5. What exists today

Built and running in this repo: ingestion (price, universe, social context), the
ranker, the decision/sizing logic with its policy clamps, execution against the broker
behind an explicit confirmation, and the ledger with forward labeling and offline
scoring.

Not built: push notification delivery, the phone client, the expiry window, and
per-user accounts. Today the approval gate is a button in the web dashboard rather
than a card on a phone. The safety properties above hold in the current code; the
delivery mechanism is what is missing.
