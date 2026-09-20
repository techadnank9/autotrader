# AI Trader

A FastAPI app and CLI that use Codex, Bright Data, and Robinhood's official Trading MCP server to:

1. Recommend one long U.S. equity idea.
2. Keep the order size at or below `$5`.
3. Weight Reddit, X, and realtime context.
4. Keep Robinhood execution defaulted to no-op unless explicitly confirmed.
5. Run a one-click autonomous portfolio-management pass against the Robinhood Agentic account.
6. Log replay data for offline SIA evaluation and manual portfolio-agent activation.

This project is intentionally conservative. It defaults to a single-ticker, long-only flow and separates:

- `recommend`: generate one idea
- `preview`: simulate the order
- `live`: launch Codex and require explicit `CONFIRM` before placing an order
- `manage portfolio`: inspect balances, positions, news context, then decide buy / hold / trim / exit in one pass
- `server`: run the FastAPI UI at `http://127.0.0.1:8000`

## Why this shape

Robinhood's current Agentic Trading rollout supports a dedicated Agentic account, long equities only, and these relevant MCP tools: `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_quotes`, `get_equity_orders`, `get_equity_tradability`, `review_equity_order`, `place_equity_order`, `cancel_equity_order`, and `search`.

The official Robinhood support pages say:

- Agentic Trading is still rolling out.
- The Trading MCP endpoint is `https://agent.robinhood.com/mcp/trading`.
- Orders are currently limited to long equities.
- You remain responsible for agent-placed trades.

Codex supports MCP servers directly through `codex mcp add` and `codex mcp login`.
In testing here, Robinhood MCP tool calls were getting cancelled in non-interactive `codex exec` mode, so this starter uses an interactive Codex launch for the trading session instead of pretending the flow is fully headless.

## Files

- `ai_trader/cli.py`: CLI entrypoint
- `ai_trader/server.py`: FastAPI app
- `ai_trader/engine.py`: Bright Data collection, Codex ranking, trade guardrails
- `ai_trader/portfolio_engine.py`: account-aware portfolio-management pass
- `ai_trader/portfolio.py`: default buy / hold / trim / exit planner
- `ai_trader/brightdata_api.py`: Bright Data evidence collection and normalization
- `ai_trader/market_research.py`: daily top-universe snapshots and forward labels
- `ai_trader/sia.py`: SIA task status, replay export, and run orchestration
- `ai_trader/agent_runtime.py`: active portfolio-agent registry and rollback support
- `ai_trader/replay.py`: management-run logs, next-day labels, SIA replay export
- `ai_trader/robinhood.py`: Codex launch orchestration
- `ai_trader/prompts.py`: Codex trading prompts
- `ai_trader/static/`: browser UI
- `ai_trader/config.py`: env loading
- `sia_tasks/portfolio-management-replay/`: replay task scaffold for offline SIA scoring
- `tests/`: smoke and unit coverage for prompts, server routes, portfolio planning, replay, and agent activation

## Setup

1. Install Codex and log in.
2. Make sure you have access to Robinhood Agentic Trading.
3. Add the Robinhood MCP server:

```bash
codex mcp add robinhood-trading --url https://agent.robinhood.com/mcp/trading
codex mcp login robinhood-trading
```

4. Complete Robinhood's desktop onboarding for your Agentic account.
5. Copy `.env.example` to `.env` if you want to override defaults.

Minimal `.env`:

```bash
CODEX_BIN=codex
ROBINHOOD_MCP_URL=https://agent.robinhood.com/mcp/trading
ENABLE_WEB_SEARCH=true
MAX_BUDGET_USD=5
DEFAULT_BUDGET_USD=5
BRIGHT_DATA_API_KEY=
BRIGHT_DATA_ZONE=serp_api1
BRIGHT_DATA_ENDPOINT=https://api.brightdata.com/discover
REPLAY_LOG_DIR=.ai_trader/replay
PORTFOLIO_AGENT_DIR=.ai_trader/portfolio_agents
PORTFOLIO_CASH_RESERVE_USD=5
PORTFOLIO_MAX_POSITIONS=5
PORTFOLIO_MAX_POSITION_PCT=0.45
PORTFOLIO_MIN_TRADE_USD=5
PORTFOLIO_CANDIDATE_POOL_SIZE=6
```

## Usage

Run from the repo root:

