package main

import (
	"fmt"
	"reflect"
	"testing"
	"time"
)

type fakeInverterModbus struct {
	holding   map[int]uint16
	writes    []queuedWrite
	deferred  []time.Duration
	fail      bool
	failWrite bool
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
	if f.failWrite {
		return fmt.Errorf("write failed")
	}
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
	i.now = c.Now
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

func TestWriteConfigEnforcesProtocolLimits(t *testing.T) {
	i, _, _, _ := testInverter(0)
	for _, test := range []struct {
		key   string
		value any
	}{
		{"SysMonth", 13},
		{"MaxChargeAmps", 181},
		{"BulkChargeVolt", 49.9},
		{"BulkChargeVolt", 56.45},
	} {
		if _, err := i.WriteConfig(test.key, test.value); err == nil {
			t.Errorf("WriteConfig(%q, %v) unexpectedly succeeded", test.key, test.value)
		}
	}
	if _, err := i.WriteConfig("BulkChargeVolt", 56.4); err != nil {
		t.Fatalf("valid value rejected: %v", err)
	}
}

func TestBatteryTypeControlsWriteLimits(t *testing.T) {
	i, _, _, f := testInverter(0)
	f.holding[39] = 3 // Lithium
	if _, err := i.ReadConfig(); err != nil {
		t.Fatal(err)
	}
	if _, err := i.WriteConfig("BatLowtoUti", 30.5); err == nil {
		t.Fatal("fractional lithium percentage unexpectedly accepted")
	}
	if _, err := i.WriteConfig("BatLowtoUti", 30); err != nil {
		t.Fatalf("valid lithium percentage rejected: %v", err)
	}
}

func TestPendingBatteryTypeControlsSubsequentWriteLimits(t *testing.T) {
	i, _, _, f := testInverter(time.Minute)
	f.holding[39] = 0 // AGM
	if _, err := i.ReadConfig(); err != nil {
		t.Fatal(err)
	}
	if _, err := i.WriteConfig("BatteryType", "Lithium"); err != nil {
		t.Fatal(err)
	}
	if _, err := i.WriteConfig("BatLowtoUti", 80); err != nil {
		t.Fatalf("pending lithium type was not used for validation: %v", err)
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

func TestTimezoneOffsetChangeTriggersImmediateSync(t *testing.T) {
	i, s, clock, modbus := testInverter(0)
	standard := time.FixedZone("EET", 2*60*60)
	daylight := time.FixedZone("EEST", 3*60*60)
	clock.now = time.Date(2026, 3, 29, 2, 59, 59, 250_000_000, standard)
	i.rememberTimezone(clock.now)
	i.sleep = clock.Advance
	clock.now = clock.now.In(daylight)

	must(s.Schedule(taskTimezoneCheck, 0, true))
	s.RunPending()

	if len(modbus.writes) != 1 || modbus.writes[0].start != 45 {
		t.Fatalf("timezone change writes = %v", modbus.writes)
	}
	want := []uint16{2026, 3, 29, 4, 0, 0}
	if got := modbus.writes[0].values; !reflect.DeepEqual(got, want) {
		t.Fatalf("synced time = %v, want %v", got, want)
	}
	if next, ok := s.NextTimeout(); !ok || next != timezoneCheckInterval {
		t.Fatalf("timezone checker was not rearmed: %v, %v", next, ok)
	}
}

func TestUnchangedTimezoneDoesNotSync(t *testing.T) {
	i, _, clock, modbus := testInverter(0)
	clock.now = time.Date(2026, 1, 1, 12, 0, 0, 0, time.FixedZone("EET", 2*60*60))
	i.rememberTimezone(clock.now)
	i.CheckTimezone()
	if len(modbus.writes) != 0 {
		t.Fatalf("unchanged timezone caused writes: %v", modbus.writes)
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

func TestFailedWriteCancelsOptimisticReadback(t *testing.T) {
	i, s, _, f := testInverter(0)
	f.holding[34] = 25
	f.failWrite = true
	if _, err := i.WriteConfig("MaxChargeAmps", 30); err != nil {
		t.Fatal(err)
	}
	s.RunPending()
	if i.HasPendingReadback() {
		t.Fatal("failed write remained pending")
	}
	cfg, err := i.ReadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg["MaxChargeAmps"] != 25 {
		t.Fatalf("device value = %v", cfg["MaxChargeAmps"])
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
