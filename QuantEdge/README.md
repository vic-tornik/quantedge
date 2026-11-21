# QuantEdge Spot 0.7

## Synchronized depth collection
The collector subscribes to BTCUSDT and ETHUSDT diff-depth streams at 100ms alongside best bid/ask streams. After receiving the first depth update for a symbol, it buffers events while a dedicated thread fetches a 5000-level REST snapshot. Buffered events already covered by the snapshot are discarded. Remaining updates must bridge the next required update ID, and any sequence gap rebuilds the connection and its books. Duplicate/overlapping ranges are handled, quantities replace existing quantities, and a zero quantity removes a level.

Only prices within the initial snapshot's bid/ask coverage are retained. Exhausting a known side, a crossed book, invalid numeric data or a level bound triggers resynchronization. Snapshot buffers are capped at 2000 events and 8 MiB per symbol. Each side is limited to 20000 tracked levels. Snapshot HTTP requests run in a separate two-thread pool. Persistent reconnect failures back off to 30 seconds plus jitter, including connections that fail shortly after opening. A connection must remain stable for 30 seconds before the retry delay resets.

Raw depth deltas and REST snapshots are retained in local depth_events and exported transactionally to PostgreSQL qe_depth_events with a separate committed qe_depth_cursors checkpoint. A sampled top-20 view is published at most every 500ms and used by the dashboard, which displays ten levels per side. Books are hidden when stale, disconnected or awaiting a bridging update. A restart obtains new snapshots. Disk retention remains an operator responsibility.

```powershell
python quantedge.py collect
python quantedge.py depth
python quantedge.py dashboard
```

Run the collector in its own terminal. Use --mode testnet consistently for Testnet. Existing databases gain the additional tables automatically, and subsequent PostgreSQL exports create the new depth tables. Order execution continues to use its existing fresh REST quote and order-risk checks. Depth-based sizing, slippage estimates and strategy execution remain future work.

The 0.7 suite has 45 passing checks and one optional PostgreSQL integration check skipped in the development environment. Mocked snapshot handoff, overlapping sequence ranges, deletion, gap recovery, malformed/crossed books and collector routing are covered. Package installation still fails in this environment, Docker's database engine is unavailable, and Binance's public check fails DNS resolution. Live depth synchronization, PostgreSQL export and signed Testnet execution remain unverified. Browser layout verification is also outstanding.

## Collector and recovery improvements
Portfolio reconciliation, exchange balance fetches and PostgreSQL export run on a dedicated background thread with its own SQLite connection. Refresh requests coalesce to at most one pending refresh. A slow persistence operation therefore does not run inside the WebSocket receive loop. Exchange balance requests also complete before the portfolio transaction acquires the local write lock. Local quote persistence is still synchronous and remains subject to SQLite or disk contention.

The dashboard now shows tracked position quantities and cost, the latest exchange account snapshot balances, and the last reported health of the stream, background worker, portfolio calculation and PostgreSQL export. Run `python quantedge.py health` to inspect these states and current per-symbol quote ages in the terminal. Component states are historical reports with timestamps; check their age when diagnosing a stopped process. The collector clears the stream state on a clean shutdown and invalidates quote valuations immediately when a disconnect is detected. The configured --stale-seconds value is also used by portfolio snapshots and the dashboard.

On shutdown the worker is asked to stop and is given ten seconds to finish the current operation. If an exchange or database request takes longer, the process can exit while that daemon thread is pending. Incomplete PostgreSQL transactions roll back and are retried from the committed cursor after restart. Local events remain buffered.

