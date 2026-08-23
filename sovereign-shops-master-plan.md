# SOVEREIGN SHOPS — CONSOLIDATED MASTER PLAN

**Last updated:** 2026-07-03 13:00 UTC
**Board:** `sovereign-shops`
**Framework:** Professor Günter Faltin — "Kopf schlägt Kapital" (Brains Beat Capital)

---

## REPO QUICK REFERENCE

| Shop | Repo | npub | Status |
|------|------|------|--------|
| Sovereign Optics | ~/nostr-glasses/ | sovereignoptics@coinos.io | Active, needs CoinOS LN |
| Sovereign Trade | ~/nostr-trade/ | soverigntrade@coinos.io | Active, needs CoinOS LN |
| Sovereign Guides | ~/nostr-guides/ | sovereignguides@coinos.io | Active, needs CoinOS LN |
| Sovereign Engineering | ~/nostr-engineering/ | sovereigneng@coinos.io | Active, needs CoinOS LN |
| Plebeian Market codebase | ~/market/ | — | See plebeian-market-master-plan.md |

### Infrastructure
- **Price scraper:** `~/plebeian-shop/services/`, indiamart-prices.js on VPS2 (cron every 6h)
- **Kalman price tracker:** (planned) `~/price-insight/`
- **ContextVM:** VPS2 `:9090` — `/api/catalog`, `/api/translate`, `/api/price-intel`
- **Relays:** tollgate-strfry-agg, tollgate-strfry-market-agg on VPS2
- **Blossom:** tollgate-blossom on VPS2 (file/media hosting)
- **Payment:** Wallet of Satsoshi (WoS), CoinOS LN (coinos.io)
- **Shipping:** DHL India→Germany calculator (`~/plebeian-shop/services/dhl-shipping-tracker.py`)

---

## STRATEGIC FRAMEWORK: FALTIN'S "KOPF SCHLÄGT KAPITAL"

### Core Thesis

> A brilliantly engineered business design matters more than capital or hard labor.
> — Prof. Dr. Günter Faltin

Faltin proved this with the **Teekampagne** (Tea Campaign, 1985): a single product (Darjeeling tea), direct-imported, pre-sold, with everything outsourced, became the world's largest importer of pure Darjeeling. His core mechanics:

### The 5 Faltin Principles

| # | Principle | Our Adaptation |
|---|-----------|----------------|
| 1 | **Radical Simplification** — one product, one size, one source | Pick ONE lead product (Darjeeling tea). Go deep. Other products remain Kalman-tracked intelligence only. |
| 2 | **Bypass Middlemen** — direct import, no wholesalers | Eventually go direct to Darjeeling tea gardens, skip IndiaMART commission. For MVP: use IndiaMART verified suppliers as first source. |
| 3 | **Business Components Model** — outsource everything except the design | 3PL for fulfillment, CoinOS/WoS for payments, Plebeian Market for shop UI, DeepL for translation. Our unique asset: the Kalman price prediction engine. |
| 4 | **Pre-Financing via Crowdsourcing** — collect orders + payment before purchase | Customers pre-order and pre-pay. Their money finances the bulk purchase. Zero inventory risk, negative cash conversion cycle. |
| 5 | **Kopf schlägt Kapital** — the entrepreneurial design IS the competitive advantage | Our Kalman price tracker = the "brain" that times the market. Predict the price bottom → launch pre-order at that moment → lock in margin. |

### The Faltin-Felix Sequence

```
┌──────────────────┐   ┌───────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  1. KALMAN       │   │  2. PRE-ORDER     │   │  3. DIRECT BUY   │   │  4. 3PL FULFILL   │
│  TRACK PRICES    │──▶│  CAMPAIGN LIVE    │──▶│  AT PREDICTED    │──▶│  SHIP TO BUYER   │
│  + PREDICT BOTTOM│   │  COLLECT PAYMENTS  │   │  LOW POINT       │   │  (never touch    │
│  (timing signal)  │   │  (customer = bank) │   │  (customer money) │   │   inventory)      │
└──────────────────┘   └───────────────────┘   └──────────────────┘   └──────────────────┘
```

### Why This Model Wins for India-EU Arbitrage

| Factor | Traditional Importer | Faltin Model | Our Kalman-Augmented Model |
|--------|---------------------|--------------|---------------------------|
| Working capital | €50K+ needed upfront | ZERO — customers pre-pay | ZERO — customers pre-pay |
| Inventory risk | Unsold stock = dead money | Zero — buy only what's pre-sold | Zero + timed to price bottom |
| Margin capture | Wholesale + retail layers | Direct + no layers | Direct + bottom-timed purchase |
| Team size | Warehouse, logistics, CS, buying | 1-2 people (design + coordination) | 1-2 people + Kalman automation |
| Timing | Fixed buying cycles | Anytime pre-order fills | Algorithmically optimized |

### The Trust Hurdle & Nostr Solution

Faltin's biggest operational challenge: convincing strangers to pre-pay for goods arriving in 6-8 weeks.

**Our advantage:** The Nostr/SovEng community is the ideal first audience:
- Persistent identity (npub83d999a1) with verifiable history
- Early adopters who trust peer-to-peer and hate middlemen
- SovEng SEC cohorts provide natural social proof
- Plebeian Market provides escrow/dispute infrastructure
- Public Nostr events for supply chain transparency (sourcing trips, certificates)

**First campaign targets:** SovEng community → then wider Nostr → then general public.

---

## PRODUCT STRATEGY

### Lead Product: Darjeeling Tea

**Why Darjeeling tea first:**
1. Faltin's own Teekampagne proved the market exists
2. Best margin story: ₹360/kg (wholesale India) → €60-120/kg (retail Germany)
3. Seasonal flush periods = natural pre-order calendar
4. Non-perishable, standardized, European customers understand it
5. Already tracked in our price intelligence

