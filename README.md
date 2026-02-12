# EO Strata Demo Simulator

Generates realistic internal monitoring alerts aligned with **live external disruption events** (power outages, ISP issues, weather, disasters, SaaS outages) to demonstrate the Strata External Observability correlation engine.

The tool fetches real events from [publicobservability.io](https://publicobservability.io), uses OpenAI to craft a believable internal alert payload, and sends it to a BigPanda OIM integration — allowing the correlation engine to automatically match the internal alert with its external root cause.

---

## Quick Start

```bash
# 1. Run setup (creates venv, installs deps, creates .env template)
./setup.sh

# 2. Edit .env with your credentials
nano .env

# 3. (Optional) Configure the OIM integration format
./run.sh --setup-oim

# 4. Run the demo
./run.sh
```

## Prerequisites

- **Python 3.10+**
- **BigPanda account** with an OIM (Open Integration Manager) integration created
- **OpenAI API key** with access to `gpt-5-mini` (or your preferred model)

## Environment Variables

Set these in the `.env` file (created by `./setup.sh`):

| Variable | Required | Description |
|----------|----------|-------------|
| `BIGPANDA_ORG_ACCESS_TOKEN` | Yes | BigPanda Org Access Token (Settings → API Keys → Org Access Token) |
| `BIGPANDA_APP_KEY` | Yes | App Key from your OIM integration |
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `BIGPANDA_ALERTS_URL` | No | Override the alerts endpoint (default: `https://integrations.bigpanda.io/oim/api/alerts`) |
| `OPENAI_MODEL` | No | Override the AI model (default: `gpt-5-mini`) |

## How It Works

### 1. Setup OIM Integration (One-Time)

Before sending demo alerts, configure the OIM integration so BigPanda knows how to parse the payload:

```bash
./run.sh --setup-oim
```

This sends a `POST` to `https://integrations.bigpanda.io/configurations/alerts/oim/{app_key}` with:
- **Primary properties**: `host`, `service`, `application`, `cluster`
- **Secondary property**: `check`
- **Status mapping**: `critical`, `warning`, `ok`, `acknowledged`
- **map_remaining**: `true` (unmapped fields automatically become tags)
- A sample payload so BigPanda can validate field references

### 2. Generate & Send Alerts

1. Fetches currently active external events from publicobservability.io
2. Presents event types (power, weather, disaster, user/SaaS) with counts and severity breakdown
3. You filter by type and select a specific event
4. OpenAI generates a realistic **internal** alert that looks like independent monitoring data — but is semantically aligned with the real external event (matching region, service impact, timing)
5. Previews the full payload for review (with option to regenerate)
6. Sends to BigPanda with `eo_correlator: "true"` to trigger the correlation engine
7. Tracks the alert locally for later resolution

### 3. Resolve Alerts

When the demo is over, resolve your alerts to clean up:

- **Interactive**: Choose option 2 from the main menu
- **Quick mode**: `./run.sh --resolve-all`

This sends `status: "ok"` for each tracked alert using the same identity fields, clearing them in BigPanda.

## Alert Payload Format

The tool sends alerts matching BigPanda's OIM Alerts API format:

```json
{
  "status": "critical",
  "host": "us-south-dal-db-primary-01.corp.internal",
  "check": "database_cluster_health",
  "description": "UPS battery backup activated on primary rack cluster...",
  "service": "Core Infrastructure",
  "application": "Enterprise Data Platform",
  "cluster": "us-south-dallas-dc1",
  "instance": "Rack A3 - PDU-Primary-01",
  "location": "Dallas-Fort Worth DC1",
  "environment": "production",
  "cloud_region": "us-south-1",
  "cloud_provider": "hybrid",
  "cloud_account_id": "1234567891011",
  "assignment_group": "Infrastructure - South Region",
  "escalation_group": "VP Infrastructure",
  "business_criticality": "tier 1",
  "known_dependencies": ["AWS Cloud", "Core Switching Fabric - Dallas", "Enterprise Backup Power Grid"],
  "business_owner": "R. Dalton",
  "eo_correlator": "true",
  "timestamp": 1739380000
}
```

**Authentication & routing** (handled separately from the payload body):
- `Authorization: Bearer <BIGPANDA_ORG_ACCESS_TOKEN>` header
- `access_token=<BIGPANDA_ORG_ACCESS_TOKEN>` query parameter
- `app_key=<BIGPANDA_APP_KEY>` query parameter

**Key design choices:**
- **`host`** includes a geography hint matching the external event's location
- **`description`** describes plausible internal symptoms, *not* the external event
- **`location`** maps to the external event's geography in human-readable form
- **`known_dependencies`** lists upstream services/infrastructure relevant to the external event type
- **`eo_correlator: "true"`** ensures the Strata correlator processes this alert
- **`timestamp`** sent as epoch seconds (set automatically at send time)

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Missing dependencies` error | Run `./setup.sh` or activate venv: `source venv/bin/activate` |
| `Missing Configuration` on launch | Edit `.env` with real credentials |
| BigPanda returns 401 | Verify `BIGPANDA_ORG_ACCESS_TOKEN` is a valid Org Access Token (not User API Key) |
| BigPanda returns 400 | Verify `BIGPANDA_APP_KEY` matches your OIM integration |
| OIM setup returns 404 | Ensure the OIM integration exists in BigPanda UI first |
| No events found | publicobservability.io may be temporarily empty; try again shortly |
| OpenAI error | Verify `OPENAI_API_KEY` and that your account has API credits |
