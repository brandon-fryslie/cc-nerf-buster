package main

import (
	"strings"
	"testing"
)

func TestRequestCostCanonicalizesModelID(t *testing.T) {
	u := &Usage{InputTokens: 1}
	if _, ok := RequestCost(" claude-opus-4-7 ", u); !ok {
		t.Fatal("expected trimmed model id to price successfully")
	}
}

func TestExtractUsageFromSSEParsesUsage(t *testing.T) {
	stream := strings.Join([]string{
		"event: message_start",
		`data: {"message":{"usage":{"input_tokens":12,"cache_creation_input_tokens":34,"cache_read_input_tokens":56}}}`,
		"",
		"event: message_delta",
		`data: {"usage":{"output_tokens":78}}`,
		"",
	}, "\n")

	usage, err := extractUsageFromSSE(strings.NewReader(stream))
	if err != nil {
		t.Fatalf("extractUsageFromSSE returned error: %v", err)
	}
	if usage == nil {
		t.Fatal("extractUsageFromSSE returned nil usage")
	}
	if usage.InputTokens != 12 || usage.CacheCreationInputTokens != 34 || usage.CacheReadInputTokens != 56 || usage.OutputTokens != 78 {
		t.Fatalf("unexpected usage: %+v", *usage)
	}
}

func TestExtractUsageFromSSEHandlesMultilineEventAndEOF(t *testing.T) {
	stream := strings.Join([]string{
		"event: message_start",
		`data: {"message":{"usage":{"input_tokens":9,`,
		`data: "cache_creation_input_tokens":8,"cache_read_input_tokens":7}}}`,
		"",
		"event: message_delta",
		`data: {"usage":{"output_tokens":6}}`,
	}, "\n")

	usage, err := extractUsageFromSSE(strings.NewReader(stream))
	if err != nil {
		t.Fatalf("extractUsageFromSSE returned error: %v", err)
	}
	if usage == nil {
		t.Fatal("extractUsageFromSSE returned nil usage")
	}
	if usage.InputTokens != 9 || usage.CacheCreationInputTokens != 8 || usage.CacheReadInputTokens != 7 || usage.OutputTokens != 6 {
		t.Fatalf("unexpected usage: %+v", *usage)
	}
}

func TestExtractUsageFromSSEHandlesRealProbeStreamShape(t *testing.T) {
	stream := strings.Join([]string{
		"event: message_start",
		`data: {"type":"message_start","message":{"model":"claude-opus-4-7","id":"msg_01Jh7SDewyGoAtTkU72DkSVA","type":"message","role":"assistant","content":[],"stop_reason":null,"stop_sequence":null,"stop_details":null,"usage":{"input_tokens":5,"cache_creation_input_tokens":9165,"cache_read_input_tokens":6025,"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":9165},"output_tokens":1,"service_tier":"standard","inference_geo":"not_available"}} }`,
		"",
		"event: content_block_start",
		`data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}`,
		"",
		"event: content_block_delta",
		`data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"The notes describe normal service activity."}}`,
		"",
		"event: message_delta",
		`data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null,"stop_details":null},"usage":{"input_tokens":5,"cache_creation_input_tokens":9165,"cache_read_input_tokens":6025,"output_tokens":15,"iterations":[{"input_tokens":5,"output_tokens":15,"cache_read_input_tokens":6025,"cache_creation_input_tokens":9165,"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":9165},"type":"message"}]},"context_management":{"applied_edits":[]}}`,
		"",
		"event: message_stop",
		`data: {"type":"message_stop"}`,
		"",
	}, "\n")

	usage, err := extractUsageFromSSE(strings.NewReader(stream))
	if err != nil {
		t.Fatalf("extractUsageFromSSE returned error: %v", err)
	}
	if usage == nil {
		t.Fatal("extractUsageFromSSE returned nil usage")
	}
	if usage.InputTokens != 5 || usage.CacheCreationInputTokens != 9165 || usage.CacheReadInputTokens != 6025 || usage.OutputTokens != 15 {
		t.Fatalf("unexpected usage: %+v", *usage)
	}
}
