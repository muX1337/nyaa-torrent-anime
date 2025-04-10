#!/bin/bash

# Define container IDs or names
GLUETUN_CONTAINER="gluetun"
QBITTORRENT_CONTAINER="qbittorrent"
ANIME_WATCHLIST_CONTAINER="anime_watchlist"

# Function to check if a container is running
is_running() {
    local status=$(docker container inspect -f '{{.State.Running}}' $1 2>/dev/null)
    if [ "$status" == "true" ]; then
        return 0
    else
        return 1
    fi
}

# Function to start a container if it's not running
start_container() {
    local container=$1
    if ! is_running $container; then
        echo "Starting $container..."
        docker container start $container
        # Wait for container to be up and running
        sleep 5
    else
        echo "$container is already running."
    fi
}

# Stop containers if running (in reverse order)
echo "Stopping containers if running..."
docker container stop $ANIME_WATCHLIST_CONTAINER $QBITTORRENT_CONTAINER $GLUETUN_CONTAINER 2>/dev/null

# Start containers in the correct order
echo "Starting containers in the correct order..."
start_container $GLUETUN_CONTAINER
start_container $QBITTORRENT_CONTAINER
start_container $ANIME_WATCHLIST_CONTAINER

echo "All containers should now be running."