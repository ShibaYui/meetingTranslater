#!/bin/bash
# Summarize a saved meeting transcript with Claude Code (non-interactive mode).
#   ./summarize.sh transcripts/20261125-0100.txt
# Writes transcripts/20261125-0100-summary.md next to the transcript.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
src="${1:?usage: $0 transcripts/<transcript>.txt}"
out="${src%.*}-summary.md"

claude -p "$(cat "$HERE/prompts/summarize.txt")" < "$src" > "$out"
echo "Summary written to $out"
