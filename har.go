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

// HAR (HTTP Archive 1.2) writer — single JSON document containing one entry
// per captured request. Spec: http://www.softwareishard.com/blog/har-12-spec/
//
// Streaming write strategy: we emit a fixed preamble at open, append entries
// with comma separators as they arrive, and emit the closing footer on Close.
// If the proxy crashes mid-run the file is missing its trailing `]}}` and is
// not valid JSON; recoverHAR appends it. The alternative — buffering all
// entries in memory until close — would lose everything on crash.

type harEntry struct {
	StartedDateTime string      `json:"startedDateTime"`
	Time            int64       `json:"time"`
	Request         harRequest  `json:"request"`
	Response        harResponse `json:"response"`
	Cache           struct{}    `json:"cache"`
	Timings         harTimings  `json:"timings"`
	// Custom fields are allowed in HAR; the spec reserves underscore-prefixed names.
	XErrors []string `json:"_errors,omitempty"`
	XModel  *string  `json:"_model,omitempty"`
}

type harRequest struct {
	Method      string       `json:"method"`
	URL         string       `json:"url"`
	HTTPVersion string       `json:"httpVersion"`
	Cookies     []struct{}   `json:"cookies"`
	Headers     []harHeader  `json:"headers"`
	QueryString []struct{}   `json:"queryString"`
	PostData    *harPostData `json:"postData,omitempty"`
	HeadersSize int64        `json:"headersSize"`
	BodySize    int64        `json:"bodySize"`
}

type harResponse struct {
	Status      int         `json:"status"`
	StatusText  string      `json:"statusText"`
	HTTPVersion string      `json:"httpVersion"`
	Cookies     []struct{}  `json:"cookies"`
	Headers     []harHeader `json:"headers"`
	Content     harContent  `json:"content"`
	RedirectURL string      `json:"redirectURL"`
	HeadersSize int64       `json:"headersSize"`
	BodySize    int64       `json:"bodySize"`
}

type harHeader struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type harPostData struct {
	MimeType string `json:"mimeType"`
	Text     string `json:"text"`
}

type harContent struct {
	Size     int64  `json:"size"`
	MimeType string `json:"mimeType"`
	Text     string `json:"text"`
}

type harTimings struct {
	Send    int64 `json:"send"`
	Wait    int64 `json:"wait"`
	Receive int64 `json:"receive"`
}

// HARCapture is the input shape callers build. The writer translates it to
// the on-disk HAR entry, keeping HAR's verbosity out of the call site.
type HARCapture struct {
	Start       time.Time
	Duration    time.Duration
	Errors      []string
	Model       *string
	ReqMethod   string
	ReqURL      string
	ReqHeaders  http.Header
	ReqBody     []byte
	RespStatus  int
	RespHeaders http.Header
	RespBody    []byte
}

type HARWriter struct {
	mu      sync.Mutex
	file    *os.File
	first   bool // true until the first entry is written
	closed  bool
}

func NewHARWriter(path string) (*HARWriter, error) {
	f, err := os.OpenFile(path, os.O_TRUNC|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return nil, err
	}
	preamble := `{"log":{"version":"1.2","creator":{"name":"cc-nerf-buster","version":"0"},"entries":[`
	if _, err := f.WriteString(preamble); err != nil {
		f.Close()
		return nil, err
	}
	return &HARWriter{file: f, first: true}, nil
}

func (w *HARWriter) Write(c *HARCapture) {
	if w == nil {
		return
	}
	entry := buildEntry(c)
	data, err := json.Marshal(entry)
	if err != nil {
		log.Printf("har marshal error: %v", err)
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return
	}
	prefix := ","
	if w.first {
		prefix = ""
		w.first = false
	}
	if _, err := w.file.WriteString(prefix); err != nil {
		log.Printf("har write error: %v", err)
		return
	}
	if _, err := w.file.Write(data); err != nil {
		log.Printf("har write error: %v", err)
	}
}

func (w *HARWriter) Close() error {
	if w == nil {
		return nil
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return nil
	}
	w.closed = true
	if _, err := w.file.WriteString("]}}"); err != nil {
		w.file.Close()
		return err
	}
	return w.file.Close()
}

func buildEntry(c *HARCapture) harEntry {
	reqMime := c.ReqHeaders.Get("Content-Type")
	respMime := c.RespHeaders.Get("Content-Type")
	statusText := http.StatusText(c.RespStatus)

	var postData *harPostData
	if len(c.ReqBody) > 0 {
		postData = &harPostData{MimeType: reqMime, Text: string(c.ReqBody)}
	}

	return harEntry{
		StartedDateTime: c.Start.UTC().Format(time.RFC3339Nano),
		Time:            c.Duration.Milliseconds(),
		Request: harRequest{
			Method:      c.ReqMethod,
			URL:         c.ReqURL,
			HTTPVersion: "HTTP/1.1",
			Cookies:     []struct{}{},
			Headers:     harHeaders(c.ReqHeaders),
			QueryString: []struct{}{},
			PostData:    postData,
			HeadersSize: -1,
			BodySize:    int64(len(c.ReqBody)),
		},
		Response: harResponse{
			Status:      c.RespStatus,
			StatusText:  statusText,
			HTTPVersion: "HTTP/1.1",
			Cookies:     []struct{}{},
			Headers:     harHeaders(c.RespHeaders),
			Content: harContent{
				Size:     int64(len(c.RespBody)),
				MimeType: respMime,
				Text:     string(c.RespBody),
			},
			RedirectURL: "",
			HeadersSize: -1,
			BodySize:    int64(len(c.RespBody)),
		},
		Cache: struct{}{},
		Timings: harTimings{
			Send:    0,
			Wait:    0,
			Receive: c.Duration.Milliseconds(),
		},
		XErrors: c.Errors,
		XModel:  c.Model,
	}
}

// harHeaders flattens http.Header into HAR's name/value array form, redacting
// credential headers the same way debug.go does so a HAR file can be shared
// without leaking API keys.
func harHeaders(h http.Header) []harHeader {
	if h == nil {
		return []harHeader{}
	}
	out := make([]harHeader, 0, len(h))
	for name, vals := range h {
		val := strings.Join(vals, ", ")
		if sensitiveHeaders[strings.ToLower(name)] {
			val = "[REDACTED]"
		}
		out = append(out, harHeader{Name: name, Value: val})
	}
	return out
}

