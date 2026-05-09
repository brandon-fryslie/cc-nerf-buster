package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Proxy is the forward HTTP proxy that captures API traffic.
type Proxy struct {
	upstreamHosts    map[string]bool // hosts to intercept
	metrics          *Metrics
	jsonlWriter      *JSONLWriter
	verbose          bool
	downstreamProxy  *url.URL        // nil = direct connect; set = chain through this proxy
	ca               *CertAuthority  // SSL inspection CA for intercepting CONNECT on captured hosts
	captureTransport *http.Transport // transport for captured upstream HTTPS requests
	plainTransport   *http.Transport // transport for non-captured plain HTTP requests
}

func NewProxy(upstreamHosts []string, metrics *Metrics, jsonlWriter *JSONLWriter, verbose bool, downstreamProxy *url.URL, ca *CertAuthority) *Proxy {
	hosts := make(map[string]bool, len(upstreamHosts))
	for _, h := range upstreamHosts {
		hosts[h] = true
	}

	// Transport for captured traffic (Anthropic HTTPS requests).
	// If chaining, Go's Transport handles CONNECT tunneling to the downstream proxy automatically.
	captureTransport := &http.Transport{
		TLSClientConfig: &tls.Config{},
		MaxIdleConns:    100,
		IdleConnTimeout: 90 * time.Second,
	}
	if downstreamProxy != nil {
		captureTransport.Proxy = http.ProxyURL(downstreamProxy)
	}

	// Transport for non-captured plain HTTP forwarding.
	plainTransport := &http.Transport{
		MaxIdleConns:    100,
		IdleConnTimeout: 90 * time.Second,
	}
	if downstreamProxy != nil {
		plainTransport.Proxy = http.ProxyURL(downstreamProxy)
	}

	return &Proxy{
		upstreamHosts:    hosts,
		metrics:          metrics,
		jsonlWriter:      jsonlWriter,
		verbose:          verbose,
		downstreamProxy:  downstreamProxy,
		ca:               ca,
		captureTransport: captureTransport,
		plainTransport:   plainTransport,
	}
}

// shouldCapture checks if the host (without port) is in the upstream list.
func (p *Proxy) shouldCapture(host string) bool {
	h := host
	if idx := strings.LastIndex(h, ":"); idx != -1 {
		h = h[:idx]
	}
	return p.upstreamHosts[h]
}

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodConnect {
		p.handleConnect(w, r)
		return
	}

	// Plain HTTP forward for non-captured hosts
	p.forwardPlain(w, r)
}

// handleConnect either tunnels blindly or performs SSL inspection for captured hosts.
func (p *Proxy) handleConnect(w http.ResponseWriter, r *http.Request) {
	// SSL inspection path: intercept CONNECT for captured hosts
	if p.shouldCapture(r.Host) {
		p.handleSSLInspect(w, r)
		return
	}

	// Blind tunnel for everything else
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "hijack not supported", http.StatusInternalServerError)
		return
	}

	targetAddr := r.Host
	if !strings.Contains(targetAddr, ":") {
		targetAddr += ":443"
	}

	var upstream net.Conn
	var err error

	if p.downstreamProxy != nil {
		upstream, err = p.connectViaProxy(targetAddr)
	} else {
		upstream, err = net.DialTimeout("tcp", targetAddr, 10*time.Second)
	}
	if err != nil {
		http.Error(w, fmt.Sprintf("connect to %s failed: %v", targetAddr, err), http.StatusBadGateway)
		return
	}

	w.WriteHeader(http.StatusOK)

	clientConn, _, err := hj.Hijack()
	if err != nil {
		upstream.Close()
		return
	}

	go transfer(upstream, clientConn)
	go transfer(clientConn, upstream)
}

