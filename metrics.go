package main

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
)

// Metrics holds all Prometheus-compatible metrics for the proxy.
type Metrics struct {
	// Counters keyed by "model\x00status\x00org\x00upstream"
	mu       sync.RWMutex
	counters map[string]*counterSet

	// Gauges keyed by "org\x00upstream"
	gauges map[string]*gaugeSet

	// Log errors counter
	logErrors atomic.Int64

	// Unknown-model error counters (all token types lumped together)
	noModelInputTokens  atomic.Int64
	noModelOutputTokens atomic.Int64

	// Data directory for persisting estimates
	dataDir string
}

type counterSet struct {
	requests         atomic.Int64
	inputTokens      atomic.Int64
	outputTokens     atomic.Int64
	cacheCreateInput atomic.Int64
	cacheReadInput   atomic.Int64
}

type gaugeSet struct {
	fiveHourUtil atomic.Int64 // stored as float64 bits
	sevenDayUtil atomic.Int64
	overageUtil  atomic.Int64
	fallbackPct  atomic.Int64

	// Quota capacity estimation
	costMu         sync.Mutex // protects cost/snapshot/estimator fields below
	cumulativeCost float64    // running weighted cost since proxy boot

	fiveHourSnap quotaSnapshot     // last observation for 5h window
	sevenDaySnap quotaSnapshot     // last observation for 7d window
	fiveHourEst  capacityEstimator // running estimate for 5h
	sevenDayEst  capacityEstimator // running estimate for 7d

}

// quotaSnapshot records utilization and cumulative cost at a point in time.
type quotaSnapshot struct {
	util float64
	cost float64
	set  bool // false until first observation
}

// capacityEstimator accumulates cost and utilization deltas across all ticks.
// capacity = totalCostDelta / totalUtilDelta — improves with every observation.
type capacityEstimator struct {
	TotalCostDelta float64 `json:"total_cost_delta"`
	TotalUtilDelta float64 `json:"total_util_delta"`
}

func (e *capacityEstimator) capacity() float64 {
	if e.TotalUtilDelta <= 0 {
		return 0
	}
	return e.TotalCostDelta / e.TotalUtilDelta
}

func NewMetrics(dataDir string) *Metrics {
	m := &Metrics{
		counters: make(map[string]*counterSet),
		gauges:   make(map[string]*gaugeSet),
		dataDir:  dataDir,
	}
	m.loadEstimates()
	return m
}

