// Package main - Rhodawk AI Sandbox Manager
// Type definitions for the sandbox management system.
//
// Security Model:
// - SandboxRequest defines the attack surface boundary: all fields are validated
//   before container creation. Timeout, resource limits, and network isolation
//   ensure untrusted code cannot escape or abuse host resources.
// - ActiveSandbox tracks live containers with cancellation support for immediate
//   termination when timeouts expire or kill requests arrive.
// - All output is bounded (max 200 lines) to prevent memory exhaustion from
//   malicious programs that produce unlimited output.

package main

import (
	"context"
	"sync"
	"time"
)

// SandboxRequest represents an incoming request to create a new sandbox.
// All fields are validated before container creation proceeds.
type SandboxRequest struct {
	// TaskID is a unique identifier for the task that owns this sandbox.
	// Used for tracking and audit purposes.
	TaskID string `json:"task_id"`

	// Command is the shell command to execute inside the sandbox.
	// Executed via /bin/bash -c in the container.
	Command string `json:"command"`

	// WorkdirContent is optional base64-encoded content to write to the sandbox workspace.
	// If provided, decoded and mounted into /home/sandbox/workspace.
	WorkdirContent string `json:"workdir_content,omitempty"`

	// Timeout is the maximum execution time in seconds.
	// If zero, defaults to the configured SANDBOX_TIMEOUT.
	Timeout int `json:"timeout,omitempty"`

	// Network enables network access for the sandbox (default: false/isolated).
	// WARNING: Enabling network access reduces isolation. Only enable for
	// tasks that explicitly require external connectivity.
	Network bool `json:"network,omitempty"`

	// Env is a map of additional environment variables to inject into the sandbox.
	// System-critical variables (PATH, HOME) cannot be overridden.
	Env map[string]string `json:"env,omitempty"`
}

// SandboxResponse is returned when a sandbox is successfully created.
type SandboxResponse struct {
	// SandboxID is the unique identifier for the created sandbox (Docker container ID prefix).
	SandboxID string `json:"sandbox_id"`

	// Status is the current state of the sandbox (e.g., "running", "created").
	Status string `json:"status"`

	// Timeout is the effective timeout in seconds for this sandbox.
	Timeout int `json:"timeout"`
}

// SandboxStatus represents the current state of a sandbox, including output if completed.
type SandboxStatus struct {
	// SandboxID is the unique identifier for the sandbox.
	SandboxID string `json:"sandbox_id"`

	// Status is the current state: "running", "exited", "killed", "timeout", "not_found".
	Status string `json:"status"`

	// Duration is how long the sandbox has been running (or ran), in seconds.
	Duration float64 `json:"duration"`

	// ExitCode is the process exit code (only meaningful when Status is "exited").
	ExitCode int `json:"exit_code"`

	// Output contains the last 200 lines of combined stdout/stderr.
	// Only populated when the sandbox has finished execution.
	Output string `json:"output,omitempty"`
}

// SandboxStats provides an overview of sandbox system utilization.
type SandboxStats struct {
	// Active is the number of currently running sandboxes.
	Active int `json:"active"`

	// MaxConcurrent is the configured maximum number of concurrent sandboxes.
	MaxConcurrent int `json:"max_concurrent"`

	// Sandboxes lists information about each active sandbox.
	Sandboxes []SandboxInfo `json:"sandboxes"`
}

// SandboxInfo provides summary information about a single active sandbox.
type SandboxInfo struct {
	// ID is the sandbox identifier (container ID prefix).
	ID string `json:"id"`

	// TaskID is the owning task identifier.
	TaskID string `json:"task_id"`

	// Status is the current container state.
	Status string `json:"status"`

	// Duration is elapsed time since creation in seconds.
	Duration float64 `json:"duration"`
}

// ActiveSandbox tracks a live sandbox container with its metadata and cancellation context.
// Protected by the global sandboxMu mutex for concurrent access safety.
type ActiveSandbox struct {
	// ContainerID is the full Docker container ID.
	ContainerID string

	// TaskID is the task that owns this sandbox.
	TaskID string

	// StartedAt records when the container was started.
	StartedAt time.Time

	// Timeout is the maximum duration this sandbox is allowed to run.
	Timeout time.Duration

	// Cancel is the context cancellation function for timeout enforcement.
	// Calling Cancel() signals the timeout goroutine to stop waiting.
	Cancel context.CancelFunc
}

// ErrorResponse is returned for all error conditions.
type ErrorResponse struct {
	// Error is a human-readable error message.
	Error string `json:"error"`

	// Code is the HTTP status code associated with this error.
	Code int `json:"code"`
}

// SandboxManager holds the global state for all active sandboxes.
// Access is protected by a read-write mutex for safe concurrent operation.
type SandboxManager struct {
	// mu protects the sandboxes map from concurrent access.
	mu sync.RWMutex

	// sandboxes maps sandbox IDs to their ActiveSandbox tracking data.
	sandboxes map[string]*ActiveSandbox

	// config holds the loaded configuration.
	config *Config
}

// NewSandboxManager creates a new SandboxManager with the given configuration.
func NewSandboxManager(cfg *Config) *SandboxManager {
	return &SandboxManager{
		sandboxes: make(map[string]*ActiveSandbox),
		config:    cfg,
	}
}
