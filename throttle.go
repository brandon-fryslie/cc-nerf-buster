package main

import (
	"fmt"
	"log"
	"sync"
	"sync/atomic"
	"time"
)

// throttledLog logs a message at most once per 60 seconds per category.
// Suppressed messages are counted and reported in the next allowed log.
var (
	throttleMu    sync.Mutex
	throttleState = make(map[string]*throttleEntry)
)

type throttleEntry struct {
	lastLog    time.Time
	suppressed atomic.Int64
}

func throttledLog(category string, format string, args ...any) {
	throttleMu.Lock()
	entry, ok := throttleState[category]
	if !ok {
		entry = &throttleEntry{}
		throttleState[category] = entry
	}
	throttleMu.Unlock()

	now := time.Now()
	if ok && now.Sub(entry.lastLog) < 60*time.Second {
		entry.suppressed.Add(1)
		return
	}

	suppressed := entry.suppressed.Swap(0)
	entry.lastLog = now

	msg := fmt.Sprintf(format, args...)
	if suppressed > 0 {
		log.Printf("[%s] %s (%d occurrences suppressed)", category, msg, suppressed)
	} else {
		log.Printf("[%s] %s", category, msg)
	}
}
