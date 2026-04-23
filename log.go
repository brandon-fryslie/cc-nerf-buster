package main

import (
	"bufio"
	"encoding/json"
	"os"
	"sync"
	"time"
)

// JSONLWriter is a buffered, append-only JSONL file writer.
type JSONLWriter struct {
	mu      sync.Mutex
	file    *os.File
	buf     *bufio.Writer
	metrics *Metrics
	done    chan struct{}
}

// NewJSONLWriter opens the file for append and starts a flush ticker.
func NewJSONLWriter(path string, metrics *Metrics) (*JSONLWriter, error) {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return nil, err
	}

	w := &JSONLWriter{
		file:    f,
		buf:     bufio.NewWriterSize(f, 4096),
		metrics: metrics,
		done:    make(chan struct{}),
	}

	go w.flushLoop()
	return w, nil
}

func (w *JSONLWriter) flushLoop() {
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			w.mu.Lock()
			w.buf.Flush()
			w.mu.Unlock()
		case <-w.done:
			return
		}
	}
}

// Write serializes an APIEvent as a single JSONL line.
func (w *JSONLWriter) Write(event *APIEvent) {
	data, err := json.Marshal(event)
	if err != nil {
		w.metrics.IncLogErrors()
		throttledLog("jsonl_marshal", "failed to marshal event: %v", err)
		return
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	data = append(data, '\n')
	if _, err := w.buf.Write(data); err != nil {
		w.metrics.IncLogErrors()
		throttledLog("jsonl_write", "failed to write event: %v", err)
		return
	}

	if w.buf.Buffered() >= 4096 {
		w.buf.Flush()
	}
}

// Close flushes and closes the underlying file.
func (w *JSONLWriter) Close() error {
	close(w.done)
	w.mu.Lock()
	defer w.mu.Unlock()
	w.buf.Flush()
	return w.file.Close()
}
