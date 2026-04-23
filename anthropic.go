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
// It returns the body reader (rewound) and the model string.
func extractModelFromRequest(body io.ReadCloser) (io.ReadCloser, *string, error) {
	data, err := io.ReadAll(body)
	body.Close()
	if err != nil {
		return io.NopCloser(strings.NewReader("")), nil, err
	}

	var req struct {
		Model string `json:"model"`
	}
	if err := json.Unmarshal(data, &req); err != nil {
		return io.NopCloser(strings.NewReader(string(data))), nil, err
	}

	var model *string
	if req.Model != "" {
		model = &req.Model
	}
	return io.NopCloser(strings.NewReader(string(data))), model, nil
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
	p, ok := modelPricing[model]
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
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)

	var (
		eventType string
		usage     Usage
		found     bool
	)

	for scanner.Scan() {
		line := scanner.Text()

		if strings.HasPrefix(line, "event: ") {
			eventType = strings.TrimPrefix(line, "event: ")
			continue
		}

		if !strings.HasPrefix(line, "data: ") {
			continue
		}

		if eventType != "message_start" && eventType != "message_delta" {
			continue
		}

		data := strings.TrimPrefix(line, "data: ")

		switch eventType {
		case "message_start":
			var msg struct {
				Message struct {
					Usage *Usage `json:"usage"`
				} `json:"message"`
			}
			if err := json.Unmarshal([]byte(data), &msg); err == nil && msg.Message.Usage != nil {
				usage.InputTokens = msg.Message.Usage.InputTokens
				usage.CacheCreationInputTokens = msg.Message.Usage.CacheCreationInputTokens
				usage.CacheReadInputTokens = msg.Message.Usage.CacheReadInputTokens
				found = true
			}

		case "message_delta":
			var msg struct {
				Usage *Usage `json:"usage"`
			}
			if err := json.Unmarshal([]byte(data), &msg); err == nil && msg.Usage != nil {
				usage.OutputTokens = msg.Usage.OutputTokens
				found = true
			}
		}
	}

	if !found {
		return nil, fmt.Errorf("no usage events found in SSE stream")
	}
	return &usage, scanner.Err()
}
