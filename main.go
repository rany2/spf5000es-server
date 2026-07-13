package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func configureLogging(level string) error {
	var l slog.Level
	if err := l.UnmarshalText([]byte(strings.ToUpper(strings.TrimSpace(level)))); err != nil {
		return err
	}
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: l})))
	return nil
}

func run(ctx context.Context) error {
	cfg, err := ReadAppConfig("config.ini")
	if err != nil {
		return err
	}
	if err = configureLogging(cfg.LogLevel); err != nil {
		return err
	}
	scheduler := NewScheduler()
	inverter := NewInverter(cfg.Modbus, scheduler)
	service := NewMQTTService(inverter, cfg.MQTT, scheduler)
	inverter.Connect()
	if err = service.Start(); err != nil {
		slog.Warn("initial MQTT connection incomplete; reconnect continues in background", "error", err)
	}
	defer service.Stop()
	defer func() {
		if e := inverter.Close(); e != nil {
			slog.Error("failed to close inverter", "error", e)
		}
	}()
	slog.Info("service loop started", "port", cfg.Modbus.Port, "mqtt_host", cfg.MQTT.Host, "mqtt_port", cfg.MQTT.Port, "status_interval", cfg.MQTT.StatusInterval, "config_interval", cfg.MQTT.ConfigInterval)
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}
		scheduler.RunPending()
		scheduler.Wait(500 * time.Millisecond)
	}
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx); err != nil {
		slog.Error("service stopped", "error", err)
		os.Exit(1)
	}
}
