#!/bin/sh
# `schedule` (default): optional first run, then supercronic runs the crontab.
# Anything else is passed to the CLI, e.g. `docker compose run --rm app search --once`.
set -e

if [ "$1" = "schedule" ]; then
    if [ "${RUN_ON_START:-true}" = "true" ]; then
        # in the background so the schedule starts right away; a file lock prevents overlap
        daytrip search --once &
    fi
    exec supercronic -passthrough-logs /app/crontab
fi

exec daytrip "$@"