```bash
PYTHONPATH=. venv/bin/uvicorn ai_trader.server:app --host 127.0.0.1 --port 8000
PYTHONPATH=. venv/bin/python -m ai_trader doctor
PYTHONPATH=. venv/bin/python -m ai_trader query --query AAPL
PYTHONPATH=. venv/bin/python -m ai_trader query
PYTHONPATH=. venv/bin/python -m ai_trader query --interactive
PYTHONPATH=. venv/bin/python -m ai_trader query --query AAPL --print-only
PYTHONPATH=. venv/bin/python -m ai_trader run --mode recommend --budget 5
PYTHONPATH=. venv/bin/python -m ai_trader run --mode preview --budget 5
PYTHONPATH=. venv/bin/python -m ai_trader run --mode live --budget 5
PYTHONPATH=. venv/bin/python -m ai_trader prompt --mode live --budget 5
PYTHONPATH=. venv/bin/python -m ai_trader run --mode live --budget 5 --print-only
PYTHONPATH=. venv/bin/python -m ai_trader replay-build --task-dir sia_tasks/portfolio-management-replay
PYTHONPATH=. venv/bin/python -m ai_trader agents-list
PYTHONPATH=. venv/bin/python -m ai_trader agents-register sia-gen-1 /path/to/target_agent.py --source-run run-7 --metrics-json '{"score":0.71,"sample_count":42}'
PYTHONPATH=. venv/bin/python -m ai_trader agents-activate sia-gen-1
PYTHONPATH=. venv/bin/python -m ai_trader agents-rollback
```

`run` launches an interactive Codex session in your terminal.

`uvicorn ai_trader.server:app` serves the UI and API.

`prompt` prints only the generated Codex prompt.

`query` defaults to `AAPL` and uses `codex exec --json` so Python can print the final agent result directly.

`query --interactive` launches the same Robinhood MCP prompt in interactive Codex instead.

`--print-only` prints the exact Codex command plus prompt without launching Codex.

The web app now also exposes `Manage portfolio`, which:

1. Reads the Agentic account snapshot.
2. Pulls Bright Data context for held and candidate names.
3. Ranks the opportunity set.
4. Builds a buy / hold / trim / exit plan through the active portfolio agent.
5. Reviews and attempts the plan in one autonomous pass.
6. Logs the run for offline replay and SIA evaluation.

The UI also exposes SIA controls:

1. `Build replay dataset` labels logged management runs and exports the replay task data.
2. `Run SIA` starts `sia run` against the local task when the `sia` binary is installed.
3. Reasoning traces render for analysis, portfolio management, and SIA actions so you can inspect why each step happened.

You can also run the direct file entrypoint from an editor:

```bash
python3 ai_trader/robinhood.py
python3 ai_trader/robinhood.py MSFT
```

## Interactive behavior

- `recommend`: Codex should use Robinhood MCP plus web search to return one JSON recommendation or `no_trade`.
- `preview`: Codex should recommend a ticker and call `review_equity_order`, then return a JSON preview.
- `live`: Codex should recommend, review, then stop and wait for your explicit `CONFIRM` reply before using `place_equity_order`.
- `POST /api/analyze`: builds a stock pool from budget, pool size, source weights, Bright Data context, and Codex ranking.
- `POST /api/trade`: returns `no_op` by default; real execution requires `execute=true` and `confirm_phrase=CONFIRM`.
- `POST /api/manage-portfolio`: runs one full autonomous management pass and logs the result to replay storage.
- `GET /api/portfolio-agents`: lists the active manager, previous manager, and passed candidates.
- `POST /api/portfolio-agents/activate`: switches the active portfolio manager immediately for the next request.
- `POST /api/portfolio-agents/rollback`: swaps back to the previous active portfolio manager.
- `GET /api/sia/status`: reports whether the `sia` binary and task assets are available.
- `POST /api/sia/replay-build`: exports the replay dataset for the portfolio-management task.
- `POST /api/sia/run`: triggers one local SIA run and returns stdout/stderr for inspection.

The live mode is intentionally interactive because headless `codex exec` Robinhood MCP tool calls were canceling during testing in this environment.

## Example live flow

```bash
PYTHONPATH=. venv/bin/python -m ai_trader run --mode live --budget 5
```

Then, inside the launched Codex session:

1. Let Codex inspect the Robinhood account and build the order preview.
2. Review the suggested ticker and preview details.
3. Type `CONFIRM` only if you want the order placed.

## Deploy

