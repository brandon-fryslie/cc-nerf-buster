package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type harHeader struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type harPostData struct {
	MimeType string `json:"mimeType"`
	Text     string `json:"text"`
}

type harContent struct {
	Size     int    `json:"size"`
	MimeType string `json:"mimeType"`
	Text     string `json:"text"`
}

type harRequest struct {
	Method      string       `json:"method"`
	URL         string       `json:"url"`
	HTTPVersion string       `json:"httpVersion"`
	Headers     []harHeader  `json:"headers"`
	QueryString []harHeader  `json:"queryString"`
	PostData    *harPostData `json:"postData,omitempty"`
	HeadersSize int          `json:"headersSize"`
	BodySize    int          `json:"bodySize"`
}

type harResponse struct {
	Status      int         `json:"status"`
	StatusText  string      `json:"statusText"`
	HTTPVersion string      `json:"httpVersion"`
	Headers     []harHeader `json:"headers"`
	Content     harContent  `json:"content"`
	RedirectURL string      `json:"redirectURL"`
	HeadersSize int         `json:"headersSize"`
	BodySize    int         `json:"bodySize"`
}

type harTimings struct {
	Send    int64 `json:"send"`
	Wait    int64 `json:"wait"`
	Receive int64 `json:"receive"`
}

type harEntry struct {
	StartedDateTime string      `json:"startedDateTime"`
	Time            int64       `json:"time"`
	Request         harRequest  `json:"request"`
	Response        harResponse `json:"response"`
	Cache           struct{}    `json:"cache"`
	Timings         harTimings  `json:"timings"`
}

// sensitiveHeaders are redacted in HAR dumps to avoid leaking credentials.
var sensitiveHeaders = map[string]bool{
	"x-api-key":     true,
	"authorization": true,
	"cookie":        true,
}

func headersToHAR(h http.Header) []harHeader {
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

// writeHARDump writes a .har file for a failed request to debugDir.
// Always called after the response has been fully forwarded to the client.
func writeHARDump(debugDir string, start time.Time, durationMs int64, reqMethod, reqURL string, reqHeader http.Header, reqBody []byte, respStatus int, respHeader http.Header, respBody []byte) {
	if debugDir == "" {
		return
	}

	reqMIME := reqHeader.Get("Content-Type")
	if reqMIME == "" {
		reqMIME = "application/octet-stream"
	}
	respMIME := respHeader.Get("Content-Type")
	if respMIME == "" {
		respMIME = "application/octet-stream"
	}

	var postData *harPostData
	if len(reqBody) > 0 {
		postData = &harPostData{MimeType: reqMIME, Text: string(reqBody)}
	}

	entry := harEntry{
		StartedDateTime: start.UTC().Format(time.RFC3339Nano),
		Time:            durationMs,
		Request: harRequest{
			Method:      reqMethod,
			URL:         reqURL,
			HTTPVersion: "HTTP/1.1",
			Headers:     headersToHAR(reqHeader),
			QueryString: []harHeader{},
			PostData:    postData,
			HeadersSize: -1,
			BodySize:    len(reqBody),
		},
		Response: harResponse{
			Status:      respStatus,
			StatusText:  http.StatusText(respStatus),
			HTTPVersion: "HTTP/1.1",
			Headers:     headersToHAR(respHeader),
			Content:     harContent{Size: len(respBody), MimeType: respMIME, Text: string(respBody)},
			RedirectURL: "",
			HeadersSize: -1,
			BodySize:    len(respBody),
		},
		Cache:   struct{}{},
		Timings: harTimings{Send: 0, Wait: durationMs, Receive: 0},
	}

	type harLog struct {
		Log struct {
			Version string `json:"version"`
			Creator struct {
				Name    string `json:"name"`
				Version string `json:"version"`
			} `json:"creator"`
			Entries []harEntry `json:"entries"`
		} `json:"log"`
	}

	var h harLog
	h.Log.Version = "1.2"
	h.Log.Creator.Name = "cc-nerf-buster"
	h.Log.Creator.Version = "1.0"
	h.Log.Entries = []harEntry{entry}

	filename := fmt.Sprintf("debug_%s.har", start.UTC().Format("20060102T150405.000Z07"))
	path := filepath.Join(debugDir, filename)

	data, err := json.MarshalIndent(h, "", "  ")
	if err != nil {
		log.Printf("HAR marshal error: %v", err)
		return
	}
	if err := os.WriteFile(path, data, 0600); err != nil {
		log.Printf("HAR write error: %v", err)
	}
}
