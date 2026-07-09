# Multi-Cloud FinOps Command Center

An enterprise FinOps platform for **AWS, Azure and GCP** — VP/Director dashboards by
application, showback/chargeback with shared-cost allocation, spend baselining, a
**24-month forecast against budget**, and an optimization backlog built from a catalog of
FinOps levers. An **OpenAI + LangGraph** agent team answers questions against the real
data rather than against a language model's memory.

Built to be **tool-agnostic**. Whatever commercial FinOps platform gets procured —
Apptio Cloudability, Tanzu CloudHealth, Flexera One, Finout, Vantage, CloudZero, Harness
CCM, nOps, IBM Turbonomic, ServiceNow — it plugs in behind the same interface, because
everything downstream reads one schema: **FOCUS**.

---

## The one idea

Every source of cost data is normalised to the **FinOps Open Cost and Usage Specification
(FOCUS) 1.2** on the way in.

```
AWS Data Exports (FOCUS_1_2_AWS) ─┐
Azure Exports (FocusCost)         ├─► Connector ─► FOCUS 1.2 DataFrame ─► KPIs
GCP gcp_billing_export_focus_*    │                                        Allocation
Cloudability / CloudHealth /      │                                        Forecast
Flexera / Finout / Vantage /      │                                        Optimize
CloudZero / Harness / nOps        │                                        Anomalies
Kubecost / OpenCost / Turbonomic  │                                        Agents
Any FOCUS CSV or Parquet         ─┘
```

No dashboard, no KPI formula, no optimization detector and no agent tool has ever seen a
vendor-specific field. Adopting a new tool is one subclass of `Connector`. Dropping a
tool is deleting it. That is the whole architectural bet, and it is the same bet the
FinOps Foundation made with FOCUS.

`focus.py` is the contract. Read it first.

---

## Two modes, one code path

| Mode | What it does | Credentials |
|---|---|---|
| **Demo** | Generates a deterministic, utility-shaped FOCUS 1.2 estate in-process — 24 months, 3 clouds, 11 applications, ~55k charge rows | None |
| **Live** | Pulls real billing data through the configured connectors | Streamlit secrets or env |

Demo Mode is not a mock. It is the same `Connector` interface producing a frame that
passes `focus.validate()`, and it contains real things to find:

- a **commitment portfolio** with genuine unused amortisation, so Effective Savings Rate
  and commitment waste are non-trivial numbers rather than round ones;
- **untagged spend** that drags allocation coverage below the chargeback threshold;
- a **shared-services pool** that must be split before showback means anything;
- planted **usage waste** — unattached volumes, idle NAT gateways, unassociated IPs,
  `gp2` volumes, previous-generation instance families, non-prod running 24×7;
- a **step change** from a data-centre exit wave and a **GenAI ramp**, because a forecast
  model that has never met a step change is not worth shipping;
- two injected **anomalies** for the detector to catch;
- real **seasonality** — summer cooling load, autumn storm season, winter steam.

Switch modes from the sidebar. Nothing below `data.py` knows which one is active.

---

## What's in it

**Executive** — the VP/Director view. Total spend with MoM and YoY, forecast against
budget, projected year-end variance, Effective Savings Rate, commitment coverage and
utilisation, cost of waste, allocation coverage.

**Applications** — spend by application, business unit and environment.

**Showback & Chargeback** — direct costs, the shared-cost pool, and the split. Five
allocation methods: direct, even split, proportional-to-direct-spend, fixed percentage,
usage driver. Invoice-shaped chargeback output.

**Baseline** — current run-rate against a trailing 90-day baseline, seasonally aware.
Catches the well-documented decay where optimisation gains erode from month three.

**Forecast & Budget** — 24-month forecast with 80% and 95% prediction intervals, budget
overlay, variance waterfall, and a **commitment-expiry overlay**: when an RI or Savings
Plan term ends, the on-demand rate snaps back, and a pure trend model will walk straight
through that cliff. Backtested with WAPE and mapped to the Foundation's forecast-variance
maturity bands (Crawl < 20%, Walk < 15%, Run < 12%, best-in-class < 5%).

**Optimize** — a catalog of FinOps levers across Rate, Usage, Architecture and AI/GPU,
each with a savings range, effort, risk, prerequisites and a cited source. Rule-based
detectors run over the FOCUS frame to turn levers into costed opportunities, sequenced
into a delivery roadmap.

**Anomalies** — STL decomposition plus a median-absolute-deviation test on the residual,
so weekday and monthly cycles don't trip alerts. Mirrors AWS Cost Anomaly Detection's
semantics: dynamic thresholds, ≥10-day warm-up.

