// mock-anthropic emulates the slice of api.anthropic.com/v1/messages that
// the cc-nerf-buster proxy and capacity_probe consume: a streaming SSE
// response carrying usage tokens plus the unified rate-limit utilization
// headers. State is server-local: each request advances a cumulative
// input-equivalent cost counter, which is divided by the configured per-
// window capacity to produce the 5h and 7d utilization headers. That lets
// a probe run watch utilization tick across realistic boundaries without
// touching real Anthropic infrastructure.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strconv"
	"sync/atomic"
	"time"
)

// Pricing weights mirror anthropic.go and probe.py exactly so the cost the
// mock attributes to a request matches what the proxy will compute from the
// usage values we hand back. Drift here would silently mis-bucket ticks.
const (
	cacheWriteWeight = 2.0
	cacheReadWeight  = 0.10
	outputWeight     = 5.0
)

// Default per-tick cost matches probe.py's DEFAULT_INPUT_EQUIV_PER_TICK for
// 5h. A "window" holds 100 ticks; capacity = perTickCost * 100.
const defaultInputEquivPerTick = 550_623.0

// charsPerToken approximates the Claude tokenizer at the granularity the
// probe already assumes (probe.py's MICRO_CHARS_PER_TOKEN).
const charsPerToken = 3.6

// cacheThresholdTokens mirrors Anthropic's 1024-token floor for prompt
// caching. Below this, the request is billed as plain input; at or above,
// the bulk of the prompt becomes cache_creation tokens on the first call.
const cacheThresholdTokens = 1024

type config struct {
	addr         string
	orgID        string
	perTickCost  float64
	sevenDayMult float64
	model        string
	stopText     string
	verbose      bool
}

type server struct {
	cfg          config
	cumCost      atomic.Uint64 // microcost units (1 input-equiv = 1_000_000 microcost) so we can use atomic
	requestCount atomic.Uint64
	startTime    time.Time
}

func newServer(cfg config) *server {
	return &server{
		cfg:       cfg,
		startTime: time.Now(),
	}
}

const microPerUnit = 1_000_000

func (s *server) addCost(units float64) float64 {
	delta := uint64(units * microPerUnit)
	total := s.cumCost.Add(delta)
	return float64(total) / microPerUnit
}

func (s *server) currentCost() float64 {
	return float64(s.cumCost.Load()) / microPerUnit
}

// requestBody is the subset of /v1/messages we read. Anthropic's real schema
// is much larger; we only need what feeds usage estimation.
type requestBody struct {
	Model    string           `json:"model"`
	Messages []requestMessage `json:"messages"`
	System   any              `json:"system"`
	Stream   bool             `json:"stream"`
}

type requestMessage struct {
	Role    string `json:"role"`
	Content any    `json:"content"`
}

// estimateUsage produces (input, output, cache_create, cache_read) token
// counts that scale with the request body. The point isn't tokenizer
// fidelity — the probe never compares to Anthropic's true tokenization —
// it's that the same prompt yields the same numbers, larger prompts yield
// larger numbers, and the input-equiv cost matches what probe.py expects
// per prompt size. cache_read currently always returns 0; if a cache_read
// path is added later, accept the prompt-cache map then.
func estimateUsage(body []byte) (input, output, cacheCreate, cacheRead int64) {
	totalChars := int64(len(body))
	totalTokens := totalChars * 10 / int64(charsPerToken*10) // avoid float in hot path

	// Output is short and bounded — the probe's prompt asks for a single
	// short sentence and the proxy treats output as 5x input weight, so a
	// stable small value keeps the per-call cost dominated by input/cache.
	output = 15

	if totalTokens < cacheThresholdTokens {
		// Below the cache threshold the request bills as plain input only.
		input = totalTokens
		return input, output, 0, 0
	}

	// Above the threshold: the first 5 tokens are billed as plain input
	// (Anthropic's typical pattern: the system + first turn header is not
	// cached), the bulk becomes cache_create on a cold prompt and cache_read
	// on a warm one. The probe sets a fresh timestamp at the top of every
	// prompt so cache_read stays low in practice — we model that by hashing
	// the *body excluding the timestamp prefix* later if needed; for now,
	// every probe call is a fresh prompt → cache_create dominates.
	input = 5
	cacheCreate = totalTokens - input
	cacheRead = 0
	return input, output, cacheCreate, cacheRead
}

