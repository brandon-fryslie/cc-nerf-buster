package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// APIEvent is the top-level JSONL record written for every intercepted request.
type APIEvent struct {
	TS         time.Time    `json:"ts"`
	Upstream   string       `json:"upstream"`
	Model      *string      `json:"model"`
	Status     int          `json:"status"`
	DurationMs int64        `json:"duration_ms"`
	Streaming  bool         `json:"streaming"`
	Errors     []string     `json:"errors"`
	Usage      *Usage       `json:"usage"`
	Quota      *QuotaInfo   `json:"quota"`
	Meta       *RequestMeta `json:"meta"`
}

type Usage struct {
	InputTokens              int64 `json:"input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
	// Breakdown of CacheCreationInputTokens by TTL bucket. When present, their
	// sum equals CacheCreationInputTokens and they let RequestCost charge the
	// correct multiplier per bucket (5m=1.25×, 1h=2.0× base input). Absent on
	// older API responses — RequestCost falls back to the 1h rate in that case.
	CacheCreation5mInputTokens int64 `json:"cache_creation_5m_input_tokens,omitempty"`
	CacheCreation1hInputTokens int64 `json:"cache_creation_1h_input_tokens,omitempty"`
}

type QuotaInfo struct {
	UnifiedStatus       *string  `json:"unified_status"`
	UnifiedReset        *int64   `json:"unified_reset"`
	RepresentativeClaim *string  `json:"representative_claim"`
	FallbackPercentage  *float64 `json:"fallback_percentage"`
	Fallback            *string  `json:"fallback"`
	FiveHourStatus      *string  `json:"five_hour_status"`
	FiveHourReset       *int64   `json:"five_hour_reset"`
	FiveHourUtilization *float64 `json:"five_hour_utilization"`
	SevenDayStatus      *string  `json:"seven_day_status"`
	SevenDayReset       *int64   `json:"seven_day_reset"`
	SevenDayUtilization *float64 `json:"seven_day_utilization"`
	OverageStatus       *string  `json:"overage_status"`
	OverageReset        *int64   `json:"overage_reset"`
	OverageUtilization  *float64 `json:"overage_utilization"`
}

type RequestMeta struct {
	OrganizationID string `json:"organization_id"`
	RequestID      string `json:"request_id"`
	CFRay          string `json:"cf_ray"`
	Date           string `json:"date"`
}

// extractQuota reads unified rate-limit headers from the response.
// Returns nil if no quota headers are present.
func extractQuota(h http.Header) *QuotaInfo {
	q := &QuotaInfo{}
	found := false

	setStr := func(dst **string, key string) {
		if v := h.Get(key); v != "" {
			*dst = &v
			found = true
		}
	}
	setInt := func(dst **int64, key string) {
		if v := h.Get(key); v != "" {
			if n, err := strconv.ParseInt(v, 10, 64); err == nil {
				*dst = &n
				found = true
			}
		}
	}
	setFloat := func(dst **float64, key string) {
		if v := h.Get(key); v != "" {
			if f, err := strconv.ParseFloat(v, 64); err == nil {
				*dst = &f
				found = true
			}
		}
	}

	setStr(&q.UnifiedStatus, "anthropic-ratelimit-unified-status")
	setInt(&q.UnifiedReset, "anthropic-ratelimit-unified-reset")
	setStr(&q.RepresentativeClaim, "anthropic-ratelimit-unified-representative-claim")
	setFloat(&q.FallbackPercentage, "anthropic-ratelimit-unified-fallback-percentage")
	setStr(&q.Fallback, "anthropic-ratelimit-unified-fallback")

	setStr(&q.FiveHourStatus, "anthropic-ratelimit-unified-5h-status")
	setInt(&q.FiveHourReset, "anthropic-ratelimit-unified-5h-reset")
	setFloat(&q.FiveHourUtilization, "anthropic-ratelimit-unified-5h-utilization")

	setStr(&q.SevenDayStatus, "anthropic-ratelimit-unified-7d-status")
	setInt(&q.SevenDayReset, "anthropic-ratelimit-unified-7d-reset")
	setFloat(&q.SevenDayUtilization, "anthropic-ratelimit-unified-7d-utilization")

	setStr(&q.OverageStatus, "anthropic-ratelimit-unified-overage-status")
	setInt(&q.OverageReset, "anthropic-ratelimit-unified-overage-reset")
	setFloat(&q.OverageUtilization, "anthropic-ratelimit-unified-overage-utilization")

	if !found {
		return nil
	}
	return q
}

func canonicalModelID(model string) string {
	return strings.TrimSpace(model)
}

// extractMeta reads request metadata headers from the response.
func extractMeta(h http.Header) *RequestMeta {
	m := &RequestMeta{
		OrganizationID: h.Get("anthropic-organization-id"),
		RequestID:      h.Get("request-id"),
		CFRay:          h.Get("cf-ray"),
		Date:           h.Get("date"),
	}
	if m.OrganizationID == "" && m.RequestID == "" && m.CFRay == "" {
		return nil
	}
	return m
}

// extractModelFromRequest reads the request body to find the model field.
// Returns the body reader (rewound), the raw bytes (for HAR debug dumps), and the model string.
func extractModelFromRequest(body io.ReadCloser) (io.ReadCloser, []byte, *string, error) {
	data, err := io.ReadAll(body)
	body.Close()
	if err != nil {
		return io.NopCloser(strings.NewReader("")), nil, nil, err
	}

	var req struct {
		Model string `json:"model"`
	}
	if err := json.Unmarshal(data, &req); err != nil {
		return io.NopCloser(strings.NewReader(string(data))), data, nil, err
	}

	var model *string
	if req.Model != "" {
		canonical := canonicalModelID(req.Model)
		model = &canonical
	}
	return io.NopCloser(strings.NewReader(string(data))), data, model, nil
}

// modelPricing maps model IDs to their API pricing ($/MTok).
// Source: docs/pricing.md (Anthropic list price, captured 2026-05-11).
// These are real USD, not normalized weights — cost values produced by
// RequestCost are list-price dollars. Update docs/pricing.md and this
// table together.
var modelPricing = map[string]struct{ Input, Output float64 }{
	"claude-haiku-4-5-20251001": {1.00, 5.00},
	"claude-sonnet-4-6":         {3.00, 15.00},
	"claude-opus-4-6":           {5.00, 25.00},
	"claude-opus-4-7":           {5.00, 25.00},
}

// Multipliers relative to base input price. Source: docs/pricing.md.
// These ratios are constant across every current-gen model.
const (
	cacheWrite5mMultiplier = 1.25 // 5-minute ephemeral cache write
	cacheWrite1hMultiplier = 2.00 // 1-hour ephemeral cache write
	cacheReadMultiplier    = 0.10 // cache hit / refresh
)

// RequestCost computes the cost of a request in list-price USD.
// Returns (cost, true) for known models, or (0, false) for unknown models.
//
// Cache-creation tokens are charged at the per-bucket rate when the response
// provides the breakdown (CacheCreation5m/1hInputTokens). If only the
// aggregate CacheCreationInputTokens is set — older API responses, or any
// path that hasn't been updated to capture the breakdown — they fall back to
// the 1h rate. That's conservative (overcharges any 5m writes by 1.6×)
// rather than silently undercharging.
func RequestCost(model string, u *Usage) (float64, bool) {
	p, ok := modelPricing[canonicalModelID(model)]
	if !ok {
		return 0, false
	}
	cache5m := u.CacheCreation5mInputTokens
	cache1h := u.CacheCreation1hInputTokens
	if cache5m == 0 && cache1h == 0 {
		cache1h = u.CacheCreationInputTokens
	}
	weightedInput := float64(u.InputTokens) +
		cacheWrite5mMultiplier*float64(cache5m) +
		cacheWrite1hMultiplier*float64(cache1h) +
		cacheReadMultiplier*float64(u.CacheReadInputTokens)
	cost := (p.Input*weightedInput + p.Output*float64(u.OutputTokens)) / 1_000_000
	return cost, true
}

// usageResult is returned from the body parser goroutine.
type usageResult struct {
	Usage *Usage
	Err   error
}

type sseEvent struct {
	Event string
	Data  string
}

// extractUsageFromBody parses usage from a non-streaming JSON response body.
// The body has already been fully read into data.
func extractUsageFromBody(data []byte) (*Usage, error) {
	var resp struct {
		Usage *Usage `json:"usage"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, fmt.Errorf("json parse: %w", err)
	}
	return resp.Usage, nil
}

