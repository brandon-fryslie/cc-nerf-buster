package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// stringSlice implements flag.Value for repeatable --upstream-url flags.
type stringSlice []string

func (s *stringSlice) String() string { return strings.Join(*s, ",") }
func (s *stringSlice) Set(v string) error {
	*s = append(*s, v)
	return nil
}

func main() {
	var (
		port        int
		metricsPort int
		dataDir     string
		verbose     bool
		initCA      bool
		upstreams   stringSlice
		proxyChain  string
	)

	flag.IntVar(&port, "port", 9480, "Proxy listen port")
	flag.IntVar(&metricsPort, "metrics", 9481, "Prometheus metrics port")
	flag.StringVar(&dataDir, "data-dir", defaultDataDir(), "Data directory")
	flag.BoolVar(&verbose, "verbose", false, "Log activity to stderr")
	flag.BoolVar(&initCA, "init-ca", false, "Generate CA certificate and exit")
	flag.Var(&upstreams, "upstream-url", "Upstream API host to intercept (repeatable, default api.anthropic.com)")
	flag.StringVar(&proxyChain, "proxy", "", "Downstream proxy to chain through (e.g. http://localhost:8080)")
	flag.Parse()

	if len(upstreams) == 0 {
		upstreams = []string{"api.anthropic.com"}
	}

	// Create data directory
	if err := os.MkdirAll(dataDir, 0755); err != nil {
		log.Fatalf("failed to create data directory %s: %v", dataDir, err)
	}

	// --init-ca: generate CA and exit
	if initCA {
		if _, err := LoadOrCreateCA(dataDir); err != nil {
			log.Fatalf("CA generation failed: %v", err)
		}
		return
	}

	// Initialize components
	metrics := NewMetrics(dataDir)

	jsonlPath := filepath.Join(dataDir, "usage.jsonl")
	jsonlWriter, err := NewJSONLWriter(jsonlPath, metrics)
	if err != nil {
		log.Fatalf("failed to open JSONL log %s: %v", jsonlPath, err)
	}

	// Parse downstream proxy URL if provided
	var downstreamProxy *url.URL
	if proxyChain != "" {
		var err error
		downstreamProxy, err = url.Parse(proxyChain)
		if err != nil {
			log.Fatalf("invalid --proxy URL %q: %v", proxyChain, err)
		}
	}

	// Load or create CA for SSL inspection
	ca, err := LoadOrCreateCA(dataDir)
	if err != nil {
		log.Fatalf("failed to initialize CA: %v", err)
	}

	proxy := NewProxy(upstreams, metrics, jsonlWriter, verbose, downstreamProxy, ca)

	// Proxy server
	proxyServer := &http.Server{
		Addr:    fmt.Sprintf(":%d", port),
		Handler: proxy,
	}

	// Metrics server
	metricsMux := http.NewServeMux()
	metricsMux.Handle("/metrics", metrics)
	metricsServer := &http.Server{
		Addr:    fmt.Sprintf(":%d", metricsPort),
		Handler: metricsMux,
	}

	// Start servers
	errCh := make(chan error, 2)

	go func() {
		log.Printf("proxy listening on :%d", port)
		log.Printf("intercepting upstream hosts: %s", strings.Join(upstreams, ", "))
		if downstreamProxy != nil {
			log.Printf("chaining through downstream proxy: %s", downstreamProxy)
		}
		log.Printf("JSONL log: %s", jsonlPath)
		printClaudeCodeInstructions(port, metricsPort, dataDir)
		errCh <- proxyServer.ListenAndServe()
	}()

	go func() {
		log.Printf("metrics listening on :%d/metrics", metricsPort)
		errCh <- metricsServer.ListenAndServe()
	}()

	// Graceful shutdown
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	select {
	case <-ctx.Done():
		log.Println("shutting down...")
	case err := <-errCh:
		log.Fatalf("server error: %v", err)
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	proxyServer.Shutdown(shutdownCtx)
	metricsServer.Shutdown(shutdownCtx)
	jsonlWriter.Close()

	log.Println("shutdown complete")
}

// printClaudeCodeInstructions prints copy-pasteable setup instructions for Claude Code.
func printClaudeCodeInstructions(port, metricsPort int, dataDir string) {
	proxyURL := fmt.Sprintf("http://localhost:%d", port)
	caPath := filepath.Join(dataDir, "ca.crt")

	fmt.Fprintln(os.Stderr)
	fmt.Fprintln(os.Stderr, "╔══════════════════════════════════════════════════════════════╗")
	fmt.Fprintln(os.Stderr, "║                  Claude Code Setup                          ║")
	fmt.Fprintln(os.Stderr, "╚══════════════════════════════════════════════════════════════╝")
	fmt.Fprintln(os.Stderr)
	fmt.Fprintln(os.Stderr, "  Quick start — run Claude Code with the proxy:")
	fmt.Fprintln(os.Stderr)
	fmt.Fprintf(os.Stderr, "    https_proxy=%s NODE_EXTRA_CA_CERTS=%s claude\n", proxyURL, caPath)
	fmt.Fprintln(os.Stderr)
	fmt.Fprintln(os.Stderr, "  Persistent — add to ~/.claude/settings.json:")
	fmt.Fprintln(os.Stderr)
	fmt.Fprintln(os.Stderr, "    {")
	fmt.Fprintln(os.Stderr, "      \"env\": {")
	fmt.Fprintf(os.Stderr, "        \"https_proxy\": \"%s\",\n", proxyURL)
	fmt.Fprintf(os.Stderr, "        \"HTTPS_PROXY\": \"%s\",\n", proxyURL)
	fmt.Fprintf(os.Stderr, "        \"http_proxy\": \"%s\",\n", proxyURL)
	fmt.Fprintf(os.Stderr, "        \"HTTP_PROXY\": \"%s\",\n", proxyURL)
	fmt.Fprintf(os.Stderr, "        \"NODE_EXTRA_CA_CERTS\": \"%s\"\n", caPath)
	fmt.Fprintln(os.Stderr, "      }")
	fmt.Fprintln(os.Stderr, "    }")
	fmt.Fprintln(os.Stderr)
	fmt.Fprintln(os.Stderr, "  Trust the CA (macOS) — required once:")
	fmt.Fprintln(os.Stderr)
	fmt.Fprintf(os.Stderr, "    sudo security add-trusted-cert -d -r trustRoot \\\n")
	fmt.Fprintf(os.Stderr, "      -k /Library/Keychains/System.keychain %s\n", caPath)
	fmt.Fprintln(os.Stderr)
	fmt.Fprintf(os.Stderr, "  Metrics: http://localhost:%d/metrics\n", metricsPort)
	fmt.Fprintf(os.Stderr, "  Usage log: %s/usage.jsonl\n", dataDir)
	fmt.Fprintln(os.Stderr)
	fmt.Fprintln(os.Stderr, "──────────────────────────────────────────────────────────────")
	fmt.Fprintln(os.Stderr)
}

func defaultDataDir() string {
	if xdg := os.Getenv("XDG_DATA_HOME"); xdg != "" {
		return filepath.Join(xdg, "cc-nerf-buster")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".cc-nerf-buster"
	}
	return filepath.Join(home, ".local", "cc-nerf-buster")
}
