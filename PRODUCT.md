# AI Trader — product context

## What it is
Autonomous research agents read the market continuously, then send the user **one
decision at a time**: a ticker, a dollar amount, and the reasoning behind it. The
user approves or skips with a single tap. Nothing is ever placed without that tap.

## Who it is for
Retail investors who want research they cannot do themselves, without handing over
discretion. The pitch is not "the AI trades for you" — it is "the AI does the
reading, you keep the decision."

## Product truth (what the code actually does today)
- Long-only US equities. No options, crypto, shorting, leverage, OTC.
- Every order is capped by `MAX_BUDGET_USD`.
- Execution defaults to no-op; a real order needs explicit confirmation.
- Every management pass is logged with its evidence and scored later offline.
- The mobile app and push delivery are **not built yet**. The working surface is
  the web dashboard at `/app`. Landing copy must not imply a shipped phone app.

## Voice
Plain, precise, unhurried. Names the limits out loud — the restraint is the product.
Never hype, never "10x your portfolio", no fabricated performance numbers.

## Non-negotiable
Illustrative figures are labeled as illustrative. No performance claims. No implied
returns. The page says plainly that this is prototype software and not advice.
