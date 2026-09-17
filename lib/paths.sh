# Shared path resolution for the bin/ scripts. Sourced, not executed.

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

resolve_data_dir() {
  if [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
    printf '%s\n' "$CLAUDE_PLUGIN_DATA"
    return
  fi
  local candidate
  for candidate in "$HOME"/.claude/plugins/data/usage-report-*; do
    if [ -d "$candidate" ]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  printf '%s\n' "$HOME/.claude/plugins/data/usage-report-claude-usage-report"
}

DATA_DIR="$(resolve_data_dir)"
DB_PATH="$DATA_DIR/usage.db"
REPORT_PATH="$DATA_DIR/report.html"
LIMITS_PATH="$DATA_DIR/limits.jsonl"
LOG_PATH="$DATA_DIR/update.log"
