FROM openzeppelin/openzeppelin-monitor:v1.5.0@sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635

ARG SOURCE_GIT_SHA
LABEL org.opencontainers.image.revision="${SOURCE_GIT_SHA}"

# The production platform overrides this to log-only for notification-disabled
# replays. Long-running Monitor instances use the image default.
ENV TELLOR_DELIVERY_MODE=live

# Keep this literal and numeric: the platform verifies Config.User before start.
USER 65532:65532
