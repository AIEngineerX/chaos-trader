---
name: chaos-position-aware-alpha
description: Use when someone wants a Solana token read that accounts for a position their own wallets already hold, such as "verdict with the tokens my own wallets hold in mind" or "should I add to or exit a position my wallets hold", and it returns a read that keeps the fresh-entry gate separate from position management (hold-core, trim-risk, manage, exit-watch) with the catalyst, flow, wallet-timing, and risk evidence behind each.
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, solana, memecoin, position, catalysts, fake-flow, read-only]
    related_skills: [chaos-alpha-decoder, chaos-crypto-trader, chaos-risk-engine, solana]
---

# Chaos Position-Aware Alpha

The agent must not collapse every token read into a single `avoid` / `watch` verdict. A token can be a bad new entry and still require active position management when the user already has exposure and a catalyst changes.

## Prerequisites

- The package is installed: `pip install git+https://github.com/AIEngineerX/chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Overview

Core split:

```text
ENTRY GATE = should a fresh entry be considered?
POSITION = how should existing exposure in your own wallets be managed?
```

This skill governs the current chaos-trader alpha tool architecture around:

```text
Discovery
→ Token read
→ Venue/mode classification
→ Entry gate
→ Wallet/secondary evidence
→ Position context (your own wallets)
→ Wallet timing
→ Social catalyst
→ Fake-flow conversion
→ Delta tracker
→ Ledger/outcome calibration
→ Token card
```

Boundary: market and wallet acquisition are read-only; read labels (study, watch, manual-review, avoid-entry) and policy-governed paper decisions (enter, wait, avoid) are allowed. No live execution, signing, wallet connection, public alerts, or trading buttons.

## When to Use

Use when the user asks the agent about:

- a Solana memecoin mint/token read;
- exposure in your own wallets or existing position context;
- whether an `avoid` token should still be managed because the user has exposure;
- fake-looking flow that may be converting into real attention;
- Toly/KOL/dev/community/X catalysts;
- wallet timing such as early-holder, transfer-recipient, distributor, or scaler;
- deltas between repeated reads for the same mint;
- `chaos token`, `chaos analyze token`, `chaos sweep`, ledger, calibration, or token card behavior.

Do not use this skill for live trading, signing, routing, swap construction, wallet connection, public alpha distribution, or social posting.

## Current Module Map

Scripts run with `chaos run <name>`; state lives under `CHAOS_HOME`.

| Layer | Script | Purpose |
|---|---|---|
| Token event orchestrator | `token_event_analyzer.py` | Builds the full token JSON artifact and markdown. |
| Command UX | `chaos_cmd.py` | Behind the `chaos` CLI verbs; renders compact token cards and render descriptors. |
| Sweep | `trending_token_sweep.py` | Runs ranked discovery plus optional deep reads. |
| Mode | `mode_classifier.py` | Classifies venue/regime such as conviction trench or dead/fake. |
| Entry gate | `gate_classifier.py` | Structural fresh-entry gate. Legacy `gate` remains compatible. |
| Position context | `position_context.py` | Private position-exposure layer and position action. |
| Wallet timing | `wallet_position_timing.py` | Converts matched wallets into timing labels without overclaiming. |
| Social catalyst | `social_catalyst_classifier.py` | Classifies why attention exists. Social links alone are not a catalyst. |
| Flow conversion | `flow_conversion.py` | Separates dead churn from fake-flow visibility/attention conversion. |
| Delta | `token_delta_tracker.py` | Compares current read against previous local artifact for same mint. |
| Ledger | `signal_ledger.py` | Persists read verdict telemetry for later outcome tracking. |
| Calibration | `signal_calibration_report.py` | Aggregates outcome performance by verdict/action. |

## JSON Contract

New token JSON should include:

```json
{
  "entry_gate": {},
  "position_context": {},
  "wallet_timing": {},
  "social_catalyst": {},
  "flow_conversion": {},
  "delta": {}
}
```

Backward-compatible fields must remain:

```text
gate
mode_context
wallet_timing
owner_exposure
classification
fact_grade
```

Do not rename or remove old fields unless every caller and historical artifact consumer is migrated.

## Entry Gate vs Position Gate

### Entry gate

`entry_gate.action` answers:

```text
Would a fresh entry deserve attention here?
```

Allowed labels:

```text
study
watch
manual-review
study-caution
exit-liquidity-watch
avoid-entry
```

Map old structural gate labels conservatively:

| Old / structural | Entry action |
|---|---|
| `avoid` | `avoid-entry` |
| `micro-study` | `study` |
| `micro-watch` | `watch` |
| `deep-check` | `manual-review` |
| `paper-plan` | `manual-review` |
| `exit-liquidity-watch` | `exit-liquidity-watch` |

### Position context

`position_context.position_action` answers:

```text
If the user's own wallets already hold the token, what management posture is warranted?
```

Exposure comes from your own wallets file, the optional `trading/watchlists/owner_wallets.json` under the home (see `docs/smart-wallets.md`): the token read checks each listed wallet's balance of the mint to mark position exposure on the card and masks those addresses in saved artifacts, and nothing else reads it.

Allowed position actions:

```text
no-position
avoid-entry
watch-entry
manage
trim-risk
hold-core
exit-watch
```

Rules:

1. No position exposure → `position_action = no-position`.
2. Position exposure + entry avoid + live catalyst/flow conversion → usually `manage`.
3. Position exposure + high concentration / fragile catalyst → surface risk, but do not automatically erase the position layer.
4. The behavior of your own wallets is private analysis context, not public alpha.
5. Never turn position exposure into copy-trade language.

## Thesis Expiry and Current-Price Discipline

A launch thesis expires. The reason to enter on day one may not justify holding, adding, or refusing to trim on day seven.

For tokens your own wallets hold, or repeat-read tokens, add:

```markdown
## Thesis Expiry
- Original thesis:
- Current thesis:
- Fresh-entry-now: yes / no / smaller / only after trigger
- Current exposure vs fresh-entry size:
- Thesis state: intact / changed / expired / contradicted
- Position implication: hold-core / trim-risk / exit-watch / no-add / re-enter-only-on-trigger
```

Rules:

1. Ask: **If the user had no exposure now, would this still deserve fresh exposure at current price? If yes, how much?**
2. The gap between current exposure and fresh-entry size is the trim-risk signal.
3. Do not let early edge become stale conviction. Once the information deficiency is public or priced, the trade must be re-underwritten.
4. A pure attention meme, a community-conviction coin, a utility-attention hybrid, and an ownership/valuation token have different valid hold windows.
5. Profit-taking and invalidation must be planned before euphoria or panic. Do not let chart urgency replace thesis review.

## Wallet Timing Discipline

Wallet presence is not enough. Classify timing as evidence quality:

```text
early-holder
late-buyer
scaler
distributor
round-tripper
transfer-recipient
airdrop/spam-recipient
unknown
```

Rules:

- Sampled holder/signer data is low-to-medium confidence unless exact transaction history proves buy/sell timing.
- Transfer-recipient is not automatically bullish.
- Quality wallet count must not become copy-trade advice.
- Your own wallets stay private context.

## Social Catalyst Discipline

Classify why attention exists. Supported labels:

```text
none
spam-raid
community-grind
dev-stream
boost-promise
KOL-call
soft-shill
hard-shill
quote-cascade
primary-source-endorsement
image/screenshot-catalyst
viral-event-canonical
vamp-risk
utility-attention
ownership-valuation
```

Important rules:

1. A DEX/market `socials` link is raw evidence only. It is not `community-grind` by itself.
2. Toly/major-source reply or quote cascade can classify as `soft-shill / quote-cascade` when the evidence text supports it.
3. Screenshot/image catalysts are high-fragility unless confirmed by durable primary-source attention.
4. Social evidence prompts investigation; it is not truth by itself.
5. For narrative tokens, separate **canonical attention** from **vamp attention**. A wrong-name/wrong-CA coin can look active while the real narrative leader forms elsewhere.
6. Community conviction requires sustained human effort through drawdowns, not just raids during green candles.

## Fake-Flow Conversion Discipline

Fake-looking flow is not always an immediate avoid. Classify whether it converts:

```text
dead-churn
visibility-engine
attention-converting
holder-converting
exit-liquidity-churn
clean-flow
social-reflexivity
unknown
```

Use:

- V/L ratio;
- average transaction size;
- transaction count;
- buy/sell balance when available;
- holder growth when available;
- social/catalyst quality;
- price response;
- quality wallet presence.

FITNESS-style rule:

```text
Extreme micro-churn + real attention + positive price response = visibility-engine / attention-converting, not automatic avoid.
```

## Delta Discipline

Use `token_delta_tracker.py` to show change only when useful:

- catalyst changed;
- gate changed;
- position action changed;
- market cap/liquidity moved meaningfully;
- wallet count or position exposure changed.

Avoid noisy deltas. A useful card line looks like:

```text
CHANGE: catalyst none → soft-shill; MC +22%
```

## Token Card Contract

Normal token card should preserve this order:

```text
☄️ TOKEN READ
$TOKEN · `mint…`

