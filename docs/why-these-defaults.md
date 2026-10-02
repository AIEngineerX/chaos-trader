# Why these defaults

The values in `trading/config/paper_autopilot.yaml` and the gates in `gate_classifier.py` are set the way they are for the reasons below. Change them in your own `CHAOS_HOME` copy; the package copy is only the starting point.

**Fees and slippage are charged on both sides.** Entry fee, exit fee, entry slippage, and exit slippage are all applied to every simulated fill. A strategy that only looks good when fees are ignored is not a strategy. The defaults add up to roughly five percent round-trip on small-cap pairs, which is what those pairs actually cost.

**The exit ladder has a time stop.** A position that has not reached its first take-profit by the time stop is closed. Without it, simulated positions sit open forever and the accounting means nothing.

**The stop-loss is checked on a polling interval, so it is not a stop.** The cron entry in the README runs the tick every 5 minutes. Inside a tick, `position_refresh_sec` (default 5) and `discovery_interval_sec` (default 30) in `paper_autopilot.yaml` govern the inner loop, which runs only when you start it with `--max-cycles` because `loop.enabled` is false. A token that loses most of its value between two polls is recorded at the next poll's price, not at the stop. The default interval is a trade-off between RPC cost and how far past the stop a fill can land. Shorten it if your RPC plan allows.

**Wallet gates need a minimum sample.** A wallet with three closed positions tells you nothing about its next one. The minimum closed-position count exists so the loop does not rank wallets on noise.

**The secondary-evidence lane cannot open a position by itself.** Anything under `trading/alpha/secondary/` is a prior, labeled as such in every output. Secondary counts can raise a token from avoid to study, and they are one of the inputs to watch and deep-check, which also need an observed on-chain first touch (`gate_classifier.py`). A paper entry additionally needs on-chain wallet timing, a credible catalyst, or X evidence (`strategy_paper_engine.py`), so secondary evidence alone never opens one.

**Paper only, by default and by config.** `mode: paper_only` and the `boundary` block in the YAML are read by the loop and by the tests. The forbidden-word list is there so a generated message cannot describe a live action.
