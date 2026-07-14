FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24
WORKDIR /app
COPY backend/requirements.txt ./backend/
RUN pip3 install --no-cache-dir -r backend/requirements.txt
COPY backend ./backend
COPY frontend ./frontend
COPY run.sh /
RUN chmod a+x /run.sh
CMD ["/run.sh"]
LABEL io.hass.type="app" io.hass.version="1.0.0" io.hass.arch="aarch64|amd64"
