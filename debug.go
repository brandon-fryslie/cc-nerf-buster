package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// DebugEvent captures a full request/response exchange that produced one or more errors.
// The Errors field is the canonical "why was this written" — all other fields are context.
type DebugEvent struct {
	TS          time.Time         `json:"ts"`
	Errors      []string          `json:"errors"`
	Model       *string           `json:"model,omitempty"`
	Upstream    string            `json:"upstream"`
	DurationMs  int64             `json:"duration_ms"`
	ReqMethod   string            `json:"req_method"`
	ReqURL      string            `json:"req_url"`
	ReqHeaders  map[string]string `json:"req_headers"`
	ReqBody     string            `json:"req_body"`
	RespStatus  int               `json:"resp_status"`
	RespHeaders map[string]string `json:"resp_headers"`
	RespBody    string            `json:"resp_body"`
}

// DebugWriter is an append-only JSONL writer for DebugEvents.
// Writes are unbuffered since they only happen on errors.
type DebugWriter struct {
	mu   sync.Mutex
	file *os.File
}

func NewDebugWriter(path string) (*DebugWriter, error) {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return nil, err
	}
	return &DebugWriter{file: f}, nil
}

func (w *DebugWriter) Write(event *DebugEvent) {
	data, err := json.Marshal(event)
	if err != nil {
		log.Printf("debug marshal error: %v", err)
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, err := w.file.Write(append(data, '\n')); err != nil {
		log.Printf("debug write error: %v", err)
	}
}

func (w *DebugWriter) Close() error {
	return w.file.Close()
}

// sensitiveHeaders are redacted in debug dumps to avoid logging credentials.
var sensitiveHeaders = map[string]bool{
	"x-api-key":     true,
	"authorization": true,
	"cookie":        true,
}

// flattenHeaders collapses http.Header into a plain map, joining multi-value headers
// and redacting credential headers.
func flattenHeaders(h http.Header) map[string]string {
	out := make(map[string]string, len(h))
	for name, vals := range h {
		val := strings.Join(vals, ", ")
		if sensitiveHeaders[strings.ToLower(name)] {
			val = "[REDACTED]"
		}
		out[name] = val
	}
	return out
}
