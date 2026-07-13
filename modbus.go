package main

import (
	"encoding/binary"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/goburrow/modbus"
)

const (
	maxDeviceReadRegisters = 45
	maxWriteRegisters      = 123
)

type modbusBackend interface {
	Connect() error
	Close() error
	ReadInputRegisters(address, quantity uint16) ([]byte, error)
	ReadHoldingRegisters(address, quantity uint16) ([]byte, error)
	WriteMultipleRegisters(address, quantity uint16, value []byte) ([]byte, error)
}

type rtuBackend struct {
	handler *modbus.RTUClientHandler
	client  modbus.Client
}

func (b *rtuBackend) Connect() error { return b.handler.Connect() }
func (b *rtuBackend) Close() error   { return b.handler.Close() }
func (b *rtuBackend) ReadInputRegisters(a, q uint16) ([]byte, error) {
	return b.client.ReadInputRegisters(a, q)
}
func (b *rtuBackend) ReadHoldingRegisters(a, q uint16) ([]byte, error) {
	return b.client.ReadHoldingRegisters(a, q)
}
func (b *rtuBackend) WriteMultipleRegisters(a, q uint16, v []byte) ([]byte, error) {
	return b.client.WriteMultipleRegisters(a, q, v)
}

type ModbusClient struct {
	mu             sync.Mutex
	backend        modbusBackend
	port           string
	retries        int
	reconnectDelay time.Duration
	nextAllowed    time.Time
}

func NewModbusClient(cfg ModbusConfig) *ModbusClient {
	h := modbus.NewRTUClientHandler(cfg.Port)
	h.BaudRate = 9600
	h.DataBits = 8
	h.Parity = "N"
	h.StopBits = 1
	h.SlaveId = 1
	h.Timeout = cfg.Timeout
	return &ModbusClient{backend: &rtuBackend{handler: h, client: modbus.NewClient(h)}, port: cfg.Port, retries: max(0, cfg.Retries), reconnectDelay: max(time.Duration(0), cfg.ReconnectDelay)}
}

func (c *ModbusClient) Connect() error { c.mu.Lock(); defer c.mu.Unlock(); return c.backend.Connect() }
func (c *ModbusClient) Close() error   { c.mu.Lock(); defer c.mu.Unlock(); return c.backend.Close() }

func validateRange(start, count, maxCount int) error {
	if start < 0 {
		return fmt.Errorf("register start must be non-negative")
	}
	if count <= 0 {
		return fmt.Errorf("register count must be positive")
	}
	if start > 65535 || start+count-1 > 65535 {
		return fmt.Errorf("register range exceeds address space")
	}
	if count > maxCount {
		return fmt.Errorf("register count exceeds %d-register maximum", maxCount)
	}
	return nil
}

func (c *ModbusClient) waitReadyLocked() {
	if c.nextAllowed.IsZero() {
		return
	}
	if d := time.Until(c.nextAllowed); d > 0 {
		time.Sleep(d)
	}
	c.nextAllowed = time.Time{}
}
func (c *ModbusClient) DeferOperations(delay time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	deadline := time.Now().Add(max(time.Duration(0), delay))
	if deadline.After(c.nextAllowed) {
		c.nextAllowed = deadline
	}
}
func (c *ModbusClient) WaitUntilReady() { c.mu.Lock(); defer c.mu.Unlock(); c.waitReadyLocked() }

func (c *ModbusClient) recoverLocked(operation func() error) error {
	var last error
	for attempt := 0; attempt <= c.retries; attempt++ {
		if err := operation(); err == nil {
			return nil
		} else {
			last = err
			slog.Warn("Modbus request failed", "attempt", attempt+1, "error", err)
		}
		_ = c.backend.Close()
		if c.reconnectDelay > 0 {
			time.Sleep(c.reconnectDelay)
		}
		if err := c.backend.Connect(); err != nil {
			slog.Warn("failed to reopen Modbus serial port", "port", c.port, "error", err)
		}
	}
	return fmt.Errorf("Modbus operation failed after %d attempt(s): %w", c.retries+1, last)
}

func decodeWords(data []byte, count int) ([]uint16, error) {
	if len(data) != count*2 {
		return nil, fmt.Errorf("Modbus read returned %d bytes for %d registers", len(data), count)
	}
	out := make([]uint16, count)
	for i := range out {
		out[i] = binary.BigEndian.Uint16(data[i*2:])
	}
	return out, nil
}

func (c *ModbusClient) read(start, count int, input bool) ([]uint16, error) {
	if err := validateRange(start, count, maxDeviceReadRegisters); err != nil {
		return nil, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.waitReadyLocked()
	var out []uint16
	err := c.recoverLocked(func() error {
		var data []byte
		var err error
		if input {
			data, err = c.backend.ReadInputRegisters(uint16(start), uint16(count))
		} else {
			data, err = c.backend.ReadHoldingRegisters(uint16(start), uint16(count))
		}
		if err != nil {
			return err
		}
		out, err = decodeWords(data, count)
		return err
	})
	return out, err
}
func (c *ModbusClient) ReadInputRegisters(start, count int) ([]uint16, error) {
	return c.read(start, count, true)
}
func (c *ModbusClient) ReadHoldingRegisters(start, count int) ([]uint16, error) {
	return c.read(start, count, false)
}
func (c *ModbusClient) WriteRegisters(start int, values []uint16) error {
	if err := validateRange(start, len(values), maxWriteRegisters); err != nil {
		return err
	}
	data := make([]byte, len(values)*2)
	for i, v := range values {
		binary.BigEndian.PutUint16(data[i*2:], v)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.waitReadyLocked()
	return c.recoverLocked(func() error {
		response, err := c.backend.WriteMultipleRegisters(uint16(start), uint16(len(values)), data)
		if err != nil {
			return err
		}
		// goburrow/modbus validates the echoed address internally and returns
		// only the two-byte quantity portion of the function-16 response.
		if len(response) != 2 || binary.BigEndian.Uint16(response) != uint16(len(values)) {
			return fmt.Errorf("Modbus write acknowledgement did not match requested count")
		}
		return nil
	})
}

type inverterModbus interface {
	Connect() error
	Close() error
	ReadInputRegisters(int, int) ([]uint16, error)
	ReadHoldingRegisters(int, int) ([]uint16, error)
	WriteRegisters(int, []uint16) error
	DeferOperations(time.Duration)
	WaitUntilReady()
}