// handleSSLInspect intercepts a CONNECT tunnel for a captured host.
// It presents a forged TLS cert to the client and forwards requests to the real upstream.
func (p *Proxy) handleSSLInspect(w http.ResponseWriter, r *http.Request) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "hijack not supported", http.StatusInternalServerError)
		return
	}

	host := r.Host
	hostOnly := host
	if idx := strings.LastIndex(hostOnly, ":"); idx != -1 {
		hostOnly = hostOnly[:idx]
	}

	// Get or generate a leaf cert for this host
	leafCert, err := p.ca.CertForHost(hostOnly)
	if err != nil {
		http.Error(w, fmt.Sprintf("cert generation failed: %v", err), http.StatusInternalServerError)
		return
	}

	// Tell the client the tunnel is established
	w.WriteHeader(http.StatusOK)

	clientConn, _, err := hj.Hijack()
	if err != nil {
		return
	}

	// TLS handshake with the client using our forged cert
	tlsConfig := &tls.Config{
		Certificates: []tls.Certificate{*leafCert},
	}
	tlsConn := tls.Server(clientConn, tlsConfig)
	if err := tlsConn.HandshakeContext(r.Context()); err != nil {
		throttledLog("ssl_inspect_handshake", "TLS handshake with client failed for %s: %v — ensure CA is trusted", hostOnly, err)
		clientConn.Close()
		return
	}

	// Serve HTTP on the decrypted connection.
	// Each request on this connection goes through forwardWithCapture.
	inspectHandler := http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		// The request arrives with a relative URL; set the full target
		req.URL.Scheme = "https"
		req.URL.Host = host
		req.Host = hostOnly
		p.forwardWithCapture(w, req)
	})

	// Use HTTP/1.1 server on the hijacked TLS connection.
	// ServeConn handles multiple requests (keep-alive) on one connection.
	server := &http.Server{
		Handler: inspectHandler,
	}
	connListener := &singleConnListener{conn: tlsConn}
	server.Serve(connListener)
}

// singleConnListener wraps a single net.Conn as a net.Listener.
// Accept returns the conn once, then blocks until Close.
type singleConnListener struct {
	conn net.Conn
	once sync.Once
	ch   chan struct{}
}

func (l *singleConnListener) Accept() (net.Conn, error) {
	var conn net.Conn
	l.once.Do(func() {
		conn = l.conn
		l.ch = make(chan struct{})
	})
	if conn != nil {
		return conn, nil
	}
	// Block until Close is called
	if l.ch != nil {
		<-l.ch
	}
	return nil, fmt.Errorf("listener closed")
}

func (l *singleConnListener) Close() error {
	if l.ch != nil {
		select {
		case <-l.ch:
		default:
			close(l.ch)
		}
	}
	return nil
}

func (l *singleConnListener) Addr() net.Addr {
	return l.conn.LocalAddr()
}

// connectViaProxy establishes a CONNECT tunnel through the downstream proxy.
// Returns a net.Conn that is the raw tunnel to the target through the proxy.
func (p *Proxy) connectViaProxy(targetAddr string) (net.Conn, error) {
	proxyAddr := p.downstreamProxy.Host
	if !strings.Contains(proxyAddr, ":") {
		if p.downstreamProxy.Scheme == "https" {
			proxyAddr += ":443"
		} else {
			proxyAddr += ":80"
		}
	}

	conn, err := net.DialTimeout("tcp", proxyAddr, 10*time.Second)
	if err != nil {
		return nil, fmt.Errorf("dial downstream proxy %s: %w", proxyAddr, err)
	}

	connectReq := fmt.Sprintf("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", targetAddr, targetAddr)
	if _, err := conn.Write([]byte(connectReq)); err != nil {
		conn.Close()
		return nil, fmt.Errorf("write CONNECT to downstream proxy: %w", err)
	}

	// Read the response status line and headers manually.
	// Cannot use http.ReadResponse here — it treats a CONNECT 200 as having
	// an infinite body (no Content-Length = read until close), so Body.Close()
	// would block forever trying to drain the tunnel.
	br := bufio.NewReader(conn)
	statusLine, err := br.ReadString('\n')
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("read CONNECT status from downstream proxy: %w", err)
	}

	// Parse "HTTP/1.x 200 ..."
	parts := strings.SplitN(strings.TrimSpace(statusLine), " ", 3)
	if len(parts) < 2 || parts[1] != "200" {
		conn.Close()
		return nil, fmt.Errorf("downstream proxy CONNECT returned: %s", strings.TrimSpace(statusLine))
	}

	// Drain remaining headers until blank line
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			conn.Close()
			return nil, fmt.Errorf("read CONNECT headers from downstream proxy: %w", err)
		}
		if strings.TrimSpace(line) == "" {
			break
		}
	}

	// If the bufio.Reader buffered bytes beyond the headers (first tunnel bytes),
	// wrap conn so those bytes are read first.
	if br.Buffered() > 0 {
		return &bufferedConn{Conn: conn, reader: br}, nil
	}
	return conn, nil
}

