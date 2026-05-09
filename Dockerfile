FROM golang:1.25-alpine AS builder
WORKDIR /build
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-w -s" -o cc-nerf-buster .

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /build/cc-nerf-buster /usr/local/bin/cc-nerf-buster
ENTRYPOINT ["cc-nerf-buster"]
