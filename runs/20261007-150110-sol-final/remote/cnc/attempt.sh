#!/usr/bin/env bash
# Snapshot the current program as the next attempt (attempts/01, 02, ...).
set -eu
cd "$HOME/cnc"
mkdir -p attempts
last=$(ls -d attempts/[0-9][0-9] 2>/dev/null | tail -1 | sed 's#attempts/##')
next=$(printf '%02d' $(( 10#${last:-0} + 1 )))
mkdir -p "attempts/$next"
cp job/1001.nc "attempts/$next/1001.nc"
[ -f gen.py ] && cp gen.py "attempts/$next/gen.py"
date -u +%FT%TZ > "attempts/$next/time.txt"
echo "saved attempt $next"
