---
name: usage-report
description: Refresh and open the local Claude Code usage report (tokens, API-equivalent cost, limits)
---

Refresh the usage report and open it, then answer whatever the user asked about their usage.

1. Run `"${CLAUDE_PLUGIN_ROOT}/bin/usage-report-update" --open`. It ingests new transcript data, re-renders the report and opens it in the browser.
2. Tell the user the report is open and where the file lives (`--print-paths`).
3. If arguments were given ($ARGUMENTS), treat them as a question about the usage data: follow the `usage-report` skill, query the SQLite database read-only and answer in chat instead of only pointing at the report.

Keep the answer short. Do not dump report contents unless asked.
