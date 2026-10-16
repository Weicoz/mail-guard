#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$DIR:$PYTHONPATH"

chmod +x "$DIR/post_shell.sh" 2>/dev/null || true
chmod +x "$DIR/handle_mail.py" 2>/dev/null || true

python3 "$DIR/test_pipeline.py"