// bufferedConn wraps a net.Conn with a bufio.Reader to drain buffered bytes first.
type bufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (bc *bufferedConn) Read(p []byte) (int, error) {
	return bc.reader.Read(p)
}

func transfer(dst, src net.Conn) {
	defer dst.Close()
	defer src.Close()
	io.Copy(dst, src)
}

func isMeasuredAnthropicRequest(method, path string) bool {
	return method == http.MethodPost && path == "/v1/messages"
}

func copyHeaders(dst, src http.Header) {
	for key, vals := range src {
		for _, val := range vals {
			dst.Add(key, val)
		}
	}
}

func dumpFailedMeasuredSSE(dataDir string, capture []byte) {
	path := filepath.Join(dataDir, "last_stream_incomplete.sse")
	if err := os.WriteFile(path, capture, 0644); err != nil {
		throttledLog("stream_incomplete_dump", "failed to write %s: %v", path, err)
	}
}

func decodeMeasuredSSECapture(capture []byte) ([]byte, error) {
	if len(capture) < 2 || capture[0] != 0x1f || capture[1] != 0x8b {
		return capture, nil
	}

	zr, err := gzip.NewReader(bytes.NewReader(capture))
	if err != nil {
		return nil, err
	}
	defer zr.Close()

	return io.ReadAll(zr)
}

func measuredUpstreamContext(ctx context.Context) context.Context {
	// [LAW:single-enforcer] the proxy owns cancellation policy for measured
	// upstream requests so downstream client disconnects do not truncate the
	// canonical usage event before Anthropic emits final usage.
	return context.WithoutCancel(ctx)
}