MODE:
ENTRY GATE:
POSITION:
CATALYST:
FLOW:
MC / Liq / V/L
WALLETS:
CHANGE: only if useful
WHY:
RISK:
NEXT:
Advisory + paper only. No wallet, signing, routing, or live execution.
```

Optional lines when useful and supported by the renderer:

```text
NARRATIVE: mechanism + canonical/vamp risk
THESIS: only if position-exposed or repeat-read
```

When your own wallets hold the token, the card can add:

```text
☄️ OWNER POSITION READ
OWNER: 1/5 · ~$403 · 0.18% liq
PRIVATE POSITION NOTE:
hold-core / trim-risk / manage
```

Live-execution invariant:

```text
Advisory + paper only. No wallet, signing, routing, or live execution.
```

must remain the final visible line. This does not suppress the read labels (study, watch, manual-review, avoid-entry) or policy-authorized paper decisions (enter, wait, avoid).

Deterministic source/position card labels:

```text
study
watch
manual-review
study-caution
exit-liquidity-watch
manage
trim-risk
hold-core
exit-watch
avoid-entry
no-position
```

Do not emit:

```text
swap
ape
snipe
copy-trade
connect wallet
sign
approve
route
submit
execute
```

The agent may add a separate read label (`study`, `watch`, `manual-review`, or `avoid-entry`) and, for the paper book, a paper decision (`enter`, `wait`, or `avoid`) when evidence supports it. That layer never implies wallet access, routing, submission, or a live fill.

## Verification Commands

Developers run `make test` from a source checkout; an installed skill has no tests to run.

Render descriptor smoke (from any directory):

```bash
chaos token <mint> --no-x --tx-limit 3 --render-json --timeout 240
```

Check descriptor has:

```text
ENTRY GATE:
POSITION:
CATALYST:
FLOW:
Advisory + paper only. No wallet, signing, routing, or live execution.
```

and no wallet, signer, route, submission, swap, or live-fill language.

## Common Pitfalls

1. **One verdict for two jobs.** Entry and position are separate. Do not let `avoid-entry` erase existing exposure management.
2. **Social-link laundering.** `market.socials` alone is not a catalyst.
3. **Fake-flow absolutism.** High churn can still be a visibility engine if attention/price/holders convert.
4. **Wallet-count worship.** Quality wallet count without timing and exit evidence is weak.
5. **Private wallet leakage.** Your own wallets are private management context, never public alpha.
6. **Authority confusion.** Read labels and paper decisions are allowed; wallet, signer, route, swap, submission, and claimed live-fill language are not.
7. **Stale artifact deltas.** Delta compares against previous local artifacts; recent smoke artifacts can influence `CHANGE` lines.
8. **Overstating exact timing.** If transaction history is sampled or unavailable, say timing unresolved/unknown.
9. **Expired launch thesis.** A day-one information edge can become public or priced. Re-underwrite positions your own wallets hold at the current price before implying hold-core.
10. **Vamp blindness.** Do not treat attention on the first visible CA as canonical when name/source/creator ambiguity can route flow to a competing coin.
11. **Community-raid confusion.** Raids are not conviction unless humans keep producing original effort when the chart is weak.

## Verification Checklist

- [ ] `entry_gate` and `position_context` both exist in token JSON.
- [ ] Existing `gate`, `classification`, `mode_context`, `wallet_timing`, `owner_exposure`, and `fact_grade` remain compatible.
- [ ] No-position tokens show `POSITION: no-position`.
- [ ] Tokens your own wallets hold can show `POSITION: manage` even when `ENTRY GATE: avoid-entry`.
- [ ] Tokens your own wallets hold, and repeat-read tokens, include thesis-expiry logic when useful.
- [ ] Analysis includes narrative/canonical/vamp risk when attention identity is ambiguous.
- [ ] Social links alone do not classify as a catalyst.
- [ ] Fake-flow conversion label is present for churn-heavy reads.
- [ ] Delta appears only when useful.
- [ ] The render descriptor keeps copy/open DEX+Solscan buttons only and the final advisory/paper/no-live-execution boundary.
- [ ] No Padre/trading-execution URLs are allowlisted or rendered.
- [ ] Full test suite passes.
