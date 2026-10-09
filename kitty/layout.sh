#!/bin/sh
# One kitty tab: prompt + final summary on the left, pending next steps in a parallel vsplit.
D="$(cd "$(dirname "$0")" && pwd)/dashboard.py"
kitten @ launch --type=tab --tab-title "turn" python3 "$D" --view turn -i 1 >/dev/null || exit 1
kitten @ launch --location=vsplit --keep-focus python3 "$D" --view next -i 1
