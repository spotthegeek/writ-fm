#!/bin/bash
# WRIT-FM Operator - Launch Claude Code for maintenance
# Run manually, via cron, or from station/operator_daemon.sh.

set -euo pipefail

# Cron runs with a minimal PATH; ensure Homebrew-installed CLIs are available.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

cd "$(dirname "$0")"

# Read the operator prompt
PROMPT=$(cat station/operator_prompt.md)

# Launch Gemini CLI with the prompt
# Note: gemini -p expects the prompt as an argument.
# We use --approval-mode plan to ensure it only reads/writes as expected.
gemini --approval-mode plan -p "$PROMPT"
