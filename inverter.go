package main

import (
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"time"
)

const (
	configWriteSettleDelay = time.Second
	timeSyncInterval       = 12 * time.Minute
	timezoneCheckInterval  = time.Second
	readbackGrace          = 10 * time.Second
)

type queuedWrite struct {
	start  int
	values []uint16
}
type pendingReadback struct {
	start    int
	values   []uint16
	deadline time.Time
}

type Inverter struct {
	mu                      sync.Mutex
	client                  inverterModbus
	scheduler               *Scheduler
	writeBatchDelay         time.Duration
	writeQueueSize          int
	writeQueue              []queuedWrite
	pending                 map[string]pendingReadback
	batteryType             string
	consecutiveReadFailures int
	now                     func() time.Time
	sleep                   func(time.Duration)
	timezoneName            string
	timezoneOffset          int
	timezoneKnown           bool
}

func NewInverter(cfg ModbusConfig, s *Scheduler) *Inverter {
	i := &Inverter{client: NewModbusClient(cfg), scheduler: s, writeBatchDelay: max(time.Duration(0), cfg.WriteBatchDelay), writeQueueSize: max(1, cfg.WriteQueueSize), pending: make(map[string]pendingReadback), now: time.Now, sleep: time.Sleep}
	must(s.Register(taskWriteFlush, i.FlushPendingWrites, 0, 10))
	must(s.Register(taskTimezoneCheck, i.CheckTimezone, timezoneCheckInterval, 80))
	must(s.Register(taskTimeSync, i.SyncTime, 0, 90))
	return i
}
func must(err error) {
	if err != nil {
		panic(err)
	}
}
func (i *Inverter) Connect() {
	if err := i.client.Connect(); err != nil {
		slog.Error("initial inverter connect failed; operations will retry", "error", err)
	}
	i.rememberTimezone(i.now())
	must(i.scheduler.Schedule(taskTimezoneCheck, timezoneCheckInterval, true))
	must(i.scheduler.Schedule(taskTimeSync, 0, true))
}
func (i *Inverter) Close() error { return i.client.Close() }

func (i *Inverter) SyncTime() {
	i.mu.Lock()
	defer i.mu.Unlock()
	defer func() { must(i.scheduler.Schedule(taskTimeSync, timeSyncInterval, true)) }()
	i.client.WaitUntilReady()
	now := i.now()
	if wait := time.Second - time.Duration(now.Nanosecond()); wait > 0 {
		i.sleep(wait)
		now = i.now()
	}
	i.timezoneName, i.timezoneOffset = now.Zone()
	i.timezoneKnown = true
	values := []uint16{uint16(now.Year()), uint16(now.Month()), uint16(now.Day()), uint16(now.Hour()), uint16(now.Minute()), uint16(now.Second())}
	if err := i.client.WriteRegisters(45, values); err != nil {
		slog.Error("failed to update inverter time", "error", err)
		return
	}
	slog.Info("inverter time sync completed", "values", values)
}

func (i *Inverter) rememberTimezone(now time.Time) {
	name, offset := now.Zone()
	i.mu.Lock()
	i.timezoneName = name
	i.timezoneOffset = offset
	i.timezoneKnown = true
	i.mu.Unlock()
}

// CheckTimezone immediately synchronizes the inverter when the local timezone
// name or UTC offset changes, including daylight-saving transitions.
func (i *Inverter) CheckTimezone() {
	now := i.now()
	name, offset := now.Zone()
	i.mu.Lock()
	if !i.timezoneKnown {
		i.timezoneName = name
		i.timezoneOffset = offset
		i.timezoneKnown = true
		i.mu.Unlock()
		return
	}
	oldName, oldOffset := i.timezoneName, i.timezoneOffset
	changed := name != oldName || offset != oldOffset
	if changed {
		i.timezoneName = name
		i.timezoneOffset = offset
	}
	i.mu.Unlock()
	if !changed {
		return
	}
	slog.Info("local timezone changed; syncing inverter time", "old_zone", oldName, "new_zone", name, "old_offset", oldOffset, "new_offset", offset)
	i.SyncTime()
}

func coalesceWrites(requested map[int][]uint16) []queuedWrite {
	words := make(map[int]uint16)
	for start, values := range requested {
		for off, v := range values {
			words[start+off] = v
		}
	}
	addresses := make([]int, 0, len(words))
	for a := range words {
		addresses = append(addresses, a)
	}
	sort.Ints(addresses)
	out := []queuedWrite{}
	for _, a := range addresses {
		n := len(out)
		if n == 0 || a != out[n-1].start+len(out[n-1].values) || len(out[n-1].values) >= maxWriteRegisters {
			out = append(out, queuedWrite{a, []uint16{words[a]}})
		} else {
			out[n-1].values = append(out[n-1].values, words[a])
		}
	}
	return out
}