// forwardWithCapture handles plain HTTP requests to captured upstream hosts.
// It upgrades the connection to HTTPS upstream and extracts usage data.
func (p *Proxy) forwardWithCapture(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	upstreamHost := r.Host
	hostOnly := upstreamHost
	if idx := strings.LastIndex(hostOnly, ":"); idx != -1 {
		hostOnly = hostOnly[:idx]
	}

	if !isMeasuredAnthropicRequest(r.Method, r.URL.Path) {
		// [LAW:single-enforcer] measurement is gated at the proxy boundary so
		// non-billable Anthropic requests do not leak into quota accounting.
		p.forwardCapturedPassthrough(w, r, upstreamHost)
		return
	}

	var errors []string
	addError := func(code string) { errors = append(errors, code) }

	// Read request body to extract model
	var model *string
	var reqBody io.ReadCloser
	if r.Body != nil {
		var err error
		reqBody, model, err = extractModelFromRequest(r.Body)
		if err != nil {
			addError("request_body_unreadable")
			if p.verbose {
				throttledLog("request_parse", "failed to parse request body: %v", err)
			}
		}
	} else {
		reqBody = http.NoBody
	}

	// Build upstream request
	upstreamURL := "https://" + upstreamHost + r.URL.Path
	if r.URL.RawQuery != "" {
		upstreamURL += "?" + r.URL.RawQuery
	}

	upstreamReq, err := http.NewRequestWithContext(measuredUpstreamContext(r.Context()), r.Method, upstreamURL, reqBody)
	if err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}

	copyHeaders(upstreamReq.Header, r.Header)

	resp, err := p.captureTransport.RoundTrip(upstreamReq)
	if err != nil {
		http.Error(w, fmt.Sprintf("upstream error: %v", err), http.StatusBadGateway)
		event := &APIEvent{
			TS:         start,
			Upstream:   hostOnly,
			Model:      model,
			Status:     502,
			DurationMs: time.Since(start).Milliseconds(),
			Errors:     append(errors, "upstream_unreachable"),
		}
		p.metrics.Record(event)
		p.jsonlWriter.Write(event)
		return
	}
	defer resp.Body.Close()

	// Extract quota and metadata from response headers
	quota := extractQuota(resp.Header)
	meta := extractMeta(resp.Header)

	if quota == nil {
		addError("quota_headers_missing")
	}

	isStreaming := strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream")

	copyHeaders(w.Header(), resp.Header)
	w.WriteHeader(resp.StatusCode)

	// Stream body to client while extracting usage
	var usage *Usage
	if isStreaming {
		usage, err = p.streamSSEWithCapture(w, resp.Body)
		if err != nil {
			addError("stream_incomplete")
		}
	} else {
		usage, err = p.forwardBodyWithCapture(w, resp.Body)
		if err != nil {
			addError("parse_error")
		}
	}

	if model == nil && !containsError(errors, "request_body_unreadable") {
		addError("model_field_missing")
	}

	var errorsOut []string
	if len(errors) > 0 {
		errorsOut = errors
	}

	event := &APIEvent{
		TS:         start,
		Upstream:   hostOnly,
		Model:      model,
		Status:     resp.StatusCode,
		DurationMs: time.Since(start).Milliseconds(),
		Streaming:  isStreaming,
		Errors:     errorsOut,
		Usage:      usage,
		Quota:      quota,
		Meta:       meta,
	}

	p.metrics.Record(event)
	p.jsonlWriter.Write(event)

	if p.verbose {
		modelStr := "unknown"
		if model != nil {
			modelStr = *model
		}
		log.Printf("captured %s %s model=%s status=%d duration=%dms",
			r.Method, r.URL.Path, modelStr, resp.StatusCode, event.DurationMs)
	}
}

