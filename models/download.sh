#!/bin/bash
# Download a whisper.cpp (ggml) model into this directory.
#
#   ./models/download.sh              # large-v3-turbo, f16, 1.6 GB  (default, best accuracy)
#   ./models/download.sh q5_0         # large-v3-turbo-q5_0, 574 MB (smaller, nearly as accurate)
#   ./models/download.sh small.en     # any other model name from huggingface.co/ggerganov/whisper.cpp
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
name="${1:-large-v3-turbo}"
case "$name" in
  q5_0|turbo-q5_0) name="large-v3-turbo-q5_0" ;;
  turbo) name="large-v3-turbo" ;;
esac
file="ggml-${name}.bin"
url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${file}"

if [ -s "$HERE/$file" ]; then
  echo "$file already exists in $HERE"
  exit 0
fi
echo "Downloading $file ..."
curl -L --fail --retry 3 --progress-bar -o "$HERE/$file.part" "$url"
mv "$HERE/$file.part" "$HERE/$file"
echo "Saved $HERE/$file"