// inputEquiv mirrors probe.py's quota_input_equivalent_tokens.
func inputEquiv(input, output, cacheCreate, cacheRead int64) float64 {
	return float64(input) +
		outputWeight*float64(output) +
		cacheWriteWeight*float64(cacheCreate) +
		cacheReadWeight*float64(cacheRead)
}

func (s *server) handleMessages(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "read body: "+err.Error(), http.StatusBadRequest)
		return
	}
	r.Body.Close()

	// Pull model out of the body so the response shape matches the request,
	// the same way real Anthropic echoes the model back in message_start.
	// Real Anthropic returns 400 for malformed JSON; matching that behaviour
	// keeps probe-against-mock parity with probe-against-prod, otherwise a
	// probe bug that ships malformed bodies passes locally and 400s in prod.
	var req requestBody
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, "invalid_request_error: "+err.Error(), http.StatusBadRequest)
		return
	}
	model := req.Model
	if model == "" {
		model = s.cfg.model
	}

	input, output, cacheCreate, cacheRead := estimateUsage(body)
	cost := inputEquiv(input, output, cacheCreate, cacheRead)
	totalCost := s.addCost(cost)
	reqNum := s.requestCount.Add(1)

	util5h := totalCost / (s.cfg.perTickCost * 100)
	util7d := totalCost / (s.cfg.perTickCost * 100 * s.cfg.sevenDayMult)

	now := time.Now().UTC()
	reset5h := now.Add(5 * time.Hour).Unix()
	reset7d := now.Add(7 * 24 * time.Hour).Unix()
	requestID := fmt.Sprintf("req_mock_%d_%d", now.UnixNano(), reqNum)
	cfRay := fmt.Sprintf("%016x-MOCK", now.UnixNano())

	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("Connection", "keep-alive")
	h.Set("anthropic-organization-id", s.cfg.orgID)
	h.Set("request-id", requestID)
	h.Set("cf-ray", cfRay)
	h.Set("Date", now.Format(http.TimeFormat))

	h.Set("anthropic-ratelimit-unified-status", "allowed")
	h.Set("anthropic-ratelimit-unified-reset", strconv.FormatInt(reset5h, 10))
	h.Set("anthropic-ratelimit-unified-5h-status", "allowed")
	h.Set("anthropic-ratelimit-unified-5h-utilization", formatUtil(util5h))
	h.Set("anthropic-ratelimit-unified-5h-reset", strconv.FormatInt(reset5h, 10))
	h.Set("anthropic-ratelimit-unified-7d-status", "allowed")
	h.Set("anthropic-ratelimit-unified-7d-utilization", formatUtil(util7d))
	h.Set("anthropic-ratelimit-unified-7d-reset", strconv.FormatInt(reset7d, 10))

	w.WriteHeader(http.StatusOK)
	flusher, _ := w.(http.Flusher)

	msgID := fmt.Sprintf("msg_mock_%d", reqNum)
	startUsage := map[string]any{
		"input_tokens":                input,
		"output_tokens":               1,
		"cache_creation_input_tokens": cacheCreate,
		"cache_read_input_tokens":     cacheRead,
		"service_tier":                "standard",
	}
	startMsg := map[string]any{
		"type": "message_start",
		"message": map[string]any{
			"id":            msgID,
			"type":          "message",
			"role":          "assistant",
			"model":         model,
			"content":       []any{},
			"stop_reason":   nil,
			"stop_sequence": nil,
			"usage":         startUsage,
		},
	}
	writeSSE(w, flusher, "message_start", startMsg)

	writeSSE(w, flusher, "content_block_start", map[string]any{
		"type":  "content_block_start",
		"index": 0,
		"content_block": map[string]any{
			"type": "text",
			"text": "",
		},
	})

	writeSSE(w, flusher, "content_block_delta", map[string]any{
		"type":  "content_block_delta",
		"index": 0,
		"delta": map[string]any{
			"type": "text_delta",
			"text": s.cfg.stopText,
		},
	})

	writeSSE(w, flusher, "content_block_stop", map[string]any{
		"type":  "content_block_stop",
		"index": 0,
	})

	deltaUsage := map[string]any{
		"input_tokens":                input,
		"output_tokens":               output,
		"cache_creation_input_tokens": cacheCreate,
		"cache_read_input_tokens":     cacheRead,
	}
	writeSSE(w, flusher, "message_delta", map[string]any{
		"type": "message_delta",
		"delta": map[string]any{
			"stop_reason":   "end_turn",
			"stop_sequence": nil,
		},
		"usage": deltaUsage,
	})

	writeSSE(w, flusher, "message_stop", map[string]any{
		"type": "message_stop",
	})

	if s.cfg.verbose {
		log.Printf("req=%d body=%dB usage=in:%d out:%d cw:%d cr:%d cost=%.0f total=%.0f util_5h=%s util_7d=%s",
			reqNum, len(body), input, output, cacheCreate, cacheRead,
			cost, totalCost, formatUtil(util5h), formatUtil(util7d))
	}
}