func (m *Metrics) getCounters(model, status, org, upstream string) *counterSet {
	key := model + "\x00" + status + "\x00" + org + "\x00" + upstream
	m.mu.RLock()
	cs, ok := m.counters[key]
	m.mu.RUnlock()
	if ok {
		return cs
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	if cs, ok = m.counters[key]; ok {
		return cs
	}
	cs = &counterSet{}
	m.counters[key] = cs
	return cs
}

func (m *Metrics) getGauges(org, upstream string) *gaugeSet {
	key := org + "\x00" + upstream
	m.mu.RLock()
	gs, ok := m.gauges[key]
	m.mu.RUnlock()
	if ok {
		return gs
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	if gs, ok = m.gauges[key]; ok {
		return gs
	}
	gs = &gaugeSet{}
	m.gauges[key] = gs
	return gs
}

// Record updates all metrics from an APIEvent.
func (m *Metrics) Record(event *APIEvent) {
	model := "unknown"
	if event.Model != nil {
		model = *event.Model
	}
	org := ""
	if event.Meta != nil {
		org = event.Meta.OrganizationID
	}
	status := fmt.Sprintf("%d", event.Status)

	cs := m.getCounters(model, status, org, event.Upstream)
	cs.requests.Add(1)
	if event.Usage != nil {
		cs.inputTokens.Add(event.Usage.InputTokens)
		cs.outputTokens.Add(event.Usage.OutputTokens)
		cs.cacheCreateInput.Add(event.Usage.CacheCreationInputTokens)
		cs.cacheReadInput.Add(event.Usage.CacheReadInputTokens)
	}

	// Compute weighted cost for quota estimation
	gs := m.getGauges(org, event.Upstream)
	if event.Usage != nil {
		cost, ok := RequestCost(model, event.Usage)
		if ok {
			gs.costMu.Lock()
			gs.cumulativeCost += cost
			gs.costMu.Unlock()
		} else {
			throttledLog("unpriced_model", "model %q not in pricing table", model)
			m.noModelInputTokens.Add(event.Usage.InputTokens + event.Usage.CacheCreationInputTokens + event.Usage.CacheReadInputTokens)
			m.noModelOutputTokens.Add(event.Usage.OutputTokens)
		}
	}

	// Update gauges and quota snapshots
	if event.Quota != nil {
		if event.Quota.FiveHourUtilization != nil {
			gs.fiveHourUtil.Store(int64(math.Float64bits(*event.Quota.FiveHourUtilization)))
		}
		if event.Quota.SevenDayUtilization != nil {
			gs.sevenDayUtil.Store(int64(math.Float64bits(*event.Quota.SevenDayUtilization)))
		}
		if event.Quota.OverageUtilization != nil {
			gs.overageUtil.Store(int64(math.Float64bits(*event.Quota.OverageUtilization)))
		}
		if event.Quota.FallbackPercentage != nil {
			gs.fallbackPct.Store(int64(math.Float64bits(*event.Quota.FallbackPercentage)))
		}

		// Update quota capacity snapshots (delta method)
		gs.costMu.Lock()
		currentCost := gs.cumulativeCost

		changed := false
		if event.Quota.FiveHourUtilization != nil {
			changed = updateCapacityEstimate(&gs.fiveHourSnap, &gs.fiveHourEst, *event.Quota.FiveHourUtilization, currentCost) || changed
		}
		if event.Quota.SevenDayUtilization != nil {
			changed = updateCapacityEstimate(&gs.sevenDaySnap, &gs.sevenDayEst, *event.Quota.SevenDayUtilization, currentCost) || changed
		}
		if changed {
			m.persistEstimates(org, event.Upstream, &gs.fiveHourEst, &gs.sevenDayEst)
		}

		gs.costMu.Unlock()
	}
}

// updateCapacityEstimate accumulates cost/utilization deltas for capacity estimation.
// Called with costMu held. Returns true if the estimator was updated.
func updateCapacityEstimate(snap *quotaSnapshot, est *capacityEstimator, util float64, currentCost float64) bool {
	// Utilization reset (window rolled over) — start fresh snapshot
	if snap.set && util < snap.util {
		snap.set = false
	}

	// First observation — record baseline
	if !snap.set {
		snap.util = util
		snap.cost = currentCost
		snap.set = true
		return false
	}

	// No change in utilization — nothing to accumulate
	deltaUtil := util - snap.util
	deltaCost := currentCost - snap.cost

	if deltaUtil <= 0 || deltaCost <= 0 {
		return false
	}

	// Accumulate into running totals
	est.TotalCostDelta += deltaCost
	est.TotalUtilDelta += deltaUtil

	// Update snapshot for next delta
	snap.util = util
	snap.cost = currentCost
	return true
}

// persistedEstimates is the on-disk format for quota capacity data.
type persistedEstimates struct {
	// Keyed by "org\x00upstream"
	Estimates map[string]*persistedEstimateEntry `json:"estimates"`
}

type persistedEstimateEntry struct {
	FiveHour capacityEstimator `json:"five_hour"`
	SevenDay capacityEstimator `json:"seven_day"`
}

func (m *Metrics) estimatesPath() string {
	return filepath.Join(m.dataDir, "quota_estimates.json")
}

// loadEstimates restores accumulated capacity estimates from disk.
func (m *Metrics) loadEstimates() {
	data, err := os.ReadFile(m.estimatesPath())
	if err != nil {
		return // file doesn't exist yet — normal on first run
	}

	var pe persistedEstimates
	if err := json.Unmarshal(data, &pe); err != nil {
		log.Printf("warning: failed to parse %s: %v", m.estimatesPath(), err)
		return
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	for key, entry := range pe.Estimates {
		// Persisted key uses "/" separator; internal key uses \x00
		parts := strings.SplitN(key, "/", 2)
		if len(parts) != 2 {
			continue
		}
		internalKey := parts[0] + "\x00" + parts[1]
		gs, ok := m.gauges[internalKey]
		if !ok {
			gs = &gaugeSet{}
			m.gauges[internalKey] = gs
		}
		gs.fiveHourEst = entry.FiveHour
		gs.sevenDayEst = entry.SevenDay
	}

	log.Printf("loaded quota estimates from %s", m.estimatesPath())
}

// persistEstimates writes accumulated capacity estimates to disk.
// Called with gs.costMu held by the caller.
func (m *Metrics) persistEstimates(org, upstream string, fiveHour, sevenDay *capacityEstimator) {
	// Read existing file to preserve other org/upstream entries
	pe := &persistedEstimates{Estimates: make(map[string]*persistedEstimateEntry)}

	if data, err := os.ReadFile(m.estimatesPath()); err == nil {
		json.Unmarshal(data, pe) // best effort
		if pe.Estimates == nil {
			pe.Estimates = make(map[string]*persistedEstimateEntry)
		}
	}

	key := org + "/" + upstream
	pe.Estimates[key] = &persistedEstimateEntry{
		FiveHour: *fiveHour,
		SevenDay: *sevenDay,
	}

	data, err := json.MarshalIndent(pe, "", "  ")
	if err != nil {
		log.Printf("warning: failed to marshal estimates: %v", err)
		return
	}

	if err := os.WriteFile(m.estimatesPath(), data, 0644); err != nil {
		log.Printf("warning: failed to write %s: %v", m.estimatesPath(), err)
	}
}

func (m *Metrics) IncLogErrors() {
	m.logErrors.Add(1)
}

// ServeHTTP writes Prometheus text exposition format.
func (m *Metrics) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")

	var b strings.Builder

	m.mu.RLock()

	// Sort counter keys for stable output
	counterKeys := make([]string, 0, len(m.counters))
	for k := range m.counters {
		counterKeys = append(counterKeys, k)
	}
	sort.Strings(counterKeys)

	// Counters
	writeCounterHelp(&b, "ccnb_requests_total", "Total API requests observed", "counter")
	for _, key := range counterKeys {
		cs := m.counters[key]
		labels := parseCounterKey(key)
		b.WriteString(fmt.Sprintf("ccnb_requests_total{%s} %d\n", labels, cs.requests.Load()))
	}

	writeCounterHelp(&b, "ccnb_input_tokens_total", "Total input tokens observed", "counter")
	for _, key := range counterKeys {
		cs := m.counters[key]
		labels := parseCounterKey(key)
		b.WriteString(fmt.Sprintf("ccnb_input_tokens_total{%s} %d\n", labels, cs.inputTokens.Load()))
	}

	writeCounterHelp(&b, "ccnb_output_tokens_total", "Total output tokens observed", "counter")
	for _, key := range counterKeys {
		cs := m.counters[key]
		labels := parseCounterKey(key)
		b.WriteString(fmt.Sprintf("ccnb_output_tokens_total{%s} %d\n", labels, cs.outputTokens.Load()))
	}

	writeCounterHelp(&b, "ccnb_cache_creation_input_tokens_total", "Total cache creation input tokens observed", "counter")
	for _, key := range counterKeys {
		cs := m.counters[key]
		labels := parseCounterKey(key)
		b.WriteString(fmt.Sprintf("ccnb_cache_creation_input_tokens_total{%s} %d\n", labels, cs.cacheCreateInput.Load()))
	}

	writeCounterHelp(&b, "ccnb_cache_read_input_tokens_total", "Total cache read input tokens observed", "counter")
	for _, key := range counterKeys {
		cs := m.counters[key]
		labels := parseCounterKey(key)
		b.WriteString(fmt.Sprintf("ccnb_cache_read_input_tokens_total{%s} %d\n", labels, cs.cacheReadInput.Load()))
	}

	// Log errors
	b.WriteString("# HELP ccnb_log_errors_total Total JSONL log write errors\n")
	b.WriteString("# TYPE ccnb_log_errors_total counter\n")
	b.WriteString(fmt.Sprintf("ccnb_log_errors_total %d\n", m.logErrors.Load()))

	writeCounterHelp(&b, "ccnb_no_model_error_input_tokens_total", "Input tokens from requests with unknown model (proxy bug)", "counter")
	b.WriteString(fmt.Sprintf("ccnb_no_model_error_input_tokens_total %d\n", m.noModelInputTokens.Load()))

	writeCounterHelp(&b, "ccnb_no_model_error_output_tokens_total", "Output tokens from requests with unknown model (proxy bug)", "counter")
	b.WriteString(fmt.Sprintf("ccnb_no_model_error_output_tokens_total %d\n", m.noModelOutputTokens.Load()))

	// Gauges
	gaugeKeys := make([]string, 0, len(m.gauges))
	for k := range m.gauges {
		gaugeKeys = append(gaugeKeys, k)
	}
	sort.Strings(gaugeKeys)

	writeGaugeHelp(&b, "ccnb_quota_5h_utilization", "5-hour quota utilization (0.0-1.0)")
	for _, key := range gaugeKeys {
		gs := m.gauges[key]
		labels := parseGaugeKey(key)
		b.WriteString(fmt.Sprintf("ccnb_quota_5h_utilization{%s} %s\n", labels, loadFloat(&gs.fiveHourUtil)))
	}

	writeGaugeHelp(&b, "ccnb_quota_7d_utilization", "7-day quota utilization (0.0-1.0)")
	for _, key := range gaugeKeys {
		gs := m.gauges[key]
		labels := parseGaugeKey(key)
		b.WriteString(fmt.Sprintf("ccnb_quota_7d_utilization{%s} %s\n", labels, loadFloat(&gs.sevenDayUtil)))
	}

	writeGaugeHelp(&b, "ccnb_quota_overage_utilization", "Overage quota utilization (0.0-1.0)")
	for _, key := range gaugeKeys {
		gs := m.gauges[key]
		labels := parseGaugeKey(key)
		b.WriteString(fmt.Sprintf("ccnb_quota_overage_utilization{%s} %s\n", labels, loadFloat(&gs.overageUtil)))
	}

	writeGaugeHelp(&b, "ccnb_quota_fallback_percentage", "Fallback percentage (0.0-1.0)")
	for _, key := range gaugeKeys {
		gs := m.gauges[key]
		labels := parseGaugeKey(key)
		b.WriteString(fmt.Sprintf("ccnb_quota_fallback_percentage{%s} %s\n", labels, loadFloat(&gs.fallbackPct)))
	}

	// Cost accumulator
	writeCounterHelp(&b, "ccnb_cost_total", "Weighted cost accumulated through proxy (API-dollar-equivalent)", "counter")
	for _, key := range gaugeKeys {
		gs := m.gauges[key]
		labels := parseGaugeKey(key)
		gs.costMu.Lock()
		cost := gs.cumulativeCost
		gs.costMu.Unlock()
		b.WriteString(fmt.Sprintf("ccnb_cost_total{%s} %g\n", labels, cost))
	}

	// Quota capacity estimates in USD (delta method, accumulated over time).
	// USD is the canonical unit — clients can project to any model's tokens via
	// tokens = usd × 1e6 / price_per_MTok (e.g. opus output = usd × 40000).
	writeGaugeHelp(&b, "ccnb_quota_5h_estimated_capacity_usd", "Estimated 5h quota capacity in USD")
	for _, key := range gaugeKeys {
		gs := m.gauges[key]
		labels := parseGaugeKey(key)
		gs.costMu.Lock()
		cap5h := gs.fiveHourEst.capacity()
		gs.costMu.Unlock()
		b.WriteString(fmt.Sprintf("ccnb_quota_5h_estimated_capacity_usd{%s} %g\n", labels, cap5h))
	}

	writeGaugeHelp(&b, "ccnb_quota_7d_estimated_capacity_usd", "Estimated 7d quota capacity in USD")
	for _, key := range gaugeKeys {
		gs := m.gauges[key]
		labels := parseGaugeKey(key)
		gs.costMu.Lock()
		cap7d := gs.sevenDayEst.capacity()
		gs.costMu.Unlock()
		b.WriteString(fmt.Sprintf("ccnb_quota_7d_estimated_capacity_usd{%s} %g\n", labels, cap7d))
	}

	m.mu.RUnlock()

	fmt.Fprint(w, b.String())
}

func writeCounterHelp(b *strings.Builder, name, help, typ string) {
	b.WriteString(fmt.Sprintf("# HELP %s %s\n", name, help))
	b.WriteString(fmt.Sprintf("# TYPE %s %s\n", name, typ))
}

func writeGaugeHelp(b *strings.Builder, name, help string) {
	b.WriteString(fmt.Sprintf("# HELP %s %s\n", name, help))
	b.WriteString(fmt.Sprintf("# TYPE %s gauge\n", name))
}

// parseCounterKey splits "model\x00status\x00org\x00upstream" into labels string.
func parseCounterKey(key string) string {
	parts := strings.Split(key, "\x00")
	return fmt.Sprintf(`model="%s",status="%s",org="%s",upstream="%s"`, parts[0], parts[1], parts[2], parts[3])
}

// parseGaugeKey splits "org\x00upstream" into labels.
func parseGaugeKey(key string) string {
	parts := strings.Split(key, "\x00")
	return fmt.Sprintf(`org="%s",upstream="%s"`, parts[0], parts[1])
}

func loadFloat(a *atomic.Int64) string {
	bits := uint64(a.Load())
	f := math.Float64frombits(bits)
	return fmt.Sprintf("%g", f)
}