`render.yaml` is a Render blueprint. Point Render at this repo, and it builds with
`pip install -r requirements.txt` and serves `uvicorn ai_trader.server:app` on `$PORT`.

What the deployed instance can and cannot do:

- It serves the UI, `/api/analyze`, `/api/manage-portfolio`, and the SIA replay endpoints.
- It pulls real market data from Yahoo for the daily universe snapshot and forward labels.
- It **cannot trade**. Codex is not installed on Render, so every Robinhood MCP call
  degrades to the documented fallback: `fetch_portfolio_snapshot` returns an empty
  account, ranking falls back to a neutral pool, and `/api/trade` returns `blocked`.
- Live trading stays local, where `codex` and `codex mcp login robinhood-trading` exist.

Set `BRIGHT_DATA_API_KEY` in the Render dashboard if you want real Bright Data context
instead of the demo context.

## Behavior without Codex installed

Every Codex subprocess call is guarded. With no `codex` on `PATH` the app still runs:
`doctor` reports `codex_found: false`, analysis returns the fallback ranking, portfolio
management returns `no_trade` against an empty account, and no order is ever attempted.

## Storage

`DATABASE_URL` selects Postgres; without it the app uses JSON files, so local
development needs no database. Both backends implement the same interface, so the
approval gate behaves identically on either. `GET /api/config` reports which one
is live as `storage_backend`.

The hosted instance runs on a free Neon Postgres, because serverless `/tmp` is
wiped on cold starts and accounts created there did not survive.

## Telegram approval loop

The approval gate is a decision record, and the Telegram card is one delivery
channel for it. Execution is reachable only through a record whose status is
`approved`, so a lost message or an unanswered card places nothing.

1. Create a bot with @BotFather and copy the token.
2. Message the bot once, then read your numeric id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Set the environment:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=555111
TELEGRAM_WEBHOOK_SECRET=<a long random string you choose>
DECISION_TTL_MINUTES=240
```

4. Register the webhook against your public URL:

```bash
PYTHONPATH=. venv/bin/python -m ai_trader telegram-webhook https://your-app.example.com
PYTHONPATH=. venv/bin/python -m ai_trader telegram-status
```

5. Propose a decision. It runs the research pass and sends at most one card:

```bash
curl -X POST https://your-app.example.com/api/decisions/propose \
  -H 'content-type: application/json' -d '{"pool_size":5}'
```

Two independent checks guard the callback: the shared secret proves the request
came from Telegram, and the responder id must match `TELEGRAM_CHAT_ID`, so
another Telegram user who finds the bot cannot approve your orders. A decision
can be answered once; replays, expired cards, and skips never reach the executor.

## Guardrails

- Budget is hard-capped by `MAX_BUDGET_USD`.
- One symbol only.
- Long equities only.
- No options, crypto, shorting, leverage, OTC, or penny-stock hunting in the prompt.
- Live mode requires an explicit `CONFIRM` reply inside Codex before placement.
- The model is told to prefer `no_trade` over forcing a weak trade.
- Portfolio management remains long-only U.S. equities and avoids same-day round trips by default.
- Passed SIA generations are manually activated; new generated code is not auto-promoted into production.

## Limitations

- Bright Data uses demo context unless `BRIGHT_DATA_API_KEY` is set.
- Real trade execution requires both the UI execution toggle and the exact `CONFIRM` phrase.
- Recommendation quality depends on model reasoning plus Robinhood tool access.
- SIA replay labeling uses next-day Yahoo EOD data and should be treated as offline evaluation, not brokerage truth.
- The autonomous manager executes only when `Manage portfolio` is clicked; there is no scheduler in v1.
- In this environment, `sia` may not be installed yet. The UI reports that explicitly and disables the run button until it is available on `PATH`.

## Doctor

Use:

```bash
PYTHONPATH=. venv/bin/python -m ai_trader doctor
```

This checks whether `codex` is installed, whether `robinhood-trading` is configured in Codex MCP settings, and reminds you that live trading is interactive.

## Test

```bash
PYTHONPATH=. venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

## Sources

- Robinhood Agentic Trading overview: https://robinhood.com/us/en/support/articles/agentic-trading-overview/
- Robinhood Trading with your agent: https://robinhood.com/us/en/support/articles/trading-with-your-agent/
- Robinhood launch post dated May 27, 2026: https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/
- Codex CLI MCP commands: `codex mcp add --help`, `codex mcp login --help`, `codex exec --help`
