package main

import (
	"fmt"
	"reflect"
	"testing"
	"time"
)

type fakeInverterModbus struct {
	holding  map[int]uint16
	writes   []queuedWrite
	deferred []time.Duration
	fail     bool
}

func (f *fakeInverterModbus) Connect() error                  { return nil }
func (f *fakeInverterModbus) Close() error                    { return nil }
func (f *fakeInverterModbus) WaitUntilReady()                 {}
func (f *fakeInverterModbus) DeferOperations(d time.Duration) { f.deferred = append(f.deferred, d) }
func (f *fakeInverterModbus) ReadInputRegisters(_ int, count int) ([]uint16, error) {
	if f.fail {
		return nil, fmt.Errorf("read failed")
	}
	return make([]uint16, count), nil
}
func (f *fakeInverterModbus) ReadHoldingRegisters(start, count int) ([]uint16, error) {
	if f.fail {
		return nil, fmt.Errorf("read failed")
	}
	out := make([]uint16, count)
	for n := range out {
		out[n] = f.holding[start+n]
	}
	return out, nil
}
func (f *fakeInverterModbus) WriteRegisters(start int, v []uint16) error {
	f.writes = append(f.writes, queuedWrite{start, append([]uint16(nil), v...)})
	return nil
}
func (f *fakeInverterModbus) apply() {
	for _, w := range f.writes {
		for n, v := range w.values {
			f.holding[w.start+n] = v
		}
	}
}

func testInverter(delay time.Duration) (*Inverter, *Scheduler, *fakeClock, *fakeInverterModbus) {
	c := &fakeClock{now: time.Unix(100, 0)}
	s := newScheduler(c.Now)
	cfg := ModbusConfig{Port: "/dev/null", WriteQueueSize: 128, WriteBatchDelay: delay, Timeout: time.Second}
	i := NewInverter(cfg, s)
	f := &fakeInverterModbus{holding: make(map[int]uint16)}
	i.client = f
	return i, s, c, f
}

func TestRegisterEncodingAndDecoding(t *testing.T) {
	d := holdingRegisters["BulkChargeVolt"]
	words, e := encodeRegisters(56.4, d)
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(words, []uint16{564}) {
		t.Fatalf("words=%v", words)
	}
	r := make([]uint16, 36)
	r[35] = 564
	got := decodeTable(r, map[string]registerDef{"BulkChargeVolt": d})["BulkChargeVolt"]
	if got != 56.4 {
		t.Fatalf("decoded=%v", got)
	}
}
func TestInvalidUTF8IsReplaced(t *testing.T) {
	s, e := registersToString([]uint16{0x4142, 0xdd00, 0}, 0, 3)
	if e != nil {
		t.Fatal(e)
	}
	if s != "AB�" {
		t.Fatalf("string=%q", s)
	}
}
func TestUnknownStatusFallsBack(t *testing.T) {
	v, e := inputRegisters["SystemStatus"].Decode(int64(999))
	if e != nil || v != "Unknown (999)" {
		t.Fatalf("value=%v error=%v", v, e)
	}
}

func TestWriteFlushAndReadbackSuppression(t *testing.T) {
	i, s, c, f := testInverter(250 * time.Millisecond)
	f.holding[34] = 25
	expected, e := i.WriteConfig("MaxChargeAmps", 30)
	if e != nil || expected != 30 {
		t.Fatalf("expected=%v error=%v", expected, e)
	}
	cfg, e := i.ReadConfig()
	if e != nil {
		t.Fatal(e)
	}
	if _, ok := cfg["MaxChargeAmps"]; ok {
		t.Fatal("stale value published before flush")
	}
	c.Advance(250 * time.Millisecond)
	s.RunPending()
	if len(f.writes) != 1 || f.writes[0].start != 34 || f.writes[0].values[0] != 30 {
		t.Fatalf("writes=%v", f.writes)
	}
	cfg, _ = i.ReadConfig()
	if _, ok := cfg["MaxChargeAmps"]; ok {
		t.Fatal("stale value published after flush")
	}
	f.apply()
	cfg, _ = i.ReadConfig()
	if cfg["MaxChargeAmps"] != 30 || i.HasPendingReadback() {
		t.Fatalf("readback=%v pending=%v", cfg["MaxChargeAmps"], i.HasPendingReadback())
	}
}

func TestReadbackGraceExpires(t *testing.T) {
	i, s, c, f := testInverter(0)
	f.holding[34] = 25
	_, _ = i.WriteConfig("MaxChargeAmps", 30)
	s.RunPending()
	c.Advance(readbackGrace)
	cfg, e := i.ReadConfig()
	if e != nil {
		t.Fatal(e)
	}
	if cfg["MaxChargeAmps"] != 25 || i.HasPendingReadback() {
		t.Fatalf("cfg=%v pending=%v", cfg["MaxChargeAmps"], i.HasPendingReadback())
	}
}

func TestWritesCoalesceAndDoNotMoveDeadline(t *testing.T) {
	i, s, c, f := testInverter(time.Minute)
	_, _ = i.WriteConfig("MaxChargeAmps", 30)
	c.Advance(30 * time.Second)
	_, _ = i.WriteConfig("BulkChargeVolt", 56.4)
	c.Advance(30 * time.Second)
	s.RunPending()
	if len(f.writes) != 1 || f.writes[0].start != 34 || !reflect.DeepEqual(f.writes[0].values, []uint16{30, 564}) {
		t.Fatalf("writes=%v", f.writes)
	}
}

func TestConsecutiveReadFailuresReset(t *testing.T) {
	i, _, _, f := testInverter(0)
	f.fail = true
	for n := 1; n <= 2; n++ {
		if _, e := i.ReadStatus(); e == nil {
			t.Fatal("expected failure")
		}
		if i.ConsecutiveReadFailures() != n {
			t.Fatalf("failures=%d", i.ConsecutiveReadFailures())
		}
	}
	f.fail = false
	if _, e := i.ReadStatus(); e != nil {
		t.Fatal(e)
	}
	if i.ConsecutiveReadFailures() != 0 {
		t.Fatal("failure count did not reset")
	}
}
