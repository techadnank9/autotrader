# AI Trader

A FastAPI app and CLI that use Codex, Bright Data, and Robinhood's official Trading MCP server to:

1. Recommend one long U.S. equity idea.
2. Keep the order size at or below `$5`.
3. Weight Reddit, X, and realtime context.
4. Keep Robinhood execution defaulted to no-op unless explicitly confirmed.

This project is intentionally conservative. It defaults to a single-ticker, long-only flow and separates:

- `recommend`: generate one idea
- `preview`: simulate the order
- `live`: launch Codex and require explicit `CONFIRM` before placing an order
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
- `ai_trader/robinhood.py`: Codex launch orchestration
- `ai_trader/prompts.py`: Codex trading prompts
- `ai_trader/static/`: browser UI
- `ai_trader/config.py`: env loading
- `tests/`: small smoke tests for prompts and launch plans

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
```

`run` launches an interactive Codex session in your terminal.

`uvicorn ai_trader.server:app` serves the UI and API.

`prompt` prints only the generated Codex prompt.

`query` defaults to `AAPL` and uses `codex exec --json` so Python can print the final agent result directly.

`query --interactive` launches the same Robinhood MCP prompt in interactive Codex instead.

`--print-only` prints the exact Codex command plus prompt without launching Codex.

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

The live mode is intentionally interactive because headless `codex exec` Robinhood MCP tool calls were canceling during testing in this environment.

## Example live flow

```bash
PYTHONPATH=. venv/bin/python -m ai_trader run --mode live --budget 5
```

Then, inside the launched Codex session:

1. Let Codex inspect the Robinhood account and build the order preview.
2. Review the suggested ticker and preview details.
3. Type `CONFIRM` only if you want the order placed.

## Guardrails

- Budget is hard-capped by `MAX_BUDGET_USD`.
- One symbol only.
- Long equities only.
- No options, crypto, shorting, leverage, OTC, or penny-stock hunting in the prompt.
- Live mode requires an explicit `CONFIRM` reply inside Codex before placement.
- The model is told to prefer `no_trade` over forcing a weak trade.

## Limitations

- Bright Data uses demo context unless `BRIGHT_DATA_API_KEY` is set.
- Real trade execution requires both the UI execution toggle and the exact `CONFIRM` phrase.
- Recommendation quality depends on model reasoning plus Robinhood tool access.
- This is not portfolio management software. It is a narrow example for a single small order.

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
