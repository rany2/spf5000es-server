package main

import (
	"reflect"
	"testing"
	"time"
)

type fakeClock struct {
	now       time.Time
	monotonic time.Duration
}

func (c *fakeClock) Now() time.Time              { return c.now }
func (c *fakeClock) MonotonicNow() time.Duration { return c.monotonic }
func (c *fakeClock) Advance(d time.Duration) {
	c.now = c.now.Add(d)
	c.monotonic += d
}

func TestSchedulerPriorityAndPeriodicReschedule(t *testing.T) {
	c := &fakeClock{now: time.Unix(100, 0)}
	s := newScheduler(c.Now)
	runs := []string{}
	must(s.Register("late", func() { runs = append(runs, "late") }, 10*time.Second, 50))
	must(s.Register("early", func() { runs = append(runs, "early") }, 0, 10))
	must(s.Schedule("late", 0, true))
	must(s.Schedule("early", 0, true))
	s.RunPending()
	if !reflect.DeepEqual(runs, []string{"early", "late"}) {
		t.Fatalf("run order = %v", runs)
	}
	if d, ok := s.NextTimeout(); !ok || d != 10*time.Second {
		t.Fatalf("next timeout = %v, %v", d, ok)
	}
}

func TestScheduleWithoutReplaceKeepsDeadline(t *testing.T) {
	c := &fakeClock{now: time.Unix(0, 0)}
	s := newScheduler(c.Now)
	must(s.Register("x", func() {}, 0, 1))
	must(s.Schedule("x", time.Second, true))
	must(s.Schedule("x", time.Minute, false))
	d, _ := s.NextTimeout()
	if d != time.Second {
		t.Fatalf("deadline moved: %v", d)
	}
}

func TestRegisterWindows(t *testing.T) {
	if len(inputRegisters) != 61 || len(holdingRegisters) != 84 {
		t.Fatalf("register table sizes = %d input, %d holding", len(inputRegisters), len(holdingRegisters))
	}
	wantIn := []registerWindow{{0, 45}, {45, 44}}
	wantHolding := []registerWindow{{0, 45}, {45, 45}, {90, 18}}
	if !reflect.DeepEqual(inputRegisterWindows, wantIn) {
		t.Fatalf("input windows = %#v", inputRegisterWindows)
	}
	if !reflect.DeepEqual(holdingRegisterWindows, wantHolding) {
		t.Fatalf("holding windows = %#v", holdingRegisterWindows)
	}
}
