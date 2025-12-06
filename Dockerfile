# syntax=docker/dockerfile:1

# Build stage
FROM golang:1.23-alpine AS builder

RUN apk add --no-cache git ca-certificates

WORKDIR /build

# Copy EVERYTHING at once - go.mod AND source code
COPY src/ ./

# Let Go figure out ALL dependencies from the source code
RUN go get -d ./...

# Now build
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -v \
    -ldflags='-w -s -extldflags "-static"' \
    -o docker-network-manager .

# Runtime stage - using alpine instead of scratch for curl
FROM alpine:latest

# Install curl for healthcheck
RUN apk --no-cache add curl ca-certificates

COPY --from=builder /build/docker-network-manager /docker-network-manager

# Healthcheck
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

USER 65534:65534

ENTRYPOINT ["/docker-network-manager"]
