FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24
WORKDIR /app
COPY backend/requirements.txt ./backend/
RUN pip3 install --no-cache-dir -r backend/requirements.txt
# SPEC-V3 §4.4: 구독 CLI 백엔드용 claude-code(네이티브 바이너리, Node 불필요)를 Alpine
# 공식 저장소에서 설치. 네트워크 사정으로 실패해도 이미지 빌드가 깨지지 않게 실패 허용(|| true);
# 바이너리 부재 시 cli_client 는 조용히 None(백엔드 off 와 동일 동작).
RUN (wget -O /etc/apk/keys/claude-code.rsa.pub https://downloads.claude.ai/keys/claude-code.rsa.pub \
     && echo "https://downloads.claude.ai/claude-code/apk/stable" >> /etc/apk/repositories \
     && apk add --no-cache claude-code) || true
COPY backend ./backend
COPY frontend ./frontend
COPY run.sh /
RUN chmod a+x /run.sh
CMD ["/run.sh"]
LABEL io.hass.type="app" io.hass.version="3.1.0" io.hass.arch="aarch64|amd64"
