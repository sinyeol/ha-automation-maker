#!/usr/bin/with-contenv bashio
bashio::log.info "Starting HA Automation Maker..."
cd /app || bashio::exit.nok "app dir missing"
# SPEC-V3 §4.3·§4.4: claude CLI 오토업데이트 차단(버전 누적 방지)은 상시.
export DISABLE_AUTOUPDATER=1
# SPEC-V3 §4.1: 애드온 옵션을 환경으로 노출(값이 있을 때만). 키/토큰은 로그에 남기지 않는다.
if bashio::config.has_value 'anthropic_api_key'; then
    export ANTHROPIC_API_KEY="$(bashio::config 'anthropic_api_key')"
fi
if bashio::config.has_value 'claude_code_oauth_token'; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(bashio::config 'claude_code_oauth_token')"
fi
if bashio::config.has_value 'llm_backend'; then
    export LLM_BACKEND="$(bashio::config 'llm_backend')"
fi
exec python3 -m backend.app
