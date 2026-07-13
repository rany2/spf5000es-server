package main

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type ModbusConfig struct {
	Port            string
	WriteQueueSize  int
	WriteBatchDelay time.Duration
	Timeout         time.Duration
	Retries         int
	ReconnectDelay  time.Duration
}

type MQTTConfig struct {
	Host                         string
	Port                         int
	Username, Password           string
	ClientID                     string
	Keepalive                    time.Duration
	TopicPrefix, DiscoveryPrefix string
	DeviceID, DeviceName         string
	ConfigInterval               time.Duration
}

type AppConfig struct {
	Modbus   ModbusConfig
	MQTT     MQTTConfig
	LogLevel string
}

func parseINI(path string) (map[string]map[string]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data := make(map[string]map[string]string)
	section := ""
	s := bufio.NewScanner(f)
	for line := 1; s.Scan(); line++ {
		v := strings.TrimSpace(s.Text())
		if v == "" || strings.HasPrefix(v, ";") || strings.HasPrefix(v, "#") {
			continue
		}
		if strings.HasPrefix(v, "[") && strings.HasSuffix(v, "]") {
			section = strings.ToUpper(strings.TrimSpace(v[1 : len(v)-1]))
			if data[section] == nil {
				data[section] = make(map[string]string)
			}
			continue
		}
		key, value, ok := strings.Cut(v, "=")
		if !ok {
			return nil, fmt.Errorf("%s:%d: invalid INI line", path, line)
		}
		if data[section] == nil {
			data[section] = make(map[string]string)
		}
		data[section][strings.ToUpper(strings.TrimSpace(key))] = strings.TrimSpace(value)
	}
	return data, s.Err()
}

func ReadAppConfig(path string) (AppConfig, error) {
	ini, err := parseINI(path)
	if err != nil {
		return AppConfig{}, err
	}
	get := func(section, key, fallback string) string {
		if v, ok := ini[section][key]; ok {
			return v
		}
		return fallback
	}
	requiredPort := get("MODBUS", "PORT", "")
	if requiredPort == "" {
		return AppConfig{}, fmt.Errorf("missing MODBUS.PORT")
	}
	intv := func(s, k string, d int) (int, error) {
		v := get(s, k, strconv.Itoa(d))
		n, e := strconv.Atoi(v)
		if e != nil {
			return 0, fmt.Errorf("invalid %s.%s: %w", s, k, e)
		}
		return n, nil
	}
	floatv := func(s, k string, d float64) (float64, error) {
		v := get(s, k, strconv.FormatFloat(d, 'f', -1, 64))
		n, e := strconv.ParseFloat(v, 64)
		if e != nil {
			return 0, fmt.Errorf("invalid %s.%s: %w", s, k, e)
		}
		return n, nil
	}
	wq, e := intv("MODBUS", "WRITE_QUEUE_SIZE", 128)
	if e != nil {
		return AppConfig{}, e
	}
	retries, e := intv("MODBUS", "RETRIES", 2)
	if e != nil {
		return AppConfig{}, e
	}
	mbd, e := floatv("MODBUS", "WRITE_BATCH_DELAY_SEC", .25)
	if e != nil {
		return AppConfig{}, e
	}
	timeout, e := floatv("MODBUS", "TIMEOUT_SEC", 1.5)
	if e != nil {
		return AppConfig{}, e
	}
	rd, e := floatv("MODBUS", "RECONNECT_DELAY_SEC", .2)
	if e != nil {
		return AppConfig{}, e
	}
	deviceID := get("MQTT", "DEVICE_ID", "growatt_spf5000es")
	port, e := intv("MQTT", "PORT", 1883)
	if e != nil {
		return AppConfig{}, e
	}
	keep, e := intv("MQTT", "KEEPALIVE_SEC", 60)
	if e != nil {
		return AppConfig{}, e
	}
	ci, e := floatv("MQTT", "CONFIG_INTERVAL_SEC", 1800)
	if e != nil {
		return AppConfig{}, e
	}
	optional := func(v string) string {
		v = strings.TrimSpace(v)
		switch strings.ToLower(v) {
		case "", "none", "null", "false":
			return ""
		}
		return v
	}
	cfg := AppConfig{
		Modbus:   ModbusConfig{requiredPort, max(1, wq), time.Duration(max(0.0, mbd) * float64(time.Second)), time.Duration(max(.1, timeout) * float64(time.Second)), max(0, retries), time.Duration(max(0.0, rd) * float64(time.Second))},
		MQTT:     MQTTConfig{Host: get("MQTT", "HOST", "localhost"), Port: port, Username: optional(get("MQTT", "USER", "")), Password: optional(get("MQTT", "PASSWORD", "")), ClientID: get("MQTT", "CLIENT_ID", deviceID), Keepalive: time.Duration(max(1, keep)) * time.Second, TopicPrefix: strings.Trim(get("MQTT", "TOPIC_PREFIX", deviceID), "/"), DiscoveryPrefix: strings.Trim(get("MQTT", "DISCOVERY_PREFIX", "homeassistant"), "/"), DeviceID: deviceID, DeviceName: get("MQTT", "DEVICE_NAME", "Growatt SPF 5000 ES"), ConfigInterval: time.Duration(max(1.0, ci) * float64(time.Second))},
		LogLevel: get("LOGGING", "LEVEL", "INFO"),
	}
	if cfg.MQTT.TopicPrefix == "" {
		cfg.MQTT.TopicPrefix = deviceID
	}
	if cfg.MQTT.DiscoveryPrefix == "" {
		cfg.MQTT.DiscoveryPrefix = "homeassistant"
	}
	return cfg, nil
}
