package main

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"testing"
)

func TestIsMeasuredAnthropicRequest(t *testing.T) {
	tests := []struct {
		method string
		path   string
		want   bool
	}{
		{method: http.MethodPost, path: "/v1/messages", want: true},
		{method: http.MethodGet, path: "/v1/messages", want: false},
		{method: http.MethodPost, path: "/v1/messages/count_tokens", want: false},
		{method: http.MethodPost, path: "/oauth/token", want: false},
	}

	for _, tc := range tests {
		if got := isMeasuredAnthropicRequest(tc.method, tc.path); got != tc.want {
			t.Fatalf("isMeasuredAnthropicRequest(%q, %q) = %v, want %v", tc.method, tc.path, got, tc.want)
		}
	}
}

func TestMeasuredUpstreamContextIgnoresParentCancellation(t *testing.T) {
	parent, cancel := context.WithCancel(context.Background())
	ctx := measuredUpstreamContext(parent)
	cancel()
	select {
	case <-ctx.Done():
		t.Fatal("measuredUpstreamContext should ignore parent cancellation")
	default:
	}
}

type failingResponseWriter struct{}

func (f *failingResponseWriter) Header() http.Header {
	return http.Header{}
}

func (f *failingResponseWriter) WriteHeader(_ int) {}

func (f *failingResponseWriter) Write(_ []byte) (int, error) {
	return 0, errors.New("downstream closed")
}

func TestStreamSSEWithCaptureKeepsReadingAfterDownstreamWriteError(t *testing.T) {
	stream := strings.Join([]string{
		"event: message_start",
		`data: {"message":{"usage":{"input_tokens":12,"cache_creation_input_tokens":34,"cache_read_input_tokens":56}}}`,
		"",
		"event: message_delta",
		`data: {"usage":{"output_tokens":78}}`,
		"",
	}, "\n")

	usage, err := (&Proxy{}).streamSSEWithCapture(&failingResponseWriter{}, strings.NewReader(stream))
	if err != nil {
		t.Fatalf("streamSSEWithCapture returned error: %v", err)
	}
	if usage == nil {
		t.Fatal("streamSSEWithCapture returned nil usage")
	}
	if usage.InputTokens != 12 || usage.CacheCreationInputTokens != 34 || usage.CacheReadInputTokens != 56 || usage.OutputTokens != 78 {
		t.Fatalf("unexpected usage: %+v", *usage)
	}
}
