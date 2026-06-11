#!/usr/bin/env bash

# Authors: Hy3-preview🧙‍♂️, scillidan🤡

set -euo pipefail

input_file="${1:-database.json}"

full_csv="compactgui-games-full-1.csv"
best_csv="compactgui-games-best.csv"
extend_csv="compactgui-games-full-2.csv"

if [[ ! -f "$input_file" ]]; then
  echo "ERROR: Input file '$input_file' not found"
  echo "--- Current directory contents ---"
  ls -la
  exit 1
fi

echo "Processing: $input_file"

# Option 1: Full Report (all compression results per game)
if [[ "${2:-}" == "--full" ]] || [[ -z "${2:-}" ]]; then
  echo "Generating full report..."

  # Table 1: All compression results
  jq -r '
  ["SteamID", "GameName", "CompType", "BeforeBytes", "AfterBytes", "SavingsPercent"],
  (
    .[] |
    . as $game |
    .CompressionResults[]? |
    [
      $game.SteamID,
      $game.GameName,
      .CompType,
      .BeforeBytes,
      .AfterBytes,
      (100 - (.AfterBytes / .BeforeBytes * 100 | floor))
    ]
  )
  | @csv
  ' "$input_file" > "$full_csv"

  echo "Generated: $full_csv"

  # Table 2: Poorly compressed file types
  jq -r '
  ["SteamID", "GameName", "Extension", "Count"],
  (
    .[] |
    . as $game |
    .PoorlyCompressedExtensions // {} |
    to_entries[] |
    [
      $game.SteamID,
      $game.GameName,
      .key,
      .value
    ]
  )
  | @csv
  ' "$input_file" > "$extend_csv"

  echo "Generated: $extend_csv"
fi

# Option 2: Best Report (best compression result per game)
if [[ "${2:-}" == "--best" ]] || [[ -z "${2:-}" ]]; then
  echo "Generating best compression report..."

  jq -r '
  ["SteamID", "GameName", "CompType", "BeforeBytes", "AfterBytes", "SavingsPercent"],
  (
    .[] |
    . as $game |
    # Find the compression result with smallest AfterBytes
    (.CompressionResults | sort_by(.AfterBytes) | .[0]) as $best |
    [
      $game.SteamID,
      $game.GameName,
      $best.CompType,
      $best.BeforeBytes,
      $best.AfterBytes,
      (100 - ($best.AfterBytes / $best.BeforeBytes * 100 | floor))
    ]
  )
  | @csv
  ' "$input_file" > "$best_csv"

  echo "Generated: $best_csv"
fi

echo "Done processing $input_file"