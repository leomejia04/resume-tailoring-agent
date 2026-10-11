#!/bin/sh
# Reads a DOCX on stdin and writes a PDF on stdout. Diagnostics go to stderr.
# Exit codes: 65 bad input, 70 conversion failed.
set -eu

max_bytes=20971520
limit="${CONVERT_TIMEOUT_SECONDS:-60}"
work="$(mktemp -d /tmp/convert.XXXXXX)"
trap 'rm -rf "$work"' EXIT

head -c $((max_bytes + 1)) > "$work/input.docx"
size=$(wc -c < "$work/input.docx")
if [ "$size" -eq 0 ] || [ "$size" -gt "$max_bytes" ]; then
    echo "input is empty or larger than $max_bytes bytes" >&2
    exit 65
fi

if ! timeout --kill-after=5 "$limit" soffice \
    "-env:UserInstallation=file://$work/profile" \
    --headless --norestore --nolockcheck --nodefault \
    --convert-to pdf --outdir "$work" "$work/input.docx" >&2; then
    echo "conversion failed or timed out after ${limit}s" >&2
    exit 70
fi

if [ ! -s "$work/input.pdf" ]; then
    echo "no PDF was produced" >&2
    exit 70
fi
cat "$work/input.pdf"
