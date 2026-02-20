#!/usr/bin/env python3
"""
EO Strata Demo Simulator
=========================
Generates realistic internal alerts aligned with live external events
for demonstrating the Strata External Observability correlation engine.

Usage:
    python demo_sim.py                  Interactive mode (default)
    python demo_sim.py --resolve-all    Quick-resolve all active alerts
    python demo_sim.py --setup-oim      Configure the BigPanda OIM integration

Environment Variables (set in .env):
    BIGPANDA_ORG_ACCESS_TOKEN  - Bearer token (Org Token) for BigPanda API
    BIGPANDA_APP_KEY    - App key from OIM integration
    OPENAI_API_KEY      - OpenAI API key
"""

import os
import sys
import json
import time
import argparse
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Dependency Check ────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    from openai import OpenAI
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich import box
except ImportError:
    print("\n[ERROR] Missing dependencies. Please run setup first:")
    print("  ./setup.sh")
    print("\nOr manually:")
    print("  python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt")
    sys.exit(1)


# ─── Constants ───────────────────────────────────────────────────────────────

PO_API_URL = "https://publicobservability.io/summary/current"
BP_ALERTS_URL = "https://integrations.bigpanda.io/oim/api/alerts"
OIM_CONFIG_BASE_URL = "https://integrations.bigpanda.io/configurations/alerts/oim"
SENT_ALERTS_FILE = ".demo_sent_alerts.json"
DEFAULT_MODEL = "gpt-5-mini"
MAX_EVENT_AGE_HOURS = 15  # Exclude events with start_time older than this


# ─── OIM Integration Configuration Payload ───────────────────────────────────
# This defines how BigPanda parses incoming alert payloads for this integration.
# Sent once via POST to configure the integration before demo use.

OIM_SAMPLE_PAYLOAD = {
    "eo_correlator": "true",
    "cluster": "app_srv_cluster",
    "application": "Customer Account Management",
    "service": "Customer User Experience",
    "host": "app-srv-1.bigpanda.io",
    "description": "Customer Portal Application - Web server not responding",
    "check": "Synthetic Test - Web Application Service",
    "status": "critical",
    "instance": "Port 443 - https://customer-portal.bigpanda.io/",
    "cloud_region": "us-east-1",
    "cloud_provider": "aws",
    "cloud_account_id": "1234567891011",
    "assignment_group": "Application Team - Web Services",
    "escalation_group": "Application Team - Management",
    "business_criticality": "tier 1",
    "known_dependencies": [
        "AWS Cloud",
        "AWS Lambda",
        "Customer Identity and Access Management",
        "Point of Presence - New York | CenturyLink / Lumen T3 (1000 Mbps)",
    ],
    "business_owner": "B. Panda",
    "timestamp": 1402302570,
}

OIM_CONFIG_PAYLOAD = {
    "config": {
        "map_remaining": True,
        "is_array": False,
        "secondary_property": [{"name": "check"}],
        "map_remaining_flatten_arrays": False,
        "primary_property": [
            {"name": "host"},
            {"name": "service"},
            {"name": "application"},
            {"name": "cluster"},
        ],
        "version": "2.0",
        "force_lowercase": False,
        "additional_attributes": [
            {"name": "host", "source": ["host", "device"]},
            {"name": "check", "source": ["check"]},
            {"name": "service", "source": ["service"]},
            {"name": "application", "source": ["application"]},
            {"name": "cluster", "source": ["cluster"]},
            {"name": "description", "source": ["description"]},
        ],
        "status": {
            "default_to": "critical",
            "status_map": {
                "warning": ["warning"],
                "acknowledged": ["acknowledged"],
                "critical": ["critical"],
                "ok": ["ok"],
                "unknown": ["unknown"],
            },
            "source": ["status"],
        },
        "timestamp": {"source": ["timestamp"]},
    },
    "sample_payload": json.dumps(OIM_SAMPLE_PAYLOAD),
}


# ─── LLM System Prompt ──────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an internal IT monitoring alert generator for a Fortune 500 company.

Given details about a real external event (power outage, weather event, ISP disruption,
natural disaster, SaaS outage, etc.), generate a realistic INTERNAL monitoring alert
that would plausibly be triggered as a result of this external event.

IMPORTANT RULES:
1. The alert describes INTERNAL symptoms observed by monitoring tools — NOT the external event itself.
2. It should look like it came from an enterprise monitoring system (Datadog, Nagios, Zabbix, SolarWinds, etc.).
3. The hostname MUST include a location-relevant identifier that maps to the event's geography.
4. The description must be 2-3 sentences of realistic monitoring language describing internal symptoms.
5. DO NOT directly reference the external event in the description — it must appear to be an independent internal observation.
6. The cluster should use a datacenter/region naming convention relevant to the geography.
7. known_dependencies MUST be an array of 2-5 realistic upstream service or infrastructure names.

FIELD REFERENCE AND EXAMPLES:

host — Realistic internal FQDN with location hint.
  Examples: "us-south-dal-db-primary-01.corp.internal", "020-rtr-1.ganani.io", "connmon.ca.bigpanda.io"

check — Monitoring check or synthetic test name.
  Examples: "Synthetic Test - Web Application Service", "Connectivity lost", "Connection Monitor - ISP Outbound Test - Google - Redwood City, CA Office"

description — 2-3 sentences of internal monitoring language. Describe symptoms, not the external cause.
  Examples:
    "UPS battery backup activated on primary rack cluster. Utility power feed interrupted to pods A3-A7. Generator failover initiated but 3 of 12 compute nodes experienced ungraceful shutdown during switchover."
    "5 out of 5 tests have failed - Check Internet Service Connectivity - AT&T MPLS PATH VIA RWC-INT-RTR1"
    "Customer Portal Application - Web server not responding"

service — Internal service or team name.
  Examples: "Customer User Experience", "Network", "AT&T Internet"

application — Business application name.
  Examples: "Customer Account Management", "Order Processing Platform", "HR Self-Service Portal"

cluster — Infrastructure group or cluster identifier.
  Examples: "app_srv_cluster", "us-east-db-cluster", "edge-network-west"

instance — Specific endpoint, port, or resource being monitored.
  Examples: "Port 443 - https://customer-portal.bigpanda.io/", "MPLS Circuit ID: ATT-DFW-0042"

location — Human-readable site or facility location.
  Examples: "020 - Union, New Jersey", "RWC-Datacenter", "Dallas-Fort Worth DC1"

environment — Deployment environment.
  Must be one of: "production", "staging", "development", "dr"

cloud_region — Cloud provider region code.
  Examples: "us-east-1", "us-west-2", "eu-west-1", "eastus2"

cloud_provider — Cloud provider name.
  Must be one of: "aws", "azure", "gcp", "on-prem", "hybrid"

cloud_account_id — Cloud account or subscription identifier (fake but realistic).
  Examples: "1234567891011", "sub-9a8b7c6d-prod"

assignment_group — Team responsible for first response.
  Examples: "Application Team - Web Services", "NOC - Network Operations", "Infrastructure - East Region"

escalation_group — Team for escalation.
  Examples: "Application Team - Management", "VP Infrastructure", "Site Reliability Engineering"

business_criticality — Impact tier.
  Must be one of: "tier 1", "tier 2", "tier 3"

known_dependencies — Array of 2-5 upstream service/infrastructure dependencies that relate to the event.
  Examples: ["AWS Cloud", "AWS Lambda", "Customer Identity and Access Management", "Point of Presence - New York | CenturyLink / Lumen T3 (1000 Mbps)"]

business_owner — Fictional person name.
  Examples: "B. Panda", "J. Martinez", "S. Chen"

EXAMPLE OUTPUT for "Power outage in Dallas, TX":
{
  "host": "us-south-dal-db-primary-01.corp.internal",
  "check": "database_cluster_health",
  "description": "UPS battery backup activated on primary rack cluster. Utility power feed interrupted to pods A3-A7. Generator failover initiated but 3 of 12 compute nodes experienced ungraceful shutdown during switchover.",
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
  "known_dependencies": ["AWS Cloud", "Core Switching Fabric - Dallas", "Enterprise Backup Power Grid", "Point of Presence - Dallas | AT&T MPLS (10 Gbps)"],
  "business_owner": "R. Dalton"
}

