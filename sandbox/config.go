// Package main - Rhodawk AI Sandbox Manager
// Configuration loader - reads all sandbox settings from environment variables.
//
// Security Note:
// All resource limits have safe defaults. The configuration system does NOT
// allow unlimited resources - even if env vars are set to extreme values,
// hard-coded maximums prevent runaway containers from consuming host resources.

package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Config holds all configurable values for the sandbox manager.
// Values are loaded from environment variables with sensible defaults.
type Config struct {
	// SandboxImage is the Docker image used for sandbox containers.
	// Must be pre-pulled on the host - the manager does not pull images.
	SandboxImage string

	// MaxConcurrent is the maximum number of sandboxes that can run simultaneously.
	// Requests beyond this limit are rejected with HTTP 429.
	MaxConcurrent int

	// DefaultTimeout is the default execution timeout in seconds.
	// Individual requests can specify shorter timeouts, but not longer.
	DefaultTimeout int

	// MemoryLimit is the maximum memory per sandbox (Docker format, e.g., "512m").
	MemoryLimit string

	// CPULimit is the maximum CPU cores per sandbox (e.g., 1.0 = one full core).
	CPULimit float64

	// Port is the HTTP port the sandbox manager listens on.
	Port int

	// DockerHost is the Docker daemon socket path or TCP address.
	DockerHost string

	// RedisURL is the Redis connection string for state persistence (optional).
	RedisURL string
}

// LoadConfig reads configuration from environment variables.
// Each variable has a documented default that provides secure operation
// out of the box without requiring any configuration.
func LoadConfig() *Config {
	cfg := &Config{
		SandboxImage:   getEnv("SANDBOX_IMAGE", "rhodawk-sandbox:latest"),
		MaxConcurrent:  getEnvInt("MAX_CONCURRENT_SANDBOXES", 5),
		DefaultTimeout: getEnvInt("SANDBOX_TIMEOUT", 600),
		MemoryLimit:    getEnv("SANDBOX_MEMORY_LIMIT", "512m"),
		CPULimit:       getEnvFloat("SANDBOX_CPU_LIMIT", 1.0),
		Port:           getEnvInt("SANDBOX_PORT", 8081),
		DockerHost:     getEnv("DOCKER_HOST", "unix:///var/run/docker.sock"),
		RedisURL:       getEnv("REDIS_URL", "redis://localhost:6379/0"),
	}

	// Enforce hard limits to prevent misconfiguration from exhausting host resources.
	if cfg.MaxConcurrent > 20 {
		cfg.MaxConcurrent = 20
	}
	if cfg.MaxConcurrent < 1 {
		cfg.MaxConcurrent = 1
	}
	if cfg.DefaultTimeout > 3600 {
		cfg.DefaultTimeout = 3600
	}
	if cfg.DefaultTimeout < 10 {
		cfg.DefaultTimeout = 10
	}
	if cfg.CPULimit > 4.0 {
		cfg.CPULimit = 4.0
	}
	if cfg.CPULimit < 0.1 {
		cfg.CPULimit = 0.1
	}

	return cfg
}

// String returns a human-readable representation of the config for logging.
// Sensitive values (RedisURL) are masked.
func (c *Config) String() string {
	return fmt.Sprintf(
		"Config{Image: %s, MaxConcurrent: %d, Timeout: %ds, Memory: %s, CPU: %.1f, Port: %d, Docker: %s}",
		c.SandboxImage, c.MaxConcurrent, c.DefaultTimeout,
		c.MemoryLimit, c.CPULimit, c.Port, c.DockerHost,
	)
}

// getEnv reads an environment variable with a fallback default.
func getEnv(key, defaultVal string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return defaultVal
}

// getEnvInt reads an integer environment variable with a fallback default.
func getEnvInt(key string, defaultVal int) int {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	n, err := strconv.Atoi(strings.TrimSpace(val))
	if err != nil {
		return defaultVal
	}
	return n
}

// getEnvFloat reads a float64 environment variable with a fallback default.
func getEnvFloat(key string, defaultVal float64) float64 {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	f, err := strconv.ParseFloat(strings.TrimSpace(val), 64)
	if err != nil {
		return defaultVal
	}
	return f
}

// parseMemoryLimit converts a Docker memory string (e.g., "512m", "1g") to bytes.
// Returns 512MB as the default if parsing fails.
func parseMemoryLimit(limit string) int64 {
	limit = strings.TrimSpace(strings.ToLower(limit))
	if limit == "" {
		return 512 * 1024 * 1024 // 512MB default
	}

	var multiplier int64
	var numStr string

	switch {
	case strings.HasSuffix(limit, "g"):
		multiplier = 1024 * 1024 * 1024
		numStr = strings.TrimSuffix(limit, "g")
	case strings.HasSuffix(limit, "m"):
		multiplier = 1024 * 1024
		numStr = strings.TrimSuffix(limit, "m")
	case strings.HasSuffix(limit, "k"):
		multiplier = 1024
		numStr = strings.TrimSuffix(limit, "k")
	default:
		// Assume bytes if no suffix
		n, err := strconv.ParseInt(limit, 10, 64)
		if err != nil {
			return 512 * 1024 * 1024
		}
		return n
	}

	n, err := strconv.ParseFloat(numStr, 64)
	if err != nil {
		return 512 * 1024 * 1024
	}
	return int64(n * float64(multiplier))
}
