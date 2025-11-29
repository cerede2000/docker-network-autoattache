# syntax=docker/dockerfile:1

# Build stage
FROM golang:1.21-alpine AS builder

# Install build dependencies
RUN apk add --no-cache git ca-certificates

WORKDIR /build

# Copy go mod files from src directory
#COPY src/go.mod src/go.sum* ./

# Download dependencies
#RUN go mod download

# Copy source code from src directory
COPY src/*.go ./

# Build the binary with optimizations
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -ldflags='-w -s -extldflags "-static"' \
    -a -installsuffix cgo \
    -o docker-network-manager .

# Final stage - minimal runtime image
FROM scratch

# Copy CA certificates for HTTPS
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Copy the binary
COPY --from=builder /build/docker-network-manager /docker-network-manager

# Use non-root user (even in scratch, this sets metadata)
USER 65534:65534

ENTRYPOINT ["/docker-network-manager"]