Respond with ONLY a valid JSON object containing ALL of the fields listed above. No additional text."""


# ─── Helpers ─────────────────────────────────────────────────────────────────

def truncate(text, max_len, suffix="..."):
    """Truncate text to max_len, appending suffix if truncated."""
    if not text:
        return "N/A"
    return (text[: max_len - len(suffix)] + suffix) if len(text) > max_len else text


# ─── Main Class ──────────────────────────────────────────────────────────────


class DemoSimulator:
    """Interactive demo simulator for Strata EO correlation demos."""

    def __init__(self):
        self.console = Console()
        self.sent_alerts = []
        self.events = []
        self._load_config()
        self._load_sent_alerts()

    # ── Configuration ────────────────────────────────────────────────────

    def _load_config(self):
        """Load configuration from .env file and environment."""
        env_path = Path(__file__).parent / ".env"
        load_dotenv(env_path)

        self.bp_org_token = os.getenv("BIGPANDA_ORG_ACCESS_TOKEN", "")
        self.bp_app_key = os.getenv("BIGPANDA_APP_KEY", "")
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.bp_alerts_url = os.getenv("BIGPANDA_ALERTS_URL", BP_ALERTS_URL)
        self.openai_model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)

    def _validate_config(self):
        """Check all required environment variables are set. Returns list of missing var names."""
        missing = []
        placeholders = ("your_", "sk-your", "BPUAK-your")

        for var_name, value in [
            ("BIGPANDA_ORG_ACCESS_TOKEN", self.bp_org_token),
            ("BIGPANDA_APP_KEY", self.bp_app_key),
            ("OPENAI_API_KEY", self.openai_api_key),
        ]:
            if not value or any(value.startswith(p) for p in placeholders):
                missing.append(var_name)
        return missing

    def _show_config_help(self, missing):
        """Show guided help for setting up missing configuration."""
        self.console.print()
        self.console.print(
            Panel(
                "[bold red]Missing Configuration[/bold red]\n\n"
                "The following environment variables must be set before running the demo.",
                border_style="red",
            )
        )

        help_text = {
            "BIGPANDA_ORG_ACCESS_TOKEN": (
                "Your BigPanda Org Token (Bearer token).\n"
                "    [dim]Find it: BigPanda → Settings → API Keys → Org Token[/dim]"
            ),
            "BIGPANDA_APP_KEY": (
                "The App Key from your OIM integration.\n"
                "    [dim]Find it: BigPanda → Integrations → Open Integration Manager → "
                "select your integration → App Key[/dim]"
            ),
            "OPENAI_API_KEY": (
                "Your OpenAI API key.\n"
                "    [dim]Find it: https://platform.openai.com/api-keys[/dim]"
            ),
        }

        for var in missing:
            self.console.print(f"\n  [yellow]●[/yellow] [bold]{var}[/bold]")
            self.console.print(f"    {help_text.get(var, '')}")

        env_path = Path(__file__).parent / ".env"
        self.console.print("\n[bold]How to fix:[/bold]")
        self.console.print(f"  1. Open [cyan]{env_path}[/cyan] in any text editor")
        self.console.print("  2. Replace the placeholder values with your real credentials")
        self.console.print("  3. Save and re-run: [cyan]./run.sh[/cyan]")

        if not env_path.exists():
            self.console.print(
                "\n  [yellow]Tip:[/yellow] No .env file found. Run [cyan]./setup.sh[/cyan] first "
                "to create one from the template."
            )
        self.console.print()

    # ── Sent Alert Tracking ──────────────────────────────────────────────

    def _load_sent_alerts(self):
        """Load previously sent alerts from the local tracking file."""
        alerts_path = Path(__file__).parent / SENT_ALERTS_FILE
        if alerts_path.exists():
            try:
                with open(alerts_path, "r") as f:
                    self.sent_alerts = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.sent_alerts = []

    def _save_sent_alerts(self):
        """Persist sent alerts to the local tracking file."""
        alerts_path = Path(__file__).parent / SENT_ALERTS_FILE
        with open(alerts_path, "w") as f:
            json.dump(self.sent_alerts, f, indent=2)

    # ── UI Components ────────────────────────────────────────────────────

    def _show_banner(self):
        """Display the application banner."""
        banner = Text()
        banner.append("EO Strata Demo Simulator\n", style="bold cyan")
        banner.append("External Observability Correlation Engine\n", style="dim")
        banner.append("─" * 46 + "\n", style="dim cyan")
        banner.append("Generates realistic internal alerts aligned\n")
        banner.append("with live external disruption events to\n")
        banner.append("demonstrate automatic correlation.\n\n", style="")
        banner.append(f"Model: {self.openai_model}  ", style="dim")
        banner.append(f"Target: {self.bp_alerts_url}", style="dim")
        self.console.print(Panel(banner, box=box.DOUBLE, border_style="cyan"))

    def _show_main_menu(self):
        """Display the main menu and return the user's choice."""
        active_count = len([a for a in self.sent_alerts if a.get("status") != "ok"])

        self.console.print()
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        table.add_column(style="bold cyan", width=4, justify="right")
        table.add_column()
        table.add_row("1.", "Generate & Send Alert  [dim](from live events)[/dim]")
        table.add_row("2.", f"Resolve Previous Alerts [dim]({active_count} active)[/dim]")
        table.add_row("3.", f"View Sent Alerts       [dim]({len(self.sent_alerts)} total)[/dim]")
        table.add_row("4.", "Setup OIM Integration  [dim](one-time config)[/dim]")
        table.add_row("5.", "Exit")

        self.console.print(
            Panel(
                table,
                title="[bold]Main Menu[/bold]",
                box=box.ROUNDED,
                border_style="cyan",
            )
        )

        return Prompt.ask("Choose an option", choices=["1", "2", "3", "4", "5"], default="1")

    # ── Event Fetching ───────────────────────────────────────────────────

    def fetch_events(self):
        """Fetch current active events from publicobservability.io."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True,
        ) as progress:
            task = progress.add_task(
                "Fetching live events from publicobservability.io...", total=None
            )

            try:
                response = requests.get(
                    PO_API_URL,
                    headers={"Accept": "application/json"},
                    params={"limit": 1000},
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()

                events = data.get("alerts", [])

                # Filter: active only, no future start_times, no stale events
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(hours=MAX_EVENT_AGE_HOURS)
                active_events = []
                skipped_future = 0
                skipped_stale = 0

                for e in events:
                    if not e.get("is_active", False):
                        continue

                    start_str = e.get("start_time")
                    if start_str:
                        try:
                            start_dt = datetime.fromisoformat(
                                start_str.replace("Z", "+00:00")
                            )
                            if start_dt > now:
                                skipped_future += 1
                                continue
                            if start_dt < cutoff:
                                skipped_stale += 1
                                continue
                        except (ValueError, TypeError):
                            pass  # Keep events with unparseable times

                    active_events.append(e)

                self.events = active_events

                filter_note = ""
                if skipped_future or skipped_stale:
                    filter_note = (
                        f" | excluded: {skipped_future} future, "
                        f"{skipped_stale} older than {MAX_EVENT_AGE_HOURS}h"
                    )

                progress.update(
                    task,
                    description=(
                        f"[green]Fetched {len(active_events)} active events "
                        f"(of {data.get('total_count', len(events))} total)"
                        f"{filter_note}[/green]"
                    ),
                )
                return active_events

            except requests.RequestException as e:
                progress.update(task, description="[red]Failed to fetch events[/red]")
                self.console.print(f"\n[red]Error fetching events:[/red] {e}")
                return []

    # ── Event Display & Selection ────────────────────────────────────────

    def display_type_summary(self, events):
        """Display a summary table of event types with counts. Returns {number: type_name} map."""
        type_counts = {}
        type_severities = {}
        for event in events:
            atype = event.get("alert_type", "unknown")
            type_counts[atype] = type_counts.get(atype, 0) + 1
            sev = event.get("severity", "low")
            if atype not in type_severities:
                type_severities[atype] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
            type_severities[atype][sev] = type_severities[atype].get(sev, 0) + 1

        table = Table(
            title="Available Event Types",
            box=box.ROUNDED,
            header_style="bold cyan",
            title_style="bold",
        )
        table.add_column("#", style="dim", width=4, justify="right")
        table.add_column("Alert Type", style="bold", min_width=12)
        table.add_column("Count", justify="right", width=7)
        table.add_column("Crit/High/Med/Low", justify="center", width=20)
        table.add_column("Example", style="dim", max_width=45)

        sorted_types = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
        type_map = {}

        for i, (atype, count) in enumerate(sorted_types, 1):
            example = next((e for e in events if e.get("alert_type") == atype), None)
            example_title = truncate(example.get("title", ""), 43) if example else ""

            sevs = type_severities.get(atype, {})
            sev_str = (
                f"[red]{sevs.get('critical', 0)}[/red]/"
                f"[bold yellow]{sevs.get('high', 0)}[/bold yellow]/"
                f"[yellow]{sevs.get('medium', 0)}[/yellow]/"
                f"[green]{sevs.get('low', 0)}[/green]"
            )

            table.add_row(str(i), atype, str(count), sev_str, example_title)
            type_map[i] = atype

        self.console.print()
        self.console.print(table)
        return type_map

    def select_alert_types(self, type_map):
        """Prompt user to select alert types. Returns list of type name strings."""
        self.console.print()
        selection = Prompt.ask(
            "Select event types [dim](comma-separated numbers, or 'all')[/dim]",
            default="all",
        )

        if selection.strip().lower() == "all":
            return list(type_map.values())

        try:
            indices = [int(x.strip()) for x in selection.split(",")]
            selected = [type_map[i] for i in indices if i in type_map]
            if not selected:
                self.console.print("[yellow]No valid types selected, showing all.[/yellow]")
                return list(type_map.values())
            return selected
        except ValueError:
            self.console.print("[yellow]Invalid input, showing all types.[/yellow]")
            return list(type_map.values())

    def display_events(self, events, selected_types):
        """Display events filtered by selected types. Returns (filtered_list, {number: event} map)."""
        filtered = [e for e in events if e.get("alert_type") in selected_types]

        if not filtered:
            self.console.print("[yellow]No events found for the selected types.[/yellow]")
            return [], {}

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        filtered.sort(
            key=lambda x: (
                severity_order.get(x.get("severity", "low"), 3),
                x.get("start_time", ""),
            )
        )

        table = Table(
            title=f"Active Events ({len(filtered)} matching)",
            box=box.ROUNDED,
            header_style="bold cyan",
            title_style="bold",
            show_lines=True,
        )
        table.add_column("#", style="dim", width=4, justify="right")
        table.add_column("Type", width=10)
        table.add_column("Sev", width=9)
        table.add_column("Title", max_width=50)
        table.add_column("Location", max_width=30)

        display_limit = 40
        event_map = {}

        for i, event in enumerate(filtered[:display_limit], 1):
            severity = event.get("severity", "unknown")
            sev_style = {
                "critical": "bold red",
                "high": "bold yellow",
                "medium": "yellow",
                "low": "green",
            }.get(severity, "dim")

            location = event.get("location", {}).get("description", "N/A")
            title = event.get("title", "N/A")

            table.add_row(
                str(i),
                event.get("alert_type", "?"),
                f"[{sev_style}]{severity}[/{sev_style}]",
                truncate(title, 48),
                truncate(location, 28),
            )
            event_map[i] = event

        self.console.print()
        self.console.print(table)

        if len(filtered) > display_limit:
            self.console.print(
                f"[dim]Showing first {display_limit} of {len(filtered)} events. "
                f"Narrow your type selection for more specific results.[/dim]"
            )

        return filtered, event_map

    def select_event(self, event_map):
        """Prompt user to select one event by number."""
        self.console.print()
        try:
            raw = Prompt.ask(
                "Select event # to base the internal alert on [dim](or 'q' to go back)[/dim]"
            )
            if raw.strip().lower() in ("q", "quit", "exit", "back"):
                return None
            selection = int(raw.strip())
            if selection in event_map:
                return event_map[selection]
            self.console.print(f"[yellow]Invalid selection: {selection}[/yellow]")
            return None
        except (ValueError, EOFError, KeyboardInterrupt):
            return None

    def show_event_detail(self, event):
        """Display detailed view of a selected event."""
        desc = event.get("description", "N/A")
        if len(desc) > 300:
            desc = desc[:297] + "..."

        detail = (
            f"[bold]{event.get('title', 'N/A')}[/bold]\n\n"
            f"[dim]Type:[/dim]        {event.get('alert_type', 'N/A')}\n"
            f"[dim]Severity:[/dim]    {event.get('severity', 'N/A')}\n"
            f"[dim]Location:[/dim]    {event.get('location', {}).get('description', 'N/A')}\n"
            f"[dim]Start Time:[/dim]  {event.get('start_time', 'N/A')}\n"
            f"[dim]Source:[/dim]      {event.get('source_system', 'N/A')}\n"
            f"[dim]Affected:[/dim]    {event.get('affected_count', 'N/A')}\n\n"
            f"[dim]Description:[/dim]\n{desc}"
        )
        self.console.print(Panel(detail, title="Selected External Event", border_style="blue"))

    # ── Alert Generation (LLM) ───────────────────────────────────────────

    def generate_internal_alert(self, event):
        """Use OpenAI to generate a realistic internal alert payload aligned with the event."""
        client = OpenAI(api_key=self.openai_api_key)

        location_desc = event.get("location", {}).get("description", "Unknown location")
        affected = event.get("affected_count")
        affected_str = f"{affected:,}" if affected else "N/A"

        user_prompt = (
            f"Generate a realistic internal monitoring alert based on this external event:\n\n"
            f"Type: {event.get('alert_type', 'unknown')}\n"
            f"Title: {event.get('title', 'N/A')}\n"
            f"Description: {event.get('description', 'N/A')}\n"
            f"Severity: {event.get('severity', 'unknown')}\n"
            f"Location: {location_desc}\n"
            f"Start Time: {event.get('start_time', 'N/A')}\n"
            f"Source: {event.get('source_system', 'N/A')}\n"
            f"Affected Count: {affected_str}\n\n"
            f"Remember: Generate INTERNAL monitoring symptoms that would plausibly result "
            f"from this external event. Do NOT mention the external event directly. "
            f"Include ALL fields from the schema."
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True,
        ) as progress:
            task = progress.add_task("Generating internal alert with AI...", total=None)

            try:
                response = client.chat.completions.create(
                    model=self.openai_model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    max_completion_tokens=2000,
                )

                result = json.loads(response.choices[0].message.content)
                progress.update(
                    task, description="[green]Alert generated successfully[/green]"
                )
                return result

            except json.JSONDecodeError as e:
                progress.update(
                    task, description="[red]Failed to parse AI response[/red]"
                )
                raw = getattr(response.choices[0].message, "content", None) or ""
                if not raw.strip():
                    self.console.print(
                        "\n[red]AI returned an empty response.[/red] "
                        "[dim]This can happen intermittently. Please try again.[/dim]"
                    )
                else:
                    self.console.print(f"\n[red]JSON parse error:[/red] {e}")
                    self.console.print(f"[dim]Raw response: {raw[:300]}[/dim]")
                return None
            except Exception as e:
                progress.update(task, description="[red]Failed to generate alert[/red]")
                self.console.print(f"\n[red]Error generating alert:[/red] {e}")
                return None

    # ── BigPanda Payload Assembly ────────────────────────────────────────

    def build_bigpanda_payload(self, generated_alert, status="critical"):
        """Assemble the full BigPanda OIM alert payload from LLM output.

        The payload body contains the alert content.  Authentication (Bearer token)
        and routing (app_key) are handled via headers/query params at send time.
        """
        # Ensure known_dependencies is a list
        deps = generated_alert.get("known_dependencies", [])
        if isinstance(deps, str):
            deps = [deps]

        return {
            # ── Required fields ──────────────────────────────────
            "status": status,
            "host": generated_alert.get("host", "unknown-host"),
            "check": generated_alert.get("check", "unknown_check"),
            "description": generated_alert.get("description", "No description"),
            # ── Primary properties ───────────────────────────────
            "service": generated_alert.get("service", ""),
            "application": generated_alert.get("application", ""),
            "cluster": generated_alert.get("cluster", ""),
            "instance": generated_alert.get("instance", ""),
            # ── Location & environment ───────────────────────────
            "location": generated_alert.get("location", ""),
            "environment": generated_alert.get("environment", "production"),
            # ── Cloud context ────────────────────────────────────
            "cloud_region": generated_alert.get("cloud_region", ""),
            "cloud_provider": generated_alert.get("cloud_provider", ""),
            "cloud_account_id": generated_alert.get("cloud_account_id", ""),
            # ── ITSM / operational context ───────────────────────
            "assignment_group": generated_alert.get("assignment_group", ""),
            "escalation_group": generated_alert.get("escalation_group", ""),
            "business_criticality": generated_alert.get("business_criticality", ""),
            "known_dependencies": deps,
            "business_owner": generated_alert.get("business_owner", ""),
            # ── Correlation trigger ──────────────────────────────
            "eo_correlator": "true",
            # ── Timestamp (epoch seconds) ────────────────────────
            "timestamp": int(time.time()),
        }

    def preview_payload(self, payload, event=None):
        """Display a formatted preview of the payload before sending."""
        table = Table(
            title="BigPanda OIM Alert Payload",
            box=box.HEAVY,
            header_style="bold green",
            title_style="bold",
        )
        table.add_column("Field", style="bold", width=24)
        table.add_column("Value", max_width=68)

        # Fields in logical display order
        display_fields = [
            ("status", "status"),
            ("host", "host"),
            ("check", "check"),
            ("description", "description"),
            ("service", "service"),
            ("application", "application"),
            ("cluster", "cluster"),
            ("instance", "instance"),
            ("location", "location"),
            ("environment", "environment"),
            ("cloud_region", "cloud_region"),
            ("cloud_provider", "cloud_provider"),
            ("cloud_account_id", "cloud_account_id"),
            ("assignment_group", "assignment_group"),
            ("escalation_group", "escalation_group"),
            ("business_criticality", "business_criticality"),
            ("known_dependencies", "known_dependencies"),
            ("business_owner", "business_owner"),
            ("eo_correlator", "eo_correlator"),
            ("timestamp", "timestamp"),
        ]

        style_map = {
            "status": lambda v: (
                "[bold red]critical[/bold red]"
                if v == "critical"
                else "[bold green]ok[/bold green]"
                if v == "ok"
                else f"[yellow]{v}[/yellow]"
            ),
            "eo_correlator": lambda _: "[bold magenta]true[/bold magenta]",
        }

        for label, key in display_fields:
            value = payload.get(key)
            if value is None or value == "":
                continue

            if isinstance(value, list):
                display_value = ", ".join(str(v) for v in value)
            else:
                display_value = str(value)

            formatter = style_map.get(key)
            if formatter:
                display_value = formatter(display_value)

            table.add_row(label, display_value)

        self.console.print()
        self.console.print(table)

        if event:
            self.console.print(
                f"\n[dim]Correlated to external event:[/dim] "
                f"[italic]{truncate(event.get('title', 'N/A'), 80)}[/italic]"
            )

    # ── BigPanda Communication ───────────────────────────────────────────

    def _bp_headers(self):
        """Standard BigPanda auth headers."""
        return {
            "Authorization": f"Bearer {self.bp_org_token}",
            "Content-Type": "application/json",
        }

    def send_to_bigpanda(self, payload):
        """Send an alert payload to the BigPanda OIM alerts endpoint.

        Auth: Authorization Bearer header + access_token query param.
        Routing: app_key as query parameter.
        Body: alert payload JSON.
        """
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True,
        ) as progress:
            status_label = payload.get("status", "unknown")
            task = progress.add_task(
                f"Sending [bold]{status_label}[/bold] alert to BigPanda...",
                total=None,
            )

            try:
                response = requests.post(
                    self.bp_alerts_url,
                    headers=self._bp_headers(),
                    params={
                        "access_token": self.bp_org_token,
                        "app_key": self.bp_app_key,
                    },
                    json=payload,
                    timeout=15,
                )

                if response.status_code in (200, 201, 202):
                    progress.update(
                        task,
                        description="[bold green]Alert sent successfully![/bold green]",
                    )
                    return True
                else:
                    progress.update(
                        task,
                        description=f"[red]BigPanda returned HTTP {response.status_code}[/red]",
                    )
                    self.console.print(f"[red]Response body:[/red] {response.text[:500]}")
                    return False

            except requests.RequestException as e:
                progress.update(task, description="[red]Failed to send alert[/red]")
                self.console.print(f"\n[red]Network error:[/red] {e}")
                return False

    # ── Alert Tracking ───────────────────────────────────────────────────

    def track_sent_alert(self, payload, event):
        """Record a sent alert locally for later resolution."""
        record = {
            "host": payload.get("host"),
            "check": payload.get("check"),
            "description": payload.get("description"),
            "service": payload.get("service"),
            "application": payload.get("application"),
            "cluster": payload.get("cluster"),
            "instance": payload.get("instance"),
            "location": payload.get("location"),
            "environment": payload.get("environment"),
            "cloud_region": payload.get("cloud_region"),
            "cloud_provider": payload.get("cloud_provider"),
            "cloud_account_id": payload.get("cloud_account_id"),
            "assignment_group": payload.get("assignment_group"),
            "escalation_group": payload.get("escalation_group"),
            "business_criticality": payload.get("business_criticality"),
            "known_dependencies": payload.get("known_dependencies"),
            "business_owner": payload.get("business_owner"),
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "based_on_event": event.get("title", "N/A") if event else "N/A",
            "status": "critical",
        }
        self.sent_alerts.append(record)
        self._save_sent_alerts()

    # ── Resolve Flow ─────────────────────────────────────────────────────

    def show_sent_alerts(self):
        """Display all tracked sent alerts in a table. Returns {number: alert} map of active ones."""
        active = [a for a in self.sent_alerts if a.get("status") != "ok"]
        resolved = [a for a in self.sent_alerts if a.get("status") == "ok"]

        if not self.sent_alerts:
            self.console.print("\n[yellow]No sent alerts on record.[/yellow]")
            return {}

        if active:
            table = Table(
                title=f"Active Alerts ({len(active)})",
                box=box.ROUNDED,
                header_style="bold yellow",
                title_style="bold",
            )
            table.add_column("#", style="dim", width=4, justify="right")
            table.add_column("Host", max_width=38)
            table.add_column("Check", max_width=22)
            table.add_column("Location", max_width=20)
            table.add_column("Sent At", width=18)
            table.add_column("Based On", max_width=32, style="dim")

            alert_map = {}
            for i, alert in enumerate(active, 1):
                sent_at = alert.get("sent_at", "N/A")
                if sent_at != "N/A":
                    try:
                        dt = datetime.fromisoformat(sent_at)
                        sent_at = dt.strftime("%Y-%m-%d %H:%M")
                    except ValueError:
                        pass

                table.add_row(
                    str(i),
                    truncate(alert.get("host", "N/A"), 36),
                    truncate(alert.get("check", "N/A"), 20),
                    truncate(alert.get("location", alert.get("cluster", "N/A")), 18),
                    sent_at,
                    truncate(alert.get("based_on_event", "N/A"), 30),
                )
                alert_map[i] = alert

            self.console.print()
            self.console.print(table)
        else:
            self.console.print("\n[green]No active (unresolved) alerts.[/green]")
            alert_map = {}

        if resolved:
            self.console.print(
                f"\n[dim]{len(resolved)} previously resolved alert(s) on file.[/dim]"
            )

        return alert_map

    def resolve_alerts(self, auto_all=False):
        """Send OK status for previously sent alerts. If auto_all=True, resolve without prompting."""
        alert_map = self.show_sent_alerts()
        if not alert_map:
            return

        if auto_all:
            to_resolve = list(alert_map.values())
        else:
            self.console.print()
            selection = Prompt.ask(
                "Select alerts to resolve [dim](comma-separated numbers, 'all', or 'q' to go back)[/dim]",
                default="all",
            )

            if selection.strip().lower() in ("q", "quit", "exit", "back"):
                self.console.print("[dim]Cancelled.[/dim]")
                return

            if selection.strip().lower() == "all":
                to_resolve = list(alert_map.values())
            else:
                try:
                    indices = [int(x.strip()) for x in selection.split(",")]
                    to_resolve = [alert_map[i] for i in indices if i in alert_map]
                except ValueError:
                    self.console.print("[yellow]Invalid input.[/yellow]")
                    return

        if not to_resolve:
            self.console.print("[yellow]No alerts selected.[/yellow]")
            return

        if not auto_all:
            if not Confirm.ask(
                f"\nSend [bold green]OK[/bold green] status for {len(to_resolve)} alert(s)?"
            ):
                self.console.print("[dim]Cancelled.[/dim]")
                return

        self.console.print()
        success_count = 0
        for alert in to_resolve:
            # Build OK payload — must include the same primary + secondary property
            # values so BigPanda can match and resolve the original alert.
            ok_payload = {
                "status": "ok",
                "host": alert.get("host"),
                "check": alert.get("check"),
                "description": f"Resolved: {alert.get('description', 'Alert cleared')}",
                "service": alert.get("service", ""),
                "application": alert.get("application", ""),
                "cluster": alert.get("cluster", ""),
                "instance": alert.get("instance", ""),
                "location": alert.get("location", ""),
                "environment": alert.get("environment", ""),
                "eo_correlator": "true",
                "timestamp": int(time.time()),
            }

            success = self.send_to_bigpanda(ok_payload)
            host_short = truncate(alert.get("host", "?"), 40)
            if success:
                alert["status"] = "ok"
                alert["resolved_at"] = datetime.now(timezone.utc).isoformat()
                self.console.print(f"  [green]✓[/green] Resolved: {host_short}")
                success_count += 1
            else:
                self.console.print(f"  [red]✗[/red] Failed:   {host_short}")

        self._save_sent_alerts()
        self.console.print(
            f"\n[bold]{success_count}/{len(to_resolve)} alerts resolved.[/bold]"
        )

    # ── OIM Integration Setup ────────────────────────────────────────────

    def setup_oim_integration(self):
        """Configure the BigPanda OIM integration with the expected payload format.

        POST https://integrations.bigpanda.io/configurations/alerts/oim/{app_key}
        """
        config_url = f"{OIM_CONFIG_BASE_URL}/{self.bp_app_key}"

        self.console.print()
        self.console.print(
            Panel(
                "[bold yellow]CAUTION:[/bold yellow] Will overwrite the entire configuration of the\n"
                "OIM Integration App Key provided to make sure the demo tool works correctly.",
                border_style="yellow",
            )
        )

        if not Confirm.ask("\nAre you sure you wish to continue?", default=False):
            self.console.print("[dim]Cancelled.[/dim]")
            return

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True,
        ) as progress:
            task = progress.add_task(
                "Configuring OIM integration...", total=None
            )

            try:
                response = requests.post(
                    config_url,
                    headers=self._bp_headers(),
                    json=OIM_CONFIG_PAYLOAD,
                    timeout=20,
                )

                if response.status_code in (200, 201, 202):
                    progress.update(
                        task,
                        description="[bold green]OIM integration configured successfully![/bold green]",
                    )
                    self.console.print(
                        "\n[green]✓ Integration is ready to receive demo alerts.[/green]"
                    )
                else:
                    progress.update(
                        task,
                        description=f"[red]BigPanda returned HTTP {response.status_code}[/red]",
                    )
                    self.console.print(f"[red]Response:[/red] {response.text[:500]}")
                    if response.status_code == 401:
                        self.console.print(
                            "\n[yellow]Hint:[/yellow] Verify your BIGPANDA_ORG_ACCESS_TOKEN "
                            "is an Org Token (not a User API Key)."
                        )
                    elif response.status_code == 404:
                        self.console.print(
                            "\n[yellow]Hint:[/yellow] Verify your BIGPANDA_APP_KEY "
                            "matches an existing OIM integration."
                        )

            except requests.RequestException as e:
                progress.update(task, description="[red]Failed to configure[/red]")
                self.console.print(f"\n[red]Network error:[/red] {e}")

    # ── Generate & Send Flow ─────────────────────────────────────────────

    def generate_and_send_flow(self):
        """Full interactive flow: fetch → filter → select → generate → preview → send."""
        # Fetch live events
        events = self.fetch_events()
        if not events:
            self.console.print("[yellow]No active events found. Try again later.[/yellow]")
            return

        # Show type summary and let user filter
        type_map = self.display_type_summary(events)
        selected_types = self.select_alert_types(type_map)
        self.console.print(f"\n[dim]Filtering by: {', '.join(selected_types)}[/dim]")

        # Show filtered events
        filtered, event_map = self.display_events(events, selected_types)
        if not event_map:
            return

        # Select one event
        event = self.select_event(event_map)
        if not event:
            self.console.print("[yellow]No event selected.[/yellow]")
            return

        # Show event detail
        self.show_event_detail(event)

        # Generate the internal alert via AI
        generated = self.generate_internal_alert(event)
        if not generated:
            return

        # Build and preview the BigPanda payload
        payload = self.build_bigpanda_payload(generated)
        self.preview_payload(payload, event)

        # Confirm and send
        self.console.print()
        if not Confirm.ask("Send this alert to BigPanda?", default=True):
            if Confirm.ask("Would you like to regenerate the alert?", default=False):
                generated = self.generate_internal_alert(event)
                if generated:
                    payload = self.build_bigpanda_payload(generated)
                    self.preview_payload(payload, event)
                    self.console.print()
                    if not Confirm.ask("Send this alert to BigPanda?", default=True):
                        self.console.print("[dim]Cancelled.[/dim]")
                        return
                else:
                    return
            else:
                self.console.print("[dim]Cancelled.[/dim]")
                return

        success = self.send_to_bigpanda(payload)
        if success:
            self.track_sent_alert(payload, event)
            self.console.print("\n[bold green]✓ Alert sent and tracked![/bold green]")
            self.console.print(
                "[dim]Use option 2 from the main menu to resolve it when done.[/dim]"
            )

    # ── Main Entry Point ─────────────────────────────────────────────────

    def run(self, resolve_all=False, setup_oim=False):
        """Main entry point. Shows banner, validates config, enters menu loop."""
        self._show_banner()

        # Validate configuration
        missing = self._validate_config()
        if missing:
            self._show_config_help(missing)
            return

        self.console.print("[green]✓ Configuration loaded successfully[/green]")

        # Quick resolve mode (--resolve-all)
        if resolve_all:
            self.console.print("\n[bold]Quick Resolve Mode[/bold]")
            self.resolve_alerts(auto_all=True)
            return

        # Quick OIM setup mode (--setup-oim)
        if setup_oim:
            self.setup_oim_integration()
            return

        # Interactive menu loop
        while True:
            choice = self._show_main_menu()

            if choice == "1":
                self.generate_and_send_flow()
            elif choice == "2":
                self.resolve_alerts()
            elif choice == "3":
                self.show_sent_alerts()
            elif choice == "4":
                self.setup_oim_integration()
            elif choice == "5":
                self.console.print("\n[dim]Goodbye![/dim]\n")
                break


# ─── CLI Entry Point ─────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="EO Strata Demo Simulator — Generate correlated internal alerts from live events",
    )
    parser.add_argument(
        "--resolve-all",
        action="store_true",
        help="Quickly resolve all active sent alerts and exit",
    )
    parser.add_argument(
        "--setup-oim",
        action="store_true",
        help="Configure the BigPanda OIM integration and exit",
    )
    args = parser.parse_args()

    try:
        sim = DemoSimulator()
        sim.run(resolve_all=args.resolve_all, setup_oim=args.setup_oim)
    except KeyboardInterrupt:
        print("\n\nInterrupted. Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
