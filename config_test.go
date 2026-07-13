package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLegacyConfigFallbacks(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.ini")
	if e := os.WriteFile(path, []byte("[MODBUS]\nPORT = /dev/ttyUSB0\n"), 0600); e != nil {
		t.Fatal(e)
	}
	c, e := ReadAppConfig(path)
	if e != nil {
		t.Fatal(e)
	}
	if c.Modbus.Timeout != 1500*time.Millisecond || c.Modbus.Retries != 2 || c.MQTT.TopicPrefix != "growatt_spf5000es" || c.MQTT.ConfigInterval != 30*time.Minute {
		t.Fatalf("unexpected fallbacks: %+v", c)
	}
}

func TestOptionalMQTTCredentials(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.ini")
	if e := os.WriteFile(path, []byte("[MODBUS]\nPORT=/dev/null\n[MQTT]\nUSER = none\nPASSWORD = false\n"), 0600); e != nil {
		t.Fatal(e)
	}
	c, e := ReadAppConfig(path)
	if e != nil {
		t.Fatal(e)
	}
	if c.MQTT.Username != "" || c.MQTT.Password != "" {
		t.Fatalf("credentials not normalized")
	}
}
