# syntax=docker/dockerfile:1

# Build stage - Use Go 1.23
FROM golang:1.23-alpine AS builder

# Install build dependencies
RUN apk add --no-cache git ca-certificates

WORKDIR /build

# Copy go.mod
COPY src/go.mod ./

# Download dependencies
RUN go mod download

# Copy source code
COPY src/*.go ./

# Build directly
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -v \
    -ldflags='-w -s -extldflags "-static"' \
    -o docker-network-manager .

# Final stage
FROM scratch

COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /build/docker-network-manager /docker-network-manager

USER 65534:65534

ENTRYPOINT ["/docker-network-manager"]