func (p *Proxy) forwardCapturedPassthrough(w http.ResponseWriter, r *http.Request, upstreamHost string) {
	upstreamURL := "https://" + upstreamHost + r.URL.Path
	if r.URL.RawQuery != "" {
		upstreamURL += "?" + r.URL.RawQuery
	}

	upstreamReq, err := http.NewRequestWithContext(r.Context(), r.Method, upstreamURL, r.Body)
	if err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}

	copyHeaders(upstreamReq.Header, r.Header)

	resp, err := p.captureTransport.RoundTrip(upstreamReq)
	if err != nil {
		http.Error(w, fmt.Sprintf("upstream error: %v", err), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	copyHeaders(w.Header(), resp.Header)
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// streamSSEWithCapture pipes an SSE stream to the client while extracting usage.
func (p *Proxy) streamSSEWithCapture(w http.ResponseWriter, body io.Reader) (*Usage, error) {
	flusher, canFlush := w.(http.Flusher)
	var downstreamWriteErr error
	const maxCapture = 10 * 1024 * 1024
	var capture bytes.Buffer

	buf := make([]byte, 32*1024)
	for {
		n, readErr := body.Read(buf)
		if n > 0 {
			if capture.Len()+n <= maxCapture {
				capture.Write(buf[:n])
			}
			// [LAW:dataflow-not-control-flow] keep draining the upstream stream in
			// the same order even if the downstream client stops reading so usage
			// extraction still sees the full response and remains measurable.
			if downstreamWriteErr == nil {
				if _, writeErr := w.Write(buf[:n]); writeErr != nil {
					downstreamWriteErr = writeErr
				}
			}
			if downstreamWriteErr == nil && canFlush {
				flusher.Flush()
			}
		}
		if readErr != nil {
			if capture.Len() > maxCapture {
				return nil, fmt.Errorf("stream exceeded %d bytes", maxCapture)
			}
			decoded, decodeErr := decodeMeasuredSSECapture(capture.Bytes())
			if decodeErr != nil {
				dumpFailedMeasuredSSE(p.metrics.dataDir, capture.Bytes())
				return nil, fmt.Errorf("decode stream capture: %w", decodeErr)
			}
			usage, err := extractUsageFromSSE(bytes.NewReader(decoded))
			if err == nil {
				return usage, nil
			}
			if capture.Len() > 0 {
				dumpFailedMeasuredSSE(p.metrics.dataDir, capture.Bytes())
			}
			if readErr != io.EOF {
				return nil, fmt.Errorf("stream read: %w", readErr)
			}
			return usage, err
		}
	}
}

// forwardBodyWithCapture reads the full response body, forwards to client, and extracts usage.
func (p *Proxy) forwardBodyWithCapture(w http.ResponseWriter, body io.Reader) (*Usage, error) {
	const maxCapture = 10 * 1024 * 1024 // 10MB

	var buf bytes.Buffer
	limited := io.LimitReader(body, maxCapture+1)
	tee := io.TeeReader(limited, &buf)

	written, err := io.Copy(w, tee)
	if err != nil {
		return nil, err
	}

	// If there's more data beyond our limit, drain it to the client without capturing
	if written > maxCapture {
		io.Copy(w, body)
		return nil, fmt.Errorf("body exceeded %d bytes", maxCapture)
	}

	return extractUsageFromBody(buf.Bytes())
}

// forwardPlain forwards non-captured HTTP requests as-is.
func (p *Proxy) forwardPlain(w http.ResponseWriter, r *http.Request) {
	resp, err := p.plainTransport.RoundTrip(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for key, vals := range resp.Header {
		for _, val := range vals {
			w.Header().Add(key, val)
		}
	}
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

func containsError(errors []string, code string) bool {
	for _, e := range errors {
		if e == code {
			return true
		}
	}
	return false
}

// ListenTransparent accepts raw TLS connections on addr and intercepts them
// using SNI to determine the target host. Intended for /etc/hosts-redirect
// deployments where clients connect directly without a CONNECT proxy.
// Returns nil on clean shutdown (ctx cancelled).
func (p *Proxy) ListenTransparent(ctx context.Context, addr string) error {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	go func() {
		<-ctx.Done()
		ln.Close()
	}()
	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return nil
			default:
				return err
			}
		}
		go p.handleTransparentConn(conn)
	}
}

// handleTransparentConn performs TLS interception on a raw incoming connection.
// The SNI from the ClientHello determines the forged cert and upstream host.
func (p *Proxy) handleTransparentConn(conn net.Conn) {
	tlsConfig := &tls.Config{
		GetCertificate: func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
			if hello.ServerName == "" {
				return nil, fmt.Errorf("client sent no SNI")
			}
			return p.ca.CertForHost(hello.ServerName)
		},
	}
	tlsConn := tls.Server(conn, tlsConfig)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	err := tlsConn.HandshakeContext(ctx)
	cancel()
	if err != nil {
		throttledLog("transparent_handshake", "TLS handshake failed: %v — ensure CA cert is trusted and NODE_EXTRA_CA_CERTS is set", err)
		conn.Close()
		return
	}

	host := tlsConn.ConnectionState().ServerName
	if host == "" {
		throttledLog("transparent_no_sni", "client connected without SNI; cannot determine upstream host")
		tlsConn.Close()
		return
	}

	inspectHandler := http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		req.URL.Scheme = "https"
		req.URL.Host = host
		req.Host = host
		p.forwardWithCapture(w, req)
	})

	server := &http.Server{Handler: inspectHandler}
	server.Serve(&singleConnListener{conn: tlsConn})
}
