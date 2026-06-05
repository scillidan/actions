#!/usr/bin/env bash

# Authors: GLM-5🧙‍♂️, Hy3-preview🧙‍♂️, scillidan🤡

set -euo pipefail

output_file="${1:-spdx-licenses.csv}"

dir=$(ls -d license-list-data-*/ 2>/dev/null | head -n1)
dir="${dir%/}"

echo "Detected source dir: $dir"

input="${dir}/json/licenses.json"

if [[ ! -f "$input" ]]; then
  echo "ERROR: licenses.json not found at $input"
  echo "--- ls . ---"
  ls -la
  echo "--- ls $dir (if exists) ---"
  ls -la "$dir" 2>/dev/null || true
  exit 1
fi

jq -r '
["name","licenseId","isOsiApproved","isFsfLibre","reference"],
(
  .licenses[]
  | [
      (.name // ""),
      (.licenseId // ""),
      (if .isOsiApproved == true then "Y" else "" end),
      (if .isFsfLibre == true then "Y" else "" end),
      (.reference // "")
    ]
)
| @csv
' "$input" > "$output_file"

echo "Done: $output_file"