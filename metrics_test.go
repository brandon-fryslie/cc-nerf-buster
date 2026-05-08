package main

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"testing"
)

// TestUpdateCapacityEstimate_LeadingBracketExcluded verifies the core
// invariant: the FIRST observed crossing in a window-run does NOT contribute
// to the running capacity estimate. The cost spent reaching it spans an
// unknown sub-percent slice (we don't know where in the integer percent we
// started) and including it would inflate the estimate by random partial-tick
// noise. Only crossings AFTER the first anchor produce clean per-tick deltas.
func TestUpdateCapacityEstimate_LeadingBracketExcluded(t *testing.T) {
	snap := &quotaSnapshot{}
	est := &capacityEstimator{}

	// Initial observation at util=0.05 with $0.50 of cost already accumulated
	// from prior activity. We don't know where in tick 5 we are.
	updated := updateCapacityEstimate(snap, est, 0.05, 0.50)
	if updated {
		t.Fatalf("first observation should not update estimator")
	}
	if est.TotalUtilDelta != 0 || est.TotalCostDelta != 0 {
		t.Fatalf("estimator dirty after first observation: %+v", est)
	}

	// More cost without a tick advance — still no update.
	updated = updateCapacityEstimate(snap, est, 0.05, 1.50)
	if updated || est.TotalUtilDelta != 0 {
		t.Fatalf("no-tick observation should not update estimator")
	}

	// First observed CROSSING: util ticks up by 0.01 and we've now spent $2.00.
	// This is the LEADING BRACKET — the cost includes whatever partial slice
	// of tick 5 we started in. It must NOT be accumulated.
	updated = updateCapacityEstimate(snap, est, 0.06, 2.00)
	if updated {
		t.Fatalf("first crossing must not accumulate (leading bracket); est=%+v", est)
	}
	if est.TotalUtilDelta != 0 || est.TotalCostDelta != 0 {
		t.Fatalf("estimator polluted by leading bracket: %+v", est)
	}
	if !snap.bracketed {
		t.Fatalf("snap should be bracketed after first crossing")
	}

	// SECOND observed crossing — this is the first clean per-tick measurement.
	// Cost rose by $1.80 over a 0.01 util advance, so capacity should be $180.
	updated = updateCapacityEstimate(snap, est, 0.07, 3.80)
	if !updated {
		t.Fatalf("second crossing should accumulate")
	}
	if math.Abs(est.TotalCostDelta-1.80) > 1e-9 {
		t.Fatalf("expected costDelta=1.80, got %v", est.TotalCostDelta)
	}
	if math.Abs(est.TotalUtilDelta-0.01) > 1e-9 {
		t.Fatalf("expected utilDelta=0.01, got %v", est.TotalUtilDelta)
	}
	if got := est.capacity(); math.Abs(got-180.0) > 1e-9 {
		t.Fatalf("expected capacity=180, got %v", got)
	}

	// Third crossing: another clean measurement at $1.90 / 0.01 = $190.
	// Running capacity should now be ($1.80 + $1.90) / 0.02 = $185.
	updated = updateCapacityEstimate(snap, est, 0.08, 5.70)
	if !updated {
		t.Fatalf("third crossing should accumulate")
	}
	if got := est.capacity(); math.Abs(got-185.0) > 1e-9 {
		t.Fatalf("expected capacity=185 after two clean ticks, got %v", got)
	}
}

// TestUpdateCapacityEstimate_RolloverResetsBracket verifies that a window
// rollover (util drops) discards the bracketed anchor. The first crossing
// AFTER the rollover is again a leading bracket and must be excluded.
func TestUpdateCapacityEstimate_RolloverResetsBracket(t *testing.T) {
	snap := &quotaSnapshot{}
	est := &capacityEstimator{}

	// Pre-rollover: anchor + one clean measurement.
	updateCapacityEstimate(snap, est, 0.50, 100.0) // initial observation
	updateCapacityEstimate(snap, est, 0.51, 102.0) // first crossing — leading bracket
	updateCapacityEstimate(snap, est, 0.52, 104.0) // second crossing — clean
	if math.Abs(est.TotalUtilDelta-0.01) > 1e-9 {
		t.Fatalf("expected one clean tick before rollover, got %v", est.TotalUtilDelta)
	}

	// Rollover: util drops back to 0 (window reset).
	updateCapacityEstimate(snap, est, 0.0, 200.0) // rollover-triggering observation
	if snap.bracketed {
		t.Fatalf("bracketed flag should be cleared by rollover")
	}

	// First post-rollover crossing — must NOT accumulate (it's a new leading bracket).
	deltaUtilBefore := est.TotalUtilDelta
	deltaCostBefore := est.TotalCostDelta
	updateCapacityEstimate(snap, est, 0.01, 250.0)
	if est.TotalUtilDelta != deltaUtilBefore || est.TotalCostDelta != deltaCostBefore {
		t.Fatalf("post-rollover first crossing leaked into estimator: before=%v/%v after=%v/%v",
			deltaUtilBefore, deltaCostBefore, est.TotalUtilDelta, est.TotalCostDelta)
	}

	// Next crossing — clean.
	updateCapacityEstimate(snap, est, 0.02, 252.0)
	if math.Abs(est.TotalUtilDelta-0.02) > 1e-9 {
		t.Fatalf("expected one more clean tick post-rollover, got total utilDelta=%v",
			est.TotalUtilDelta)
	}
}

// TestUpdateCapacityEstimate_NoCostNoTick verifies that a snapshot where util
// did not advance does not corrupt the estimator and does not update the cost
// anchor (so the next crossing's cost delta still spans from the prior anchor).
func TestUpdateCapacityEstimate_NoCostNoTick(t *testing.T) {
	snap := &quotaSnapshot{}
	est := &capacityEstimator{}

	updateCapacityEstimate(snap, est, 0.0, 0.0)  // initial
	updateCapacityEstimate(snap, est, 0.01, 1.0) // leading bracket
	prevSnapCost := snap.cost
	updateCapacityEstimate(snap, est, 0.01, 1.5) // no tick advance
	if snap.cost != prevSnapCost {
		t.Fatalf("snap.cost should not advance without a tick: %v vs %v", snap.cost, prevSnapCost)
	}
	updateCapacityEstimate(snap, est, 0.02, 2.0) // clean: $1.0 / 0.01 = $100
	if got := est.capacity(); math.Abs(got-100.0) > 1e-9 {
		t.Fatalf("expected $100/tick, got %v (cost between two crossings should anchor at $1.0, end at $2.0)", got)
	}
}

// TestLoadEstimates_DiscardsOldSchema verifies that pre-v2 quota_estimates.json
// files (which contain leading-bracket pollution) are discarded on load rather
// than carrying corrupted accumulator state into a fresh process.
func TestLoadEstimates_DiscardsOldSchema(t *testing.T) {
	dir := t.TempDir()
	v1 := []byte(`{"estimates": {"org/up": {"five_hour": {"total_cost_delta": 999, "total_util_delta": 1}, "seven_day": {"total_cost_delta": 999, "total_util_delta": 1}}}}`)
	if err := os.WriteFile(filepath.Join(dir, "quota_estimates.json"), v1, 0644); err != nil {
		t.Fatal(err)
	}
	m := NewMetrics(dir)
	gs, ok := m.gauges["org\x00up"]
	if ok && (gs.fiveHourEst.TotalUtilDelta != 0 || gs.fiveHourEst.TotalCostDelta != 0) {
		t.Fatalf("pre-v2 data was loaded, expected discard: %+v", gs.fiveHourEst)
	}
}

// TestPersistEstimates_WritesCurrentSchema verifies persisted files carry
// the schema version and round-trip cleanly through load.
func TestPersistEstimates_WritesCurrentSchema(t *testing.T) {
	dir := t.TempDir()
	m := NewMetrics(dir)
	five := &capacityEstimator{TotalCostDelta: 1.5, TotalUtilDelta: 0.01}
	seven := &capacityEstimator{TotalCostDelta: 7.5, TotalUtilDelta: 0.01}
	m.persistEstimates("org", "up", five, seven)

	data, err := os.ReadFile(filepath.Join(dir, "quota_estimates.json"))
	if err != nil {
		t.Fatal(err)
	}
	var pe persistedEstimates
	if err := json.Unmarshal(data, &pe); err != nil {
		t.Fatal(err)
	}
	if pe.Version != quotaEstimatesSchemaVersion {
		t.Fatalf("expected schema version %d, got %d", quotaEstimatesSchemaVersion, pe.Version)
	}

	// New process loading this file must accept it.
	m2 := NewMetrics(dir)
	gs, ok := m2.gauges["org\x00up"]
	if !ok {
		t.Fatalf("entry not loaded")
	}
	if math.Abs(gs.fiveHourEst.TotalCostDelta-1.5) > 1e-9 {
		t.Fatalf("five_hour cost not preserved: %+v", gs.fiveHourEst)
	}
}
