#!/bin/bash
# Launcher for the live subtitle pipeline (ffmpeg -> whisper.cpp -> Ollama).
#
#   ./run.sh start [options]   start whisper-server if needed, load the Ollama model, run live subtitles
#   ./run.sh test  [options]   synthesize English speech with `say` and run the whole pipeline on it
#   ./run.sh devices           list audio input devices (look for "BlackHole 2ch")
#   ./run.sh warm              load the Ollama model into memory ahead of time
#   ./run.sh stop              stop whisper-server
#   ./run.sh route "EarPods"   rebuild the Multi-Output Device around another speaker/headphone
#
# [options] are passed to live_subs.py, e.g. --device "BlackHole" --threshold-db -45 --vu
#
# Settings: copy config.env.example to config.env and edit it, or export the variables:
#   OLLAMA_MODEL     Ollama model used for translation        (default qwen3:8b)
#   OLLAMA_URL       Ollama endpoint                           (default http://127.0.0.1:11434)
#   WHISPER_MODEL    path to a ggml whisper model              (default: newest large-v3-turbo in models/)
#   WHISPER_PORT     whisper-server port                       (default 8178)
#   WHISPER_THREADS  CPU threads for whisper-server            (default 4)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/config.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$HERE/config.env"
  set +a
fi
WHISPER_PORT="${WHISPER_PORT:-8178}"
WHISPER_THREADS="${WHISPER_THREADS:-4}"
WHISPER_MODEL="${WHISPER_MODEL:-}"
export WHISPER_URL="http://127.0.0.1:${WHISPER_PORT}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:8b}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
mkdir -p "$HERE/logs" "$HERE/transcripts"

require() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing dependency: $1  ($2)" >&2
    exit 1
  fi
}

pick_model() {
  if [ -n "$WHISPER_MODEL" ]; then
    echo "$WHISPER_MODEL"
    return
  fi
  local f
  for f in "$HERE/models/ggml-large-v3-turbo.bin" "$HERE/models/ggml-large-v3-turbo-q5_0.bin" "$HERE"/models/ggml-*.bin; do
    if [ -s "$f" ]; then
      echo "$f"
      return
    fi
  done
  echo ""
}

whisper_up() { nc -z 127.0.0.1 "$WHISPER_PORT" >/dev/null 2>&1; }

start_whisper() {
  require whisper-server "brew install whisper-cpp"
  if whisper_up; then
    echo "whisper-server already running on port $WHISPER_PORT"
    return
  fi
  local model
  model="$(pick_model)"
  if [ -z "$model" ]; then
    echo "No whisper model found. Download one with:  ./models/download.sh" >&2
    exit 1
  fi
  echo "Starting whisper-server with $(basename "$model") ..."
  nohup whisper-server -m "$model" -l en -t "$WHISPER_THREADS" --host 127.0.0.1 --port "$WHISPER_PORT" \
    > "$HERE/logs/whisper-server.log" 2>&1 &
  echo $! > "$HERE/logs/whisper-server.pid"
  local i
  for i in $(seq 1 120); do
    whisper_up && break
    sleep 0.5
  done
  if ! whisper_up; then
    echo "whisper-server failed to start; see $HERE/logs/whisper-server.log" >&2
    tail -20 "$HERE/logs/whisper-server.log" >&2
    exit 1
  fi
  echo "whisper-server ready on port $WHISPER_PORT."
}

warm_ollama() {
  echo "Loading Ollama model $OLLAMA_MODEL (first load may take a while) ..."
  local with_think without_think
  with_think="{\"model\":\"$OLLAMA_MODEL\",\"stream\":false,\"keep_alive\":\"3h\",\"think\":false,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"options\":{\"num_predict\":1}}"
  without_think="{\"model\":\"$OLLAMA_MODEL\",\"stream\":false,\"keep_alive\":\"3h\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"options\":{\"num_predict\":1}}"
  if curl -sS -f -m 600 "$OLLAMA_URL/api/chat" -d "$with_think" >/dev/null 2>&1 \
     || curl -sS -f -m 600 "$OLLAMA_URL/api/chat" -d "$without_think" >/dev/null 2>&1; then
    echo "Ollama model loaded (kept in memory for 3 hours)."
  else
    echo "Could not load $OLLAMA_MODEL from Ollama at $OLLAMA_URL." >&2
    echo "Is Ollama running? Is the model pulled?  ollama pull $OLLAMA_MODEL" >&2
    exit 1
  fi
}

stop_whisper() {
  if [ -f "$HERE/logs/whisper-server.pid" ] && kill "$(cat "$HERE/logs/whisper-server.pid")" 2>/dev/null; then
    echo "whisper-server stopped."
  elif pkill -f "whisper-server .*--port $WHISPER_PORT" 2>/dev/null; then
    echo "whisper-server stopped."
  else
    echo "whisper-server was not running."
  fi
  rm -f "$HERE/logs/whisper-server.pid"
}

cmd="${1:-start}"
if [ $# -gt 0 ]; then shift; fi

case "$cmd" in
  start)
    require ffmpeg "brew install ffmpeg"
    start_whisper
    warm_ollama
    exec python3 "$HERE/live_subs.py" "$@"
    ;;
  test)
    require ffmpeg "brew install ffmpeg"
    start_whisper
    warm_ollama
    aiff="$HERE/logs/test-speech.aiff"
    say -o "$aiff" \
      "Okay, let's get started. [[slnc 900]] First item is the driver regression we saw in the nightly build. [[slnc 900]] Can someone confirm whether the fix landed before the release branch was cut? [[slnc 1200]] If not, we need to track it in the bug and add it to the Q1 roadmap review next week. [[slnc 900]] Any questions before we move on?"
    exec python3 "$HERE/live_subs.py" --file "$aiff" "$@"
    ;;
  devices)
    require ffmpeg "brew install ffmpeg"
    python3 "$HERE/live_subs.py" --list-devices
    ;;
  warm)
    warm_ollama
    ;;
  stop)
    stop_whisper
    ;;
  route)
    # Rebuild the Multi-Output Device around another speaker/headphone, e.g. ./run.sh route "EarPods"
    name="${1:?usage: $0 route \"<speaker or headphone name>\"}"
    require swift "install Xcode or the Command Line Tools"
    swift "$HERE/tools/create-multi-output.swift" --remove >/dev/null 2>&1 || true
    exec swift "$HERE/tools/create-multi-output.swift" --primary "$name"
    ;;
  *)
    echo "usage: $0 {start|test|devices|warm|stop|route} [live_subs.py options]" >&2
    exit 2
    ;;
esac
