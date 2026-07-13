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
	if c.Modbus.Timeout != 1500*time.Millisecond || c.Modbus.Retries != 2 || c.MQTT.TopicPrefix != "growatt_spf5000es" || c.MQTT.ConfigInterval != 30*time.Minute || c.MQTT.StatusInterval != time.Second {
		t.Fatalf("unexpected fallbacks: %+v", c)
	}
}

func TestConfigRejectsUnsafeValues(t *testing.T) {
	for name, body := range map[string]string{
		"nan timeout":    "[MODBUS]\nPORT=/dev/null\nTIMEOUT_SEC=NaN\n",
		"negative retry": "[MODBUS]\nPORT=/dev/null\nRETRIES=-1\n",
		"invalid port":   "[MODBUS]\nPORT=/dev/null\n[MQTT]\nPORT=70000\n",
		"empty host":     "[MODBUS]\nPORT=/dev/null\n[MQTT]\nHOST=\n",
		"fast polling":   "[MODBUS]\nPORT=/dev/null\n[MQTT]\nSTATUS_INTERVAL_SEC=0.01\n",
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "config.ini")
			if err := os.WriteFile(path, []byte(body), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := ReadAppConfig(path); err == nil {
				t.Fatal("invalid configuration was accepted")
			}
		})
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
