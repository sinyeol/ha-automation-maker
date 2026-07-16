#!/usr/bin/with-contenv bashio
bashio::log.info "Starting HA Automation Maker..."
cd /app || bashio::exit.nok "app dir missing"
if bashio::config.has_value 'anthropic_api_key'; then
    export ANTHROPIC_API_KEY="$(bashio::config 'anthropic_api_key')"
fi
exec python3 -m backend.app
