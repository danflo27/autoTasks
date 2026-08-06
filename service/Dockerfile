FROM python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/tellor-alert-gate

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --requirement requirements.txt

COPY tellor_alert_gate ./tellor_alert_gate

RUN groupadd --gid 10001 alertgate \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin alertgate

USER 10001:10001

ENTRYPOINT ["python", "-m", "tellor_alert_gate"]
CMD ["run"]