// extractUsageFromSSE scans an SSE stream for usage data.
// Reads from r until EOF. Returns the best usage data found.
func extractUsageFromSSE(r io.Reader) (*Usage, error) {
	var (
		usage Usage
		found bool
	)

	for _, event := range readSSEEvents(r) {
		if event.Event != "message_start" && event.Event != "message_delta" {
			continue
		}
		if eventUsage, ok := extractUsageFromEventData(event.Data); ok {
			usage = mergeUsage(usage, eventUsage)
			found = true
		}
	}

	if !found {
		return nil, fmt.Errorf("no usage events found in SSE stream")
	}
	return &usage, nil
}

func mergeUsage(base Usage, next Usage) Usage {
	if next.InputTokens != 0 {
		base.InputTokens = next.InputTokens
	}
	if next.OutputTokens != 0 {
		base.OutputTokens = next.OutputTokens
	}
	if next.CacheCreationInputTokens != 0 {
		base.CacheCreationInputTokens = next.CacheCreationInputTokens
	}
	if next.CacheCreation5mInputTokens != 0 {
		base.CacheCreation5mInputTokens = next.CacheCreation5mInputTokens
	}
	if next.CacheCreation1hInputTokens != 0 {
		base.CacheCreation1hInputTokens = next.CacheCreation1hInputTokens
	}
	if next.CacheReadInputTokens != 0 {
		base.CacheReadInputTokens = next.CacheReadInputTokens
	}
	return base
}