func formatUtil(u float64) string {
	if u < 0 {
		u = 0
	}
	if u > 1.0 {
		u = 1.0
	}
	return strconv.FormatFloat(u, 'f', 4, 64)
}

func writeSSE(w io.Writer, flusher http.Flusher, event string, data any) {
	payload, err := json.Marshal(data)
	if err != nil {
		// Marshal of a fixed map[string]any shape only fails on a programming
		// error (channel/func value, NaN, cyclic ref). Failing the test loudly
		// beats a half-streamed response that looks like a network truncation.
		log.Panicf("mock-anthropic: marshal %s event: %v", event, err)
	}
	var b bytes.Buffer
	fmt.Fprintf(&b, "event: %s\n", event)
	fmt.Fprintf(&b, "data: %s\n\n", payload)
	w.Write(b.Bytes())
	if flusher != nil {
		flusher.Flush()
	}
}

func (s *server) handleState(w http.ResponseWriter, _ *http.Request) {
	state := map[string]any{
		"requests":        s.requestCount.Load(),
		"cumulative_cost": s.currentCost(),
		"per_tick_cost":   s.cfg.perTickCost,
		"capacity_5h":     s.cfg.perTickCost * 100,
		"capacity_7d":     s.cfg.perTickCost * 100 * s.cfg.sevenDayMult,
		"util_5h":         s.currentCost() / (s.cfg.perTickCost * 100),
		"util_7d":         s.currentCost() / (s.cfg.perTickCost * 100 * s.cfg.sevenDayMult),
		"uptime_seconds":  time.Since(s.startTime).Seconds(),
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(state)
}

func (s *server) handleReset(w http.ResponseWriter, _ *http.Request) {
	s.cumCost.Store(0)
	s.requestCount.Store(0)
	w.WriteHeader(http.StatusNoContent)
}

func main() {
	var cfg config
	flag.StringVar(&cfg.addr, "addr", "127.0.0.1:0", "Listen address (default: random free port on localhost)")
	flag.StringVar(&cfg.orgID, "org-id", "mock-org-00000000", "Value for anthropic-organization-id header")
	flag.Float64Var(&cfg.perTickCost, "per-tick-cost", defaultInputEquivPerTick,
		"Input-equivalent tokens per 1% utilization tick (matches probe.py default)")
	flag.Float64Var(&cfg.sevenDayMult, "seven-day-multiplier", 5.0,
		"7d capacity = perTickCost * 100 * this (matches probe.py default of 5x)")
	flag.StringVar(&cfg.model, "model", "claude-opus-4-7", "Default model name when request omits it")
	flag.StringVar(&cfg.stopText, "stop-text", "The notes describe normal service activity.",
		"Text returned in the content_block_delta")
	flag.BoolVar(&cfg.verbose, "verbose", false, "Log each request")
	flag.Parse()

	srv := newServer(cfg)

	mux := http.NewServeMux()
	mux.HandleFunc("/v1/messages", srv.handleMessages)
	mux.HandleFunc("/__mock/state", srv.handleState)
	mux.HandleFunc("/__mock/reset", srv.handleReset)

	httpSrv := &http.Server{Handler: mux}

	listener, err := net.Listen("tcp", cfg.addr)
	if err != nil {
		log.Fatalf("listen %s: %v", cfg.addr, err)
	}

	log.Printf("mock-anthropic listening on http://%s", listener.Addr())
	log.Printf("  per_tick_cost=%.0f  capacity_5h=%.0f  capacity_7d=%.0f",
		cfg.perTickCost, cfg.perTickCost*100, cfg.perTickCost*100*cfg.sevenDayMult)

	if err := httpSrv.Serve(listener); err != nil && err != http.ErrServerClosed {
		log.Fatalf("serve: %v", err)
	}
}