**Unit Economics** — cost per business driver. For a utility that means cost per customer
served, per kWh delivered, per meter read, per work order closed — not cost per vCPU,
which is just the bill with a different name on it.

**Governance** — tag coverage per canonical key, untagged breakdown, chargeback
readiness, and the per-cloud tagging constraints that bite in practice.

**AI Copilot** — a LangGraph supervisor routing to four specialists aligned to the FinOps
Framework domains. Every number it quotes came from a tool call against the actual frame.

**Integrations** — every connector, its auth scheme, its capabilities, its FOCUS support,
and a live connection test.

**Reference** — the FinOps Framework (domains, capabilities, phases, maturity, personas)
and the FOCUS schema, rendered from the same constants the code uses.

---

## KPI definitions

Every number is computed once, in `kpi.py`. Executive KPIs read `EffectiveCost`
(amortised) — blended and unblended views make commitment purchases look like lumpy
spikes and have no place on a leadership dashboard.

```
Effective Savings Rate   = (savings − cost to achieve) / on-demand-equivalent spend
                           ≈ Utilization × Coverage × Discount
Commitment Coverage %    = committed eligible spend / total eligible spend   (ListCost basis)
Commitment Utilization % = used commitment / total commitment purchased
Commitment Waste $       = EffectiveCost where CommitmentDiscountStatus == 'Unused'
Cost of Waste            = commitment waste + usage waste (idle / orphaned / over-provisioned)
Allocation Coverage %    = allocated cost / total cost
Unit Cost                = total cost / business demand driver
Variance $               = Actual − Budget            (positive = overrun)
Projected YE Variance    = (Actuals_YTD + Forecast_remaining) − Annual Budget
Run-Rate (annualised)    = daily burn rate × 365
WAPE                     = Σ|Actual − Forecast| / Σ|Actual|      (dollar-weighted)
```

`ListCost` is FOCUS's on-demand-equivalent, which is precisely the ESR denominator the
Foundation specifies. ESR is the metric to trend, because 100% commitment coverage at 60%
utilisation is still a bad deal and only ESR shows that.