func (i *Inverter) FlushPendingWrites() {
	i.mu.Lock()
	defer i.mu.Unlock()
	if len(i.writeQueue) == 0 {
		return
	}
	requested := make(map[int][]uint16)
	for _, w := range i.writeQueue {
		requested[w.start] = w.values
	}
	i.writeQueue = nil
	failedBatches := 0
	for _, batch := range coalesceWrites(requested) {
		if err := i.client.WriteRegisters(batch.start, batch.values); err != nil {
			slog.Error("failed to write registers", "start", batch.start, "count", len(batch.values), "error", err)
			failedBatches++
			batchEnd := batch.start + len(batch.values)
			for key, pending := range i.pending {
				pendingEnd := pending.start + len(pending.values)
				if pending.start < batchEnd && pendingEnd > batch.start {
					delete(i.pending, key)
				}
			}
		}
		i.client.DeferOperations(configWriteSettleDelay)
	}
	deadline := i.scheduler.now().Add(readbackGrace)
	for key, p := range i.pending {
		if p.deadline.IsZero() {
			p.deadline = deadline
			i.pending[key] = p
		}
	}
	if failedBatches > 0 {
		slog.Warn("config write flush completed with failures", "requests", len(requested), "failed_batches", failedBatches)
	} else {
		slog.Info("config writes flushed", "requests", len(requested))
	}
}

func (i *Inverter) readWindows(reader func(int, int) ([]uint16, error), windows []registerWindow) ([]uint16, error) {
	maxEnd := 0
	for _, w := range windows {
		maxEnd = max(maxEnd, w.Start+w.Count)
	}
	out := make([]uint16, maxEnd)
	for _, w := range windows {
		values, err := reader(w.Start, w.Count)
		if err != nil {
			return nil, err
		}
		if len(values) != w.Count {
			return nil, fmt.Errorf("read returned %d registers, expected %d", len(values), w.Count)
		}
		copy(out[w.Start:], values)
	}
	return out, nil
}
func (i *Inverter) trackedRead(reader func(int, int) ([]uint16, error), windows []registerWindow) ([]uint16, error) {
	r, e := i.readWindows(reader, windows)
	if e != nil {
		i.consecutiveReadFailures++
		return nil, e
	}
	i.consecutiveReadFailures = 0
	return r, nil
}
func (i *Inverter) ReadStatus() (map[string]any, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	r, e := i.trackedRead(i.client.ReadInputRegisters, inputRegisterWindows)
	if e != nil {
		return nil, e
	}
	return decodeTable(r, inputRegisters), nil
}
func (i *Inverter) ReadConfig() (map[string]any, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	r, e := i.trackedRead(i.client.ReadHoldingRegisters, holdingRegisterWindows)
	if e != nil {
		return nil, e
	}
	out := decodeTable(r, holdingRegisters)
	if batteryType, ok := out["BatteryType"].(string); ok {
		i.batteryType = batteryType
	}
	now := i.scheduler.now()
	for key, p := range i.pending {
		actual := r[p.start : p.start+len(p.values)]
		if equalWords(actual, p.values) {
			delete(i.pending, key)
		} else if !p.deadline.IsZero() && !now.Before(p.deadline) {
			delete(i.pending, key)
			slog.Warn("config write not visible before timeout", "key", key)
		} else {
			delete(out, key)
		}
	}
	return out, nil
}
func equalWords(a, b []uint16) bool {
	if len(a) != len(b) {
		return false
	}
	for n := range a {
		if a[n] != b[n] {
			return false
		}
	}
	return true
}
func (i *Inverter) ConsecutiveReadFailures() int {
	i.mu.Lock()
	defer i.mu.Unlock()
	return i.consecutiveReadFailures
}
func (i *Inverter) HasPendingReadback() bool {
	i.mu.Lock()
	defer i.mu.Unlock()
	return len(i.pending) > 0
}
func (i *Inverter) WriteBatchDelay() time.Duration { return i.writeBatchDelay }

func (i *Inverter) validationBatteryType() string {
	pending, ok := i.pending["BatteryType"]
	if !ok {
		return i.batteryType
	}
	def := holdingRegisters["BatteryType"]
	raw, err := rawValue(pending.values, registerDef{Start: 0, Length: def.Length, Kind: def.Kind})
	if err != nil {
		return i.batteryType
	}
	value, err := def.Decode(raw)
	if err != nil {
		return i.batteryType
	}
	if batteryType, ok := value.(string); ok {
		return batteryType
	}
	return i.batteryType
}

var ErrWriteQueueFull = fmt.Errorf("write queue is full")

func (i *Inverter) WriteConfig(key string, value any) (any, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	d, ok := holdingRegisters[key]
	if !ok {
		return nil, fmt.Errorf("invalid key")
	}
	if d.Encode == nil {
		return nil, fmt.Errorf("register is not writeable")
	}
	if e := validateConfigValue(key, value, d, i.validationBatteryType()); e != nil {
		return nil, fmt.Errorf("invalid value: %w", e)
	}
	words, e := encodeRegisters(value, d)
	if e != nil {
		return nil, fmt.Errorf("invalid value: %w", e)
	}
	if len(i.writeQueue) >= i.writeQueueSize {
		return nil, ErrWriteQueueFull
	}
	i.writeQueue = append(i.writeQueue, queuedWrite{d.Start, words})
	i.pending[key] = pendingReadback{start: d.Start, values: append([]uint16(nil), words...)}
	must(i.scheduler.Schedule(taskWriteFlush, i.writeBatchDelay, false))
	raw, e := rawValue(words, registerDef{Start: 0, Length: d.Length, Kind: d.Kind})
	if e != nil {
		return nil, nil
	}
	expected, e := d.Decode(raw)
	if e != nil {
		return nil, nil
	}
	return expected, nil
}

type inverterAPI interface {
	ReadStatus() (map[string]any, error)
	ReadConfig() (map[string]any, error)
	WriteConfig(string, any) (any, error)
	SyncTime()
	ConsecutiveReadFailures() int
	HasPendingReadback() bool
	WriteBatchDelay() time.Duration
}
