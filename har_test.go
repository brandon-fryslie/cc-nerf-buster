package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestHARWriterProducesValidHARDocument(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "traffic.har")

	w, err := NewHARWriter(path)
	if err != nil {
		t.Fatalf("NewHARWriter: %v", err)
	}

	model := "claude-opus-4-7"
	reqHeaders := http.Header{}
	reqHeaders.Set("Content-Type", "application/json")
	reqHeaders.Set("X-Api-Key", "secret-should-be-redacted")
	respHeaders := http.Header{}
	respHeaders.Set("Content-Type", "text/event-stream")

	w.Write(&HARCapture{
		Start:       time.Date(2026, 5, 11, 12, 51, 28, 0, time.UTC),
		Duration:    150 * time.Millisecond,
		Model:       &model,
		ReqMethod:   "POST",
		ReqURL:      "https://api.anthropic.com/v1/messages",
		ReqHeaders:  reqHeaders,
		ReqBody:     []byte(`{"model":"claude-opus-4-7"}`),
		RespStatus:  200,
		RespHeaders: respHeaders,
		RespBody:    []byte("event: message_start\ndata: {}\n\n"),
	})
	w.Write(&HARCapture{
		Start:       time.Date(2026, 5, 11, 12, 51, 29, 0, time.UTC),
		Duration:    80 * time.Millisecond,
		ReqMethod:   "POST",
		ReqURL:      "https://api.anthropic.com/v1/messages",
		RespStatus:  429,
		RespBody:    []byte(`{"type":"error"}`),
		Errors:      []string{"quota_headers_missing"},
	})

	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}

	// Must round-trip as valid JSON in HAR shape.
	var doc struct {
		Log struct {
			Version string `json:"version"`
			Creator struct {
				Name string `json:"name"`
			} `json:"creator"`
			Entries []struct {
				StartedDateTime string `json:"startedDateTime"`
				Request         struct {
					Method  string `json:"method"`
					URL     string `json:"url"`
					Headers []struct {
						Name  string `json:"name"`
						Value string `json:"value"`
					} `json:"headers"`
				} `json:"request"`
				Response struct {
					Status int `json:"status"`
				} `json:"response"`
			} `json:"entries"`
		} `json:"log"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatalf("HAR is not valid JSON: %v\nbody:\n%s", err, string(data))
	}
	if doc.Log.Version != "1.2" {
		t.Errorf("version: got %q want %q", doc.Log.Version, "1.2")
	}
	if doc.Log.Creator.Name != "cc-nerf-buster" {
		t.Errorf("creator.name: got %q", doc.Log.Creator.Name)
	}
	if len(doc.Log.Entries) != 2 {
		t.Fatalf("entries: got %d want 2", len(doc.Log.Entries))
	}
	if doc.Log.Entries[0].Response.Status != 200 {
		t.Errorf("entry[0] status: got %d want 200", doc.Log.Entries[0].Response.Status)
	}
	if doc.Log.Entries[1].Response.Status != 429 {
		t.Errorf("entry[1] status: got %d want 429", doc.Log.Entries[1].Response.Status)
	}

	// Credential header must be redacted.
	foundAPIKey := false
	for _, h := range doc.Log.Entries[0].Request.Headers {
		if h.Name == "X-Api-Key" {
			foundAPIKey = true
			if h.Value != "[REDACTED]" {
				t.Errorf("X-Api-Key not redacted: got %q", h.Value)
			}
		}
	}
	if !foundAPIKey {
		t.Errorf("X-Api-Key header missing from HAR entry")
	}
}