## Market data and portfolio foundation
Install Python 3.11+ dependencies in a virtual environment. Network access to PyPI is required.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python quantedge.py collect
```

In a second terminal, activate the same environment and run `python quantedge.py dashboard`. The collector streams BTCUSDT and ETHUSDT best bid/ask quotes, records events durably, imports journal fills, and produces portfolio snapshots approximately every ten seconds. The dashboard updates every five seconds. Run `python quantedge.py portfolio` for a fresh accounting snapshot using the latest collected quotes. It needs fresh quotes to value BTC and ETH. Use --mode testnet consistently in both terminals for Testnet data and credentials. Live market collection uses production public data.

Connections use exponential reconnect delay capped at 30 seconds plus jitter. Missing symbol updates for ten seconds cause reconnection. Server shutdown events and scheduled 23-hour rotations also reconnect. Server pings are handled by the WebSocket client library. Each new connection clears cached quotes until new events arrive. Out-of-order or duplicate update IDs are ignored within a connection. Last prices remain visible with a stale label, and dashboard valuations are hidden when quotes or the snapshot are stale.

Paper fills, including existing 0.4.1 fills, are imported from the durable order history. Testnet/live fills are retrieved by exchange order ID from signed myTrades history, checked against executed quantity, and deduplicated by symbol and exchange trade ID. The collector binds to the journal's account UID. UNKNOWN/SENDING orders must first be reconciled with the status command. A mismatch stops that portfolio update and logs portfolio_error. Existing order submission paths still obtain a fresh REST quote independently.

Accounting uses Decimal arithmetic and weighted-average acquisition cost. Quote-asset commissions adjust cost/proceeds. Base-asset commissions adjust inventory. Third-asset commissions leave P&L unavailable until historical fee conversion is implemented. Wallet equity uses fresh midpoint marks and includes free plus locked exchange balances. Unsupported nonzero assets make equity unavailable. Live tracked P&L covers QuantEdge-recorded fills. Opening inventory, external trades and transfers are outside this cost basis, and sales exceeding known inventory make P&L unavailable. This release provides per-asset position quantities and cost, realized/unrealized tracked P&L, fee totals, balances and snapshots. It does not establish strategy profitability.

## PostgreSQL storage and restart recovery
The existing SQLite execution journal also acts as the durable collection buffer. PostgreSQL receives market events, orders, fills, balances and portfolio snapshots in a single transaction. An export cursor advances only when that PostgreSQL transaction commits. The collector retries export during subsequent updates while local records remain available. Each local journal has a stable export identity. Keep its database and WAL files together during normal operation and use SQLite backup tooling for backups.

Start Docker Desktop, choose a database password in your terminal, then start PostgreSQL. The Compose service binds only to 127.0.0.1 and keeps data in a named volume.

```powershell
$env:QE_POSTGRES_PASSWORD = Read-Host 'Local PostgreSQL password'
docker compose up -d
$env:QE_POSTGRES_DSN = Read-Host 'PostgreSQL connection string'
python quantedge.py export-postgres
python quantedge.py collect
```

The connection-string format is `postgresql://quantedge:PASSWORD@127.0.0.1:5432/quantedge`. Percent-encode special characters in URL passwords or use psycopg's supported keyword connection-string format. Keep this credential in your own environment. Use the same QE_POSTGRES_DSN in any collector terminal that should export. schema.sql creates the qe_market_events, qe_orders, qe_fills, qe_balances, qe_snapshots and qe_cursors tables. Export is idempotent for the same journal identity. Local data is retained after export. Plan disk retention before sustained collection, because this release does not prune events.

```powershell
python -m unittest -v
$env:QE_TEST_POSTGRES_DSN = $env:QE_POSTGRES_DSN
python -m unittest test_foundation.PostgresIntegration -v
python quantedge.py --mode testnet verify-testnet --public-only
python quantedge.py --mode testnet verify-testnet
```

The PostgreSQL integration check exports twice, reopens the journal and verifies row counts and cursor stability. The normal test suite exercises simulated disconnect/reconnect, stale streams, duplicate events across restarts, partial-fill replay, accounting and commission cases. Mocked coroutine tests avoid Windows event-loop socket creation, which is blocked in this environment. Actual network scheduling and WebSocket transport therefore remain unverified here. Real PostgreSQL integration requires the running service and driver. Signed Testnet verification requires local credentials. Development in this environment was blocked by DNS access to PyPI/Binance and a stopped Docker engine, so these external checks remain outstanding. The dashboard has also not been visually verified in a browser.

The collector is intended for two symbols on one machine. Its background worker isolates network reconciliation and PostgreSQL export from stream reads. Measured latency targets, a distributed event bus and multi-worker scaling remain future work.
A single-operator Python trading terminal with a local dashboard, two paper strategies and an explicit real-money order path. Windows, macOS or Linux, Python 3.11+. Core terminal commands use the standard library; the foundation collector and PostgreSQL export require requirements.txt. Extract the whole ZIP and open a terminal in the folder containing quantedge.py.

## First run
```powershell
python -m unittest -v
python quantedge.py replay
python quantedge.py balance
python quantedge.py dashboard
```
Open http://127.0.0.1:8765 in your browser. Ctrl+C closes the monitor. Use another terminal in the same folder for commands. The monitor reads the last 100 tickets for the selected mode every five seconds. Exchange balances and order statuses are refreshed explicitly with balance and status. Paper starts with 100 virtual USDT, 0 BTC and 0 ETH. Replay uses synthetic minute bars and its own virtual balance. It works offline and does not write orders.