**Pricing breakdown (per kg):**

| Cost Component | Amount |
|----------------|--------|
| Wholesale (IndiaMART median) | ₹360 (€3.31) |
| Freight India→Germany (LCL) | ~€2.00 |
| Customs duty (~12% + clearance) | ~€0.80 |
| 3PL receiving + repacking | ~€3.00 |
| DHL last-mile shipping | ~€5.00 |
| **Total cost** | **~€14.11/kg** |
| **Our selling price** | **~~€35-45/kg~~** |
| **Margin** | **~60-70%** |

### Secondary Products (Kalman-tracked, future pre-order cycles)

| Product | IndiaMART Wholesale | German Retail | Potential Margin |
|---------|-------------------|---------------|------------------|
| Kashmiri Saffron (per g) | ₹399/g (€3.66) | €8-15/g | 50-75% |
| Boswellia Serrata (kg) | ₹1,075/kg (€9.87) | €25-50/kg | 60-80% |
| Premium Incense (kg) | ₹200/kg (€1.83) | €15-30/kg | 85-95% |
| Pashmina Shawl | varies | €100-400 | 70-90% |

### Pre-Order Campaign Design

**Format:** Time-limited campaign (2 weeks), Nostr-native, Lightning payments

**Campaign components:**
1. **Transparent pricing breakdown** — show exactly where every euro goes
2. **Sourcing proof** — IndiaMART supplier details, sample photos, certificates
3. **Real-time order progress** — "45% filled — 15 more kg needed to trigger order"
4. **Clear timeline** — "Campaign closes July 20. Orders ship by August 31"
5. **Nostr updates** — weekly kind:1 notes with supply chain progress

**Container math:**
- 1 LCL (Less than Container Load) pallet: ~500 kg
- At €35/kg: €17,500 revenue per campaign
- At 60% margin: ~€10,000 gross profit per campaign
- Timeline: 6-8 weeks from campaign close to customer delivery
- Frequency: every 2-3 months per product (aligned with Kalman buy signals)

---

## IMPLEMENTATION ROADMAP

### Phase 1 — Foundation (THIS WEEK)

| # | Task | Board | Status |
|---|------|-------|--------|
| 1 | **TEA-PREORDER-1:** Design pre-order campaign model | sovereign-shops | READY |
| 2 | **TEA-PREORDER-2:** Source verification — top IndiaMART Darjeeling tea suppliers | sovereign-shops | READY |
| 3 | **TEA-PREORDER-3:** Find + onboard German 3PL for food import fulfillment | sovereign-shops | READY |
| 4 | **KALMAN-DASHBOARD:** Build Kalman price prediction + trend detection pipeline | infrastructure | PLANNED |
| 5 | **KALMAN-VIZ:** HTML dashboard with charts, confidence bands, buy/sell signals | infrastructure | PLANNED |

### Phase 2 — First Campaign (2-3 WEEKS)

| # | Task | Dependencies |
|---|------|-------------|
| 6 | **TEA-PREORDER-4:** Create Plebeian Market pre-order listing | T1, T2, T3 |
| 7 | **LEGAL:** EORI number + Gewerbe registration | — |
| 8 | **LEGAL:** Import compliance checklist (EU food safety, REACH) | — |
| 9 | **PAYMENTS:** CoinOS LN addresses live on all 4 shop npubs | — |
| 10 | **DM-LISTENER:** Auto-responder for order inquiries | — |

### Phase 3 — Scale (MONTH 2+)

| # | Task |
|---|------|
| 11 | Run second tea campaign |
| 12 | Add saffron as second product (Kalman predicts bottom) |
| 13 | Go direct to Darjeeling tea gardens (cut IndiaMART) |
| 14 | Build repeat-customer program (Nostr-based loyalty) |
| 15 | Bürgeramt appointment auction service (separate product line) |

### Phase 4 — Infrastructure Maturation (ONGOING)

| # | Task |
|---|------|
| 16 | Kalman expands to all 5 tracked products |
| 17 | ContextVM hosts dashboard at :9090/api/price-intel/predictions |
| 18 | Anomaly detection → proactive buy signal notifications |
| 19 | Seasonal price model (harvest/flush cycles) |

---

## KEY METRICS & SUCCESS CRITERIA

| Metric | Target |
|--------|--------|
| First pre-order campaign fill rate | ≥80% of container |
| Time from campaign launch to customer delivery | <8 weeks |
| Margin per kg (Darjeeling tea) | ≥60% |
| Customer acquisition cost | <€10 (Nostr organic) |
| Repeat customer rate | ≥30% |
| Kalman prediction accuracy (MAPE) | <15% after 30 days of data |

---

## RISK REGISTER

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Pre-order doesn't fill | Medium | HIGH | Minimum threshold; auto-refund if not met |
| Customs seizes shipment (pesticide/doc issue) | Low | HIGH | Pre-shipment SGS inspection + compliance checklist |
| Shipping delay (canal/port) | Medium | Medium | Buffer timeline + transparent communication |
| Price moves up during campaign | Medium | Low | Fixed-price pre-order locks in margin |
| Quality variance at source | Medium | MEDIUM | Samples verified before campaign launch |
| Trust hurdle (no one pre-pays new brand) | HIGH | HIGH | Start with Nostr/SovEng community, not general public |
| z.ai proxy down (blocks worker dispatch) | HIGH | Medium | Script-only crons work; pre-order tasks are research/ops |

---

## DOCUMENT HISTORY

| Date | Change | Author |
|------|--------|--------|
| 2026-07-03 | Initial creation — Faltin framework integration | Felix Bauer |
