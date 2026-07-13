package main

import (
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"time"
)

const (
	taskWriteFlush    = "modbus_write_flush"
	taskTimeSync      = "inverter_time_sync"
	taskTimezoneCheck = "timezone_check"
	taskMQTTCommands  = "mqtt_commands"
	taskMQTTConfig    = "mqtt_config_publish"
	taskMQTTStatus    = "mqtt_status_publish"
)

type scheduledTask struct {
	name     string
	callback func()
	deadline time.Time
	priority int
	interval time.Duration
}

// Scheduler is a thread-safe cooperative deadline scheduler. Callbacks always
// execute on the goroutine that calls RunPending.
type Scheduler struct {
	mu     sync.Mutex
	tasks  map[string]*scheduledTask
	wakeup chan struct{}
	now    func() time.Time
}

func NewScheduler() *Scheduler { return newScheduler(time.Now) }

func newScheduler(clock func() time.Time) *Scheduler {
	return &Scheduler{tasks: make(map[string]*scheduledTask), wakeup: make(chan struct{}, 1), now: clock}
}

func (s *Scheduler) Register(name string, callback func(), interval time.Duration, priority int) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.tasks[name]; ok {
		return fmt.Errorf("task %q already registered", name)
	}
	s.tasks[name] = &scheduledTask{name: name, callback: callback, priority: priority, interval: interval}
	return nil
}

func (s *Scheduler) Schedule(name string, delay time.Duration, replace bool) error {
	s.mu.Lock()
	task, ok := s.tasks[name]
	if !ok {
		s.mu.Unlock()
		return fmt.Errorf("unknown task %q", name)
	}
	if delay < 0 {
		delay = 0
	}
	if task.deadline.IsZero() || replace {
		task.deadline = s.now().Add(delay)
	}
	s.mu.Unlock()
	select {
	case s.wakeup <- struct{}{}:
	default:
	}
	return nil
}

func (s *Scheduler) Cancel(name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	task, ok := s.tasks[name]
	if !ok {
		return fmt.Errorf("unknown task %q", name)
	}
	task.deadline = time.Time{}
	return nil
}

func (s *Scheduler) NextTimeout() (time.Duration, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var earliest time.Time
	for _, task := range s.tasks {
		if !task.deadline.IsZero() && (earliest.IsZero() || task.deadline.Before(earliest)) {
			earliest = task.deadline
		}
	}
	if earliest.IsZero() {
		return 0, false
	}
	d := earliest.Sub(s.now())
	if d < 0 {
		d = 0
	}
	return d, true
}

func (s *Scheduler) RunPending() {
	now := s.now()
	s.mu.Lock()
	type dueTask struct {
		task     *scheduledTask
		deadline time.Time
	}
	due := make([]dueTask, 0)
	for _, task := range s.tasks {
		if !task.deadline.IsZero() && !task.deadline.After(now) {
			deadline := task.deadline
			task.deadline = time.Time{}
			due = append(due, dueTask{task: task, deadline: deadline})
		}
	}
	s.mu.Unlock()
	sort.Slice(due, func(i, j int) bool {
		if due[i].task.priority != due[j].task.priority {
			return due[i].task.priority < due[j].task.priority
		}
		return due[i].deadline.Before(due[j].deadline)
	})
	for _, item := range due {
		task := item.task
		func() {
			defer func() {
				if r := recover(); r != nil {
					slog.Error("scheduled task panicked", "name", task.name, "error", r)
				}
			}()
			task.callback()
		}()
		s.mu.Lock()
		if task.interval > 0 && task.deadline.IsZero() {
			task.deadline = s.now().Add(task.interval)
		}
		s.mu.Unlock()
	}
}

func (s *Scheduler) Wait(max time.Duration) {
	timeout := max
	if next, ok := s.NextTimeout(); ok && next < timeout {
		timeout = next
	}
	if timeout <= 0 {
		return
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-timer.C:
	case <-s.wakeup:
	}
}
