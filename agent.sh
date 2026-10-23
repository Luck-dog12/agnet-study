#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_exe="$project_dir/.venv/bin/python"

if [ ! -x "$python_exe" ]; then
    echo "Startup failed: project virtual environment not found." >&2
    exit 2
fi

cd -- "$project_dir"
exec "$python_exe" -m app.main "$@"
