#!/bin/sh
set -eu
export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1280x720x24 -nolisten tcp &
display_pid=$!
attempt=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
    kill -0 "$display_pid"
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 50 ]; then
        echo "Virtual display failed to start" >&2
        exit 1
    fi
    sleep 0.1
done
# Python receives Docker's SIGTERM directly and closes the persistent profile.
if [ ! -f /tmp/watcher-instance-id ]; then
    cat /proc/sys/kernel/random/uuid > /tmp/watcher-instance-id
fi
export WATCHER_INSTANCE_ID="$(cat /tmp/watcher-instance-id)"
exec "$@"
