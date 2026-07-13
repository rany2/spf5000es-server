package main

import (
	"encoding/binary"
	"testing"
	"time"
)

type fakeModbusBackend struct {
	writeResponse []byte
	writtenStart  uint16
	writtenCount  uint16
	writtenData   []byte
}

func (f *fakeModbusBackend) Connect() error { return nil }
func (f *fakeModbusBackend) Close() error   { return nil }
func (f *fakeModbusBackend) ReadInputRegisters(_, _ uint16) ([]byte, error) {
	return nil, nil
}
func (f *fakeModbusBackend) ReadHoldingRegisters(_, _ uint16) ([]byte, error) {
	return nil, nil
}
func (f *fakeModbusBackend) WriteMultipleRegisters(address, quantity uint16, data []byte) ([]byte, error) {
	f.writtenStart = address
	f.writtenCount = quantity
	f.writtenData = append([]byte(nil), data...)
	return f.writeResponse, nil
}

func TestWriteRegistersAcceptsGoburrowAcknowledgement(t *testing.T) {
	response := make([]byte, 2)
	binary.BigEndian.PutUint16(response, 3)
	backend := &fakeModbusBackend{writeResponse: response}
	client := &ModbusClient{backend: backend, retries: 0, reconnectDelay: 0}

	if err := client.WriteRegisters(45, []uint16{2026, 7, 13}); err != nil {
		t.Fatal(err)
	}
	if backend.writtenStart != 45 || backend.writtenCount != 3 {
		t.Fatalf("write request = start %d, count %d", backend.writtenStart, backend.writtenCount)
	}
	want := []byte{0x07, 0xea, 0x00, 0x07, 0x00, 0x0d}
	if string(backend.writtenData) != string(want) {
		t.Fatalf("write data = %x, want %x", backend.writtenData, want)
	}
}

func TestWriteRegistersRejectsWrongAcknowledgedCount(t *testing.T) {
	response := make([]byte, 2)
	binary.BigEndian.PutUint16(response, 2)
	client := &ModbusClient{
		backend:        &fakeModbusBackend{writeResponse: response},
		retries:        0,
		reconnectDelay: time.Nanosecond,
	}
	if err := client.WriteRegisters(45, []uint16{2026, 7, 13}); err == nil {
		t.Fatal("expected mismatched acknowledgement to fail")
	}
}