## Modes and data
| Mode | Market data | Execution |
| --- | --- | --- |
| paper (default) | Binance public Spot REST; no keys | Local wallet, IOC simulations, configurable order and daily caps |
| testnet | Binance Spot Testnet | Signed Spot IOC orders using test assets |
| live | Binance production Spot | Signed Spot IOC orders using available account funds |

Each mode has a separate SQLite journal under ~/.quantedge. Keep the same state directory for all terminals. QE_STATE_DIR can override it. Use a private, persistent local disk. Restoring an older copy, deleting the journal, running multiple independent installations or trading outside the tool can bypass local caps. Bind one account to each mode journal. A change of keys for the same UID works. The first exchange submission binds the UID. In live mode the dashboard is a monitor, with orders entered through the terminal.

## Paper strategies
```powershell
python quantedge.py quote BTCUSDT
python quantedge.py signal BTCUSDT breakout
python quantedge.py watch BTCUSDT breakout --cycles 30 --budget 8
python quantedge.py orders
```
watch evaluates on the latest completed one-minute candle and sleeps 60 seconds between cycles. Its checkpoint suppresses duplicate evaluation of the same strategy, pair and candle. A crash or an unavailable quote can skip a signal. Use one strategy per symbol for paper attribution because both strategies share its position. An entry targets the budget; sells are capped at the same approximate notional and available units. An order below the venue minimum is skipped with the rejection reason.

Breakout compares the close with the previous 20 closes' range. Reversion buys below 99% of the preceding 20-close mean and signals sell at or above that mean. Offline replay fills the previous bar's signal at the next open and charges 0.10% per side. Paper IOC uses available top-of-book size and a 0.10% quote-currency fee. Actual liquidity and account fees can differ. Neither replay nor these example signals establish profitability.

## Verify Binance Spot Testnet
Run the following from the extracted project directory. The first command needs only internet access. The second and third use the Testnet credentials described below. Each command returns a nonzero exit code on failure.

```powershell
python quantedge.py --mode testnet verify-testnet --public-only
python quantedge.py --mode testnet verify-testnet
python quantedge.py --mode testnet verify-testnet --exercise
```

The public check measures server-clock offset and round-trip time and checks the current quote against order filters. The default signed check verifies account access, previews a ticket, and calls the Testnet order-validation endpoint. The optional exercise submits one immediate-or-cancel BUY using virtual funds and independently looks up its execution receipt. Each new exercise invocation is a new virtual order. The default pair is BTCUSDT with an 8 test-USDT budget. You can override these with --symbol and --budget. A venue minimum or local cap can prevent the example from running.

If submission succeeds but lookup fails, use the ticket printed at the preview stage. Run `python quantedge.py --mode testnet verify-testnet --ticket TICKET_ID` to retry just the lookup. This also works after a restart. A terminal EXPIRED result verifies exchange acceptance and reconciliation; check executed_qty to see whether any virtual assets changed hands. The original execution receipt, including any reported fill commissions, is retained when verifying an already-final order.

## Connect an exchange account
Use a Binance **HMAC** key for the chosen environment with account-read and Spot trade access. Use an IP allowlist when available. Leave withdrawals, transfers and margin permissions disabled. For production, check that your account can use Binance Spot in your location, then fund it directly at the exchange. QuantEdge uses available USDT for buys and the available base asset for sells. It does not accept deposits, convert INR or request bank details.

In PowerShell, this helper reads a credential without showing it or putting the literal in command history. It remains available to child processes for the terminal session. Enter secrets only on your own machine.
```powershell
function Set-SessionCredential([string]$Name) {
    $value = Read-Host $Name -AsSecureString
    $credential = [System.Net.NetworkCredential]::new('', $value)
    [Environment]::SetEnvironmentVariable($Name, $credential.Password, 'Process')
}
Set-SessionCredential QE_TESTNET_KEY
Set-SessionCredential QE_TESTNET_SECRET
python quantedge.py --mode testnet balance
python quantedge.py --mode testnet quote BTCUSDT
```
Keep your computer clock synchronized for signed requests. Production uses separate QE_LIVE_KEY and QE_LIVE_SECRET environment variables. On Linux/macOS use your secret manager or a hidden shell prompt to set equivalent session variables.

## Preview, submit and verify
Prices are in USDT per coin. Quantity is BTC or ETH, depending on the selected pair. Preview checks tick size, quantity increment, notional, spread, available balance and distance to the current executable quote. If a filter fails, use the reported increment or exchange rules to adjust the ticket. The price entered for a buy is the highest price you accept; for a sell it is the lowest. The order attempts to fill immediately. The unfilled portion expires. Commission may be charged separately.

Testnet walkthrough in PowerShell:
```powershell
$qty = Read-Host 'BTC quantity'
$price = Read-Host 'Maximum USDT per BTC'
python quantedge.py --mode testnet preview BTCUSDT BUY $qty $price
$id = Read-Host 'Ticket ID from preview'
python quantedge.py --mode testnet submit $id
python quantedge.py --mode testnet status $id
python quantedge.py --mode testnet balance
```
Review the price, quantity, total and mode in the preview. Submit within 60 seconds. FILLED means the entire amount executed. EXPIRED can include a partial fill. Inspect executedQty and cummulativeQuoteQty in the result. A final status command reads the terminal receipt previously stored. Check the exchange's order and trade history for independent verification and exact commissions.

## Real-money orders
Complete a Testnet preview, submission and status lookup from your own machine first. For real orders, use an eligible, funded Spot account. Use only money you can afford to lose. Choose your own asset, size and price.

```powershell
Set-SessionCredential QE_LIVE_KEY
Set-SessionCredential QE_LIVE_SECRET
python quantedge.py --mode live balance
python quantedge.py --mode live quote BTCUSDT
$qty = Read-Host 'BTC quantity'
$price = Read-Host 'Maximum USDT per BTC'
python quantedge.py --mode live preview BTCUSDT BUY $qty $price
$id = Read-Host 'Reviewed LIVE ticket ID'
$env:QE_ENABLE_LIVE = 'YES'
python quantedge.py --mode live submit $id --confirm-live $id
Remove-Item Env:QE_ENABLE_LIVE
python quantedge.py --mode live status $id
```
For a sale use SELL in the preview and enter a minimum price. BTCUSDT and ETHUSDT are the supported symbols. Testnet IDs do not work in live mode. Preview and balance are read-only at the exchange. The submit command sends the real order. Clean up credentials by closing the terminal. Strategy signals are informational in live mode.

## Controls and recovery
- QE_ORDER_CAP defaults to 10 USDT per submitted ticket. QE_DAILY_CAP defaults to 30 USDT of submitted limit notional across buys and sells from midnight UTC. These are local gross order-size limits before fees. They do not cap investment losses or bound sell proceeds from a favorable fill. Rejections after dispatch and expired orders continue to consume this conservative allowance. Previewed orders do not.
- Wider than 0.5% bid/ask spread, limit over 1% away from the executable quote, market requests taking over five seconds, or expired preview all block submission. Binance remains authoritative for dynamic filters, order availability and commission.
- Run python quantedge.py --mode live pause to block subsequent submissions or resume to allow them. An order already dispatched can execute. Check its status.
- UNKNOWN / SENDING can mean Binance accepted the order even when the response was lost. Keep the journal. Run status with the same mode and ID, which issues a signed lookup for pending orders. A timeout, temporary lookup failure or missing order leaves the hold in place. Confirm order history and fills at Binance before any further action. If a crash occurred between journal commit and network send, an operator may need to investigate an unresolved hold. There is no automatic resubmit or force-clear command.
- Back up the journal using SQLite's backup API or a clean shutdown of all QuantEdge processes. Do not operate two separate journals on the same account. The desk is intended for a single trusted OS user.
- A declined key or clock error needs corrected permissions/credentials or synchronized time. HTTP 429/418 requires stopping and respecting the venue's rate limit. If DNS or exchange access fails, replay and tests remain available offline.

## Implementation and verification scope
exchange.py signs fixed-host HTTPS requests. engine.py runs risk checks, atomic paper fills and durable live intent reservation. signals.py implements candle evaluation and replay. dashboard.py serves a loopback-only read-only view. quantedge.py exposes those paths through the CLI.

This is a standalone implementation of the execution and collection slice of the QuantEdge blueprint. It uses a local SQLite execution journal, WebSocket book tickers, synchronized depth within snapshot coverage and transactional PostgreSQL export. The original v0.1 source could not be retrieved for integration testing. Redis worker distribution, triangular and cross-exchange strategies, and authenticated multi-user UI remain future work. A signed remote acceptance check is still outstanding.

Official API references:
- https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints
- https://developers.binance.com/docs/binance-spot-api-docs/rest-api/request-security
- https://developers.binance.com/docs/binance-spot-api-docs/filters
- https://developers.binance.com/docs/binance-spot-api-docs/testnet/rest-api
