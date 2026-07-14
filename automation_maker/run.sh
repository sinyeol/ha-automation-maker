#!/usr/bin/with-contenv bashio
bashio::log.info "Starting HA Automation Maker..."
cd /app || bashio::exit.nok "app dir missing"
exec python3 -m backend.app
