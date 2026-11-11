# Polymarket Monitor

Follows the top traders on Polymarket's **sports** leaderboard and sends a Telegram alert when:

1. **Consensus pick** - several different top traders have bought the *same outcome of the same
   market* within the last 24 hours (default: 3+ traders, $3,000+ combined), or
2. **Large bet** - one top trader has put $10,000+ on a single pick.

Read-only: it reads public data and sends alerts. It never places bets. Not financial advice; a
leaderboard rank is not proof of skill.

## How it works

Every run (about every 10 minutes):

1. Read the leaderboard (`/v2/leaderboard?category=sports`, PnL board) every 6 hours to get the top N wallets.
2. For each wallet, fetch BUY trades since the last run (`/v2/trades?user=...`, with a 15-minute overlap
   so late-indexed trades aren't missed).
3. Combine each wallet's fills on a pick into one position (stake and volume-weighted price).
4. Alert on large positions and on picks backed by enough distinct wallets. Wallets with tiny stakes
   don't count toward a cluster, and buys above $0.95 average price are ignored (near-certain
   outcomes, not conviction). A cluster alert also shows how many top traders are on the other side.
5. Remember everything on a `monitor-state` branch (one force-pushed commit) so nothing alerts twice.

The first run only builds a baseline and sends "Monitor started".

## Setup

1. Create a **new empty GitHub repo** and push this folder (same five git commands as before).
2. Add secrets (Settings > Secrets and variables > Actions > **Secrets**): `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`. You can reuse your existing bot; secrets are per-repo.
3. Actions tab > *Polymarket monitor* > *Run workflow* with **Dry run** ticked. Check the log.
4. Run once without dry run (baseline). Then the schedule takes over.

## Tuning (repository *variables*, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `TOP_N` | 50 | How many leaderboard wallets to follow |
| `LEADERBOARD_WINDOW` | month | day, week, month or all |
| `WINDOW_HOURS` | 24 | How far back a bet still counts |
| `MIN_WALLETS` | 3 | Distinct top traders on the same pick |
| `MIN_WALLET_USD` | 250 | A wallet's stake must be at least this to count |
| `CLUSTER_MIN_USD` | 3000 | Combined stake across the cluster |
| `LARGE_BET_USD` | 10000 | Single-wallet stake that triggers a large-bet alert |
| `MAX_PRICE` | 0.95 | Ignore picks bought above this average price |

## Known limits (read before trusting it)

* **Not yet run against live Polymarket.** Built from the official API docs and tested on synthetic
  data. Do the dry run first and check the log.
* **Not sports-only.** The leaderboard is filtered to sports, but the trade feed has no category
  filter, so alerts can include non-sports markets those wallets trade. If that's noisy, we add a
  market-category filter.
* **BUYs only.** A trader who buys and later sells still shows as a buyer.
* **Speed.** 10-minute polling suits pre-game and long-dated markets; odds can move faster than that
  around live games.
* **A wallet's other positions (hedges, off-platform bets) are invisible.**
