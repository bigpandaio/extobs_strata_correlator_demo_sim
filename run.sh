#!/bin/bash
# ──────────────────────────────────────────────────────────────────
# EO Strata Demo Simulator - Launcher
# ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Run setup first:"
    echo "  ./setup.sh"
    exit 1
fi

source venv/bin/activate
python demo_sim.py "$@"