Sources: [Effective Savings Rate](https://www.finops.org/wg/how-to-calculate-effective-savings-rate-esr/) ·
[Commitment waste](https://www.finops.org/wg/percent-commitment-based-discount-waste-playbook/) ·
[Allocation](https://www.finops.org/framework/capabilities/allocation/) ·
[Forecasting](https://www.finops.org/framework/capabilities/forecasting/) ·
[Unit economics](https://www.finops.org/framework/capabilities/unit-economics/)

---

## Run it locally

```bash
git clone https://github.com/ajittgosavii/multicldfinops.git
cd multicldfinops
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

It opens in Demo Mode. No credentials needed.

---

## Deploy to Streamlit Cloud

1. Point a new app at this repo, main file `app.py`.
2. Add secrets (**Manage app → Settings → Secrets**):

```toml
# Optional: gate the app
APP_PASSWORD = "..."

# Optional: AI Copilot. Without this the tab explains what it would have done.
OPENAI_API_KEY = "sk-..."
OPENAI_MODEL = "gpt-5"
OPENAI_MODEL_FAST = "gpt-5-mini"

# Optional: durable scenarios/policies. Without it, SQLite — wiped on reboot.
DATABASE_URL = "postgresql://..."

ORGANISATION = "Con Edison"

# Live Mode
FINOPS_MODE = "live"

AWS_ACCESS_KEY_ID = "..."
AWS_SECRET_ACCESS_KEY = "..."
AWS_FOCUS_S3_URI = "s3://bucket/prefix"      # a FOCUS 1.2 Data Export

AZURE_TENANT_ID = "..."
AZURE_CLIENT_ID = "..."
AZURE_CLIENT_SECRET = "..."
AZURE_SUBSCRIPTION_ID = "..."

GCP_SERVICE_ACCOUNT_JSON = '{"type":"service_account", ...}'
GCP_PROJECT_ID = "..."
GCP_BILLING_ACCOUNT_ID = "01A2B3-C4D5E6-F70819"
```

**Reboot the app after each push.** Streamlit Cloud reruns `app.py` but often keeps stale
submodules in `sys.modules`.

Live Mode extras (`boto3`, `google-cloud-bigquery`, `s3fs`, …) are commented at the
bottom of `requirements.txt`. Every SDK import is lazy, so uncomment only the clouds you
actually connect.

---

## Connecting a procured FinOps tool

Three routes, in order of preference:

**1. FOCUS export.** If the tool emits FOCUS — CloudZero, Vantage — point
`FocusFileConnector` at the file or object-store URI. Zero code.

**2. A vendor connector.** Eleven are implemented in `connectors/vendors/`. Set the
secrets, pick it in the sidebar. Each documents its own auth scheme, endpoints, rate
limits and FOCUS support, with the doc URL beside each endpoint constant.

**3. A new subclass.** Implement `spec`, `test_connection()` and `fetch_costs()` returning
a `focus.normalize()`d frame, then add one line to `connectors/__init__.py:REGISTRY`.

```python
class AcmeConnector(Connector):
    @property
    def spec(self) -> ConnectorSpec: ...
    def test_connection(self) -> ConnectionResult: ...
    def fetch_costs(self, start, end) -> pd.DataFrame: ...
```

Rate limits worth knowing before you write the integration: CloudZero allows **60 cost
requests per day**. AWS Cost Explorer bills roughly **$0.01 per request**. CloudHealth's
REST GET URIs are capped at **4,000 characters** and its GraphQL access token lives **15
minutes**. Finout caps a query window at **60 days** and spells the same field
`unixTimeMillisecondsStart` in v2 but `unixTimeMillSecondsStart` in v1. Each connector's
module docstring leads with the one thing that will bite you.

---

## Architecture

```
app.py                  chrome: mode switch, the single filter row, tab registry
data.py                 the ONLY place Demo and Live diverge
finops_core.py          Mode, AppConfig, DataContext, FinOps Framework vocabulary
focus.py                FOCUS 1.2 schema, enums, validation, tag normalisation
kpi.py                  every executive formula, computed once

connectors/
  base.py               the Connector contract
  demo.py               synthetic estate generator
  aws_native.py         Cost Explorer + Data Exports (FOCUS_1_2_AWS) + Cost Optimization Hub
  azure_native.py       Cost Management query/forecast + FocusCost exports + Advisor
  gcp_native.py         BigQuery billing export + Recommender API + Budgets
  focus_file.py         any FOCUS CSV/Parquet, local or s3://, az://, gs://
  vendors/              Cloudability, CloudHealth, Flexera, Finout, Vantage, CloudZero,
                        Harness, nOps, Kubecost, OpenCost, Turbonomic, ServiceNow

allocation.py           showback, chargeback, shared-cost splitting
forecast.py             Holt-Winters / SARIMA / ensemble, PIs, commitment-expiry overlay
budget.py               variance, bridge, year-end projection, burn-down
anomaly.py              STL + MAD on the residual
optimize.py             lever catalog + rule-based detectors over the FOCUS frame

agents/                 LangGraph supervisor + 4 specialists, OpenAI tools over the frame
tabs/                   one module per tab, each exposing render(ctx)
theme.py                design tokens; the categorical palette is CVD-validated
charts.py / ui.py       Plotly and Streamlit component vocabulary
store.py                pluggable SQLite/Postgres for scenarios and policies
```

---

## Design notes

The categorical palette is not chosen by taste. Slot order was picked by maximising the
minimum adjacent colour distance under simulated colour-vision deficiency, then validated
against both surfaces (dark worst-adjacent ΔE 23.6; light 13.3). Colour follows the
entity, never its rank — filtering out a cloud does not repaint the survivors. There are
no dual-axis charts. Every chart has a table-view twin, so no value is reachable only
through a tooltip.

---

## Tests

```bash
python -m pytest -q
```

Covers FOCUS conformance of the generated estate, the KPI formulas, forecast prediction
intervals and backtest, anomaly detection against the two planted spikes, allocation
totals reconciling to the estate, the lever detectors, and that the agent layer imports
and its tools run with **no** `OPENAI_API_KEY` present.

---

## Honest limitations

- **Cost Explorer has no list price.** `ListCost` is set equal to `BilledCost` on that
  path, so Effective Savings Rate is understated. Use the FOCUS Data Export for the real
  number. The connector says so in its docstring rather than fabricating a rate.
- **Business drivers cannot be read from a cloud bill**, by definition. Live Mode returns
  an empty drivers frame; unit economics needs a feed from a system of record.
- Several vendor endpoints are marked `[UNVERIFIED]` in code where public documentation
  was thin — notably ServiceNow's Cloud Cost Management table names, which must be
  enumerated in the customer's own instance.
- Storage-tiering and rightsizing detectors infer from billing data alone. Where a
  detector cannot see what it needs (access patterns, CPU utilisation) it lowers its
  confidence and says what telemetry would confirm it.
- Savings percentages in the lever catalog are vendor "up-to" figures. Treat them as
  ceilings.
- Demo data is synthetic. Every dollar in it is invented.

---

## License

MIT