func extractUsageFromEventData(data string) (Usage, bool) {
	var payload any
	if err := json.Unmarshal([]byte(data), &payload); err != nil {
		return Usage{}, false
	}
	return findUsageValue(payload)
}

func findUsageValue(v any) (Usage, bool) {
	switch typed := v.(type) {
	case map[string]any:
		if rawUsage, ok := typed["usage"]; ok {
			if usage, ok := usageFromMap(rawUsage); ok {
				return usage, true
			}
		}
		for _, child := range typed {
			if usage, ok := findUsageValue(child); ok {
				return usage, true
			}
		}
	case []any:
		for _, child := range typed {
			if usage, ok := findUsageValue(child); ok {
				return usage, true
			}
		}
	}
	return Usage{}, false
}

func usageFromMap(v any) (Usage, bool) {
	m, ok := v.(map[string]any)
	if !ok {
		return Usage{}, false
	}
	u := Usage{
		InputTokens:              int64FromJSON(m["input_tokens"]),
		OutputTokens:             int64FromJSON(m["output_tokens"]),
		CacheCreationInputTokens: int64FromJSON(m["cache_creation_input_tokens"]),
		CacheReadInputTokens:     int64FromJSON(m["cache_read_input_tokens"]),
	}
	// The per-TTL breakdown lives in a nested "cache_creation" object:
	//   "cache_creation": { "ephemeral_5m_input_tokens": N, "ephemeral_1h_input_tokens": M }
	// When present, its components sum to cache_creation_input_tokens.
	if cc, ok := m["cache_creation"].(map[string]any); ok {
		u.CacheCreation5mInputTokens = int64FromJSON(cc["ephemeral_5m_input_tokens"])
		u.CacheCreation1hInputTokens = int64FromJSON(cc["ephemeral_1h_input_tokens"])
	}
	return u, true
}

func int64FromJSON(v any) int64 {
	switch typed := v.(type) {
	case float64:
		return int64(typed)
	case int64:
		return typed
	case int:
		return int64(typed)
	default:
		return 0
	}
}

// [LAW:single-enforcer] SSE framing is parsed in one place so usage extraction
// does not duplicate line/event boundary logic across call sites.
func readSSEEvents(r io.Reader) []sseEvent {
	br := bufio.NewReader(r)
	var (
		events    []sseEvent
		eventType string
		dataLines []string
	)

	flush := func() {
		if len(dataLines) == 0 {
			eventType = ""
			return
		}
		events = append(events, sseEvent{
			Event: eventType,
			Data:  strings.Join(dataLines, "\n"),
		})
		eventType = ""
		dataLines = nil
	}

	for {
		line, eof, err := readSSELine(br)
		if err != nil {
			break
		}

		if line == "" {
			flush()
		} else if strings.HasPrefix(line, ":") {
			// Comment line; ignore.
		} else if strings.HasPrefix(line, "event:") {
			eventType = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		} else if strings.HasPrefix(line, "data:") {
			data := strings.TrimPrefix(line, "data:")
			if strings.HasPrefix(data, " ") {
				data = data[1:]
			}
			dataLines = append(dataLines, data)
		}

		if eof {
			flush()
			break
		}
	}

	return events
}

func readSSELine(br *bufio.Reader) (line string, eof bool, err error) {
	var (
		fragments []byte
		prefix    bool
	)

	for {
		part, isPrefix, readErr := br.ReadLine()
		fragments = append(fragments, part...)
		prefix = isPrefix

		switch {
		case readErr == io.EOF:
			return string(fragments), true, nil
		case readErr != nil:
			return "", false, readErr
		case prefix:
			continue
		default:
			return string(fragments), false, nil
		}
	}
}
