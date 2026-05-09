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
// Used as proportional weights for quota cost estimation — only ratios matter.
// Model tier ratio: 1/3/5 (haiku/sonnet/opus).
var modelPricing = map[string]struct{ Input, Output float64 }{
	"claude-haiku-4-5-20251001": {1.00, 5.00},
	"claude-sonnet-4-6":         {3.00, 15.00},
	"claude-opus-4-6":           {5.00, 25.00},
	"claude-opus-4-7":           {5.00, 25.00},
}

const (
	cacheWriteMultiplier = 2.0  // 1-hour cached tokens (was 1.25 pre-Jan 2026)
	cacheReadMultiplier  = 0.10 // relative to input price
)

// RequestCost computes the weighted cost of a request in API-dollar-equivalent units.
// Returns (cost, true) for known models, or (0, false) for unknown models.
func RequestCost(model string, u *Usage) (float64, bool) {
	p, ok := modelPricing[canonicalModelID(model)]
	if !ok {
		return 0, false
	}
	weightedInput := float64(u.InputTokens) +
		cacheWriteMultiplier*float64(u.CacheCreationInputTokens) +
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
	return Usage{
		InputTokens:              int64FromJSON(m["input_tokens"]),
		OutputTokens:             int64FromJSON(m["output_tokens"]),
		CacheCreationInputTokens: int64FromJSON(m["cache_creation_input_tokens"]),
		CacheReadInputTokens:     int64FromJSON(m["cache_read_input_tokens"]),
	}, true
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
