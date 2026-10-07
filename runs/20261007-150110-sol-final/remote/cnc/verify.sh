#!/usr/bin/env bash
# Shop verifier: G-code lint + stock-removal simulation of job/1001.nc against the part model.
set -u
cd "$HOME/cnc"
n=$(ls -d attempts/[0-9][0-9] 2>/dev/null | tail -1)
out="${n:-check}/check_out"
mkdir -p "$out"
PYTHONPATH="$HOME/cnc/vendor" "$HOME/cnc/.venv/bin/python" -m checks run job --png --out "$out" > "$out.log" 2>&1
code=$?
cat "$out/report.md" 2>/dev/null || tail -40 "$out.log"
if [ $code -eq 0 ]; then echo "STATUS: PASS"; else echo "STATUS: FAIL"; fi
exit $code
