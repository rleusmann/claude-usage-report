#!/bin/bash
input=$(cat)

LIMITS_LOGGER="$HOME/.claude/plugins/cache/claude-usage-report/usage-report/bin/usage-report-log-limits"
[ -x "$LIMITS_LOGGER" ] || LIMITS_LOGGER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/bin/usage-report-log-limits"
[ -x "$LIMITS_LOGGER" ] && { printf '%s' "$input" | "$LIMITS_LOGGER" >/dev/null 2>&1 & }

at() { date -r "$1" "+$2" 2>/dev/null || date -d "@$1" "+$2" 2>/dev/null; }

eval "$(jq -r '
  def cents: (. // 0) * 100 | round | tostring
    | if length < 3 then ("00" + .)[-3:] else . end
    | .[:-2] + "." + .[-2:];
  @sh "MODEL=\(.model.display_name // "?")",
  @sh "EFFORT=\(.effort.level // "")",
  @sh "DIR=\(.workspace.current_dir // .cwd // "")",
  @sh "CTX=\(.context_window.used_percentage // 0 | floor)",
  @sh "CTX_TOK=\(.context_window.total_input_tokens // 0)",
  @sh "FIVE=\(.rate_limits.five_hour.used_percentage // "" | if . == "" then . else floor end)",
  @sh "FIVE_RESET=\(.rate_limits.five_hour.resets_at // "")",
  @sh "WEEK=\(.rate_limits.seven_day.used_percentage // "" | if . == "" then . else floor end)",
  @sh "WEEK_RESET=\(.rate_limits.seven_day.resets_at // "")",
  @sh "CACHE_WARM=\(.prompt_cache.warm // "")",
  @sh "CACHE_SEEN=\(.prompt_cache.caching_observed // "")",
  @sh "CACHE_EXP=\(.prompt_cache.expires_at // "")",
  @sh "CACHE_HIT=\(.prompt_cache.hit_ratio // "" | if . == "" then . else (. * 100 | round) end)",
  @sh "PR=\(.pr.number // "")",
  @sh "PR_STATE=\(.pr.review_state // "")",
  @sh "ADDED=\(.cost.total_lines_added // 0)",
  @sh "REMOVED=\(.cost.total_lines_removed // 0)",
  @sh "DURATION_MS=\(.cost.total_duration_ms // 0)",
  @sh "COST=\(.cost.total_cost_usd | cents)"
' <<<"$input")"

LINE1="[$MODEL${EFFORT:+ · $EFFORT}]"

if [ -n "$DIR" ] && git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  REPO=$(basename "$(git -C "$DIR" rev-parse --show-toplevel)")
  BRANCH=$(git -C "$DIR" branch --show-current 2>/dev/null)
  [ -z "$BRANCH" ] && BRANCH=$(git -C "$DIR" rev-parse --short HEAD)
  GIT="$REPO $BRANCH"
  AHEAD=$(git -C "$DIR" rev-list --count '@{u}..HEAD' 2>/dev/null)
  [ -n "$AHEAD" ] && [ "$AHEAD" -gt 0 ] && GIT="$GIT ↑$AHEAD"
  [ -n "$(git -C "$DIR" status --porcelain 2>/dev/null | head -1)" ] && GIT="$GIT ●"
  LINE1="$LINE1 $GIT"
elif [ -n "$DIR" ]; then
  LINE1="$LINE1 $(basename "$DIR")"
fi

[ -n "$PR" ] && LINE1="$LINE1 PR #$PR${PR_STATE:+ ($PR_STATE)}"

[ "$ADDED" -gt 0 ] || [ "$REMOVED" -gt 0 ] && LINE1="$LINE1 | +$ADDED/-$REMOVED"

MINS=$((DURATION_MS / 60000))
if [ "$MINS" -ge 60 ]; then
  LINE1="$LINE1 | $((MINS / 60))h $(printf '%02d' $((MINS % 60)))m"
else
  LINE1="$LINE1 | ${MINS}m"
fi

LINE2="Context ${CTX}% ($((CTX_TOK / 1000))k)"
[ -n "$FIVE" ] && LINE2="$LINE2 | 5h ${FIVE}%${FIVE_RESET:+ ($(at "$FIVE_RESET" %H:%M))}"
[ -n "$WEEK" ] && LINE2="$LINE2 | week ${WEEK}%${WEEK_RESET:+ ($(at "$WEEK_RESET" '%a %H:%M'))}"
if [ "$CACHE_WARM" = "true" ] && [ -n "$CACHE_EXP" ]; then
  LINE2="$LINE2 | cache warm until $(at "$CACHE_EXP" %H:%M)${CACHE_HIT:+, ${CACHE_HIT}% hits}"
elif [ "$CACHE_SEEN" = "true" ]; then
  LINE2="$LINE2 | cache cold${CACHE_HIT:+, ${CACHE_HIT}% hits}"
fi
LINE2="$LINE2 | API value \$$COST"

echo "$LINE1"
echo "$LINE2"
