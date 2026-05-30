#!/bin/bash
# Infinite loop runner for Daphne ASGI server to guarantee 100% uptime
echo "Starting infinite loop Daphne process manager..."
while true; do
    echo "Launching Daphne ASGI Server..."
    /root/venv/bin/python -m daphne -b 0.0.0.0 -p 8000 agent_command_center.asgi:application
    echo "Daphne exited. Respawning in 1 second..."
    sleep 1
done
