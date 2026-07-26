# Source this once per Terminal.app session before running any sandbox script/server
# (see TESTING.md) — sets every TIMEBLOCK_*_PATH var so nothing accidentally touches
# real calendars/reminders/state.
#
# Usage: source scripts/sandbox_env.sh

export TIMEBLOCK_RULES_PATH=rules.test.yaml
export TIMEBLOCK_STATE_PATH=$HOME/.timeblock-agent/state.test.json
export TIMEBLOCK_LOG_DIR=$HOME/timeblock-test-logs
export TIMEBLOCK_GOALS_DIR=$HOME/timeblock-test-goals
export TIMEBLOCK_BUCKETS_PATH=buckets.test.md
export TIMEBLOCK_TASK_PROGRESS_PATH=$HOME/.timeblock-agent/task_progress.test.json
export TIMEBLOCK_BLOCK_SNAPSHOTS_PATH=$HOME/.timeblock-agent/block_snapshots.test.json
export TIMEBLOCK_PREFERENCES_PATH=$HOME/timeblock-test-preferences.md

echo "Sandbox env vars set. TIMEBLOCK_RULES_PATH=$TIMEBLOCK_RULES_PATH"
