# Image for the VM metrics adapter. The binary is static (CGO_ENABLED=0), so it
# needs no libc or CA bundle: it only talks to the in-cluster API server with
# the ServiceAccount CA.
#
#   CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags "-s -w" \
#     -o vm-metrics-adapter ./cmd/vm-metrics-adapter
#   docker build -f cmd/vm-metrics-adapter/Dockerfile -t <ref> .
FROM scratch
COPY vm-metrics-adapter /vm-metrics-adapter
USER 65532:65532
ENTRYPOINT ["/vm-metrics-adapter"]
