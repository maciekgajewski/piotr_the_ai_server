#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p wakeword/ryszardzie/negative_datasets

base_url="https://huggingface.co/datasets/kahrendt/microwakeword/resolve/main"
for name in dinner_party.zip dinner_party_eval.zip no_speech.zip speech.zip; do
  zip_path="wakeword/ryszardzie/negative_datasets/$name"
  if [[ -f "$zip_path" ]] && ! python3 -m zipfile --test "$zip_path" >/dev/null 2>&1; then
    rm "$zip_path"
  fi

  if [[ ! -f "$zip_path" ]]; then
    wget -q --show-progress -O "$zip_path" "$base_url/$name"
  fi
  python3 - "$zip_path" wakeword/ryszardzie/negative_datasets <<'PY'
import sys
from pathlib import Path
from zipfile import ZipFile

zip_path = Path(sys.argv[1])
output_dir = Path(sys.argv[2])

with ZipFile(zip_path) as archive:
    for member in archive.infolist():
        target = output_dir / member.filename
        if target.exists():
            continue
        archive.extract(member, output_dir)
PY
done
