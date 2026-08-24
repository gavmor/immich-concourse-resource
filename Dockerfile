FROM python:3.12-alpine

RUN pip install --no-cache-dir requests==2.34.2

RUN mkdir -p /opt/resource
WORKDIR /opt/resource

COPY check /opt/resource/check
COPY in /opt/resource/in
COPY out /opt/resource/out

RUN chmod +x /opt/resource/check /opt/resource/in /opt/resource/out
