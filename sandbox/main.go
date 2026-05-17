// Package main - Rhodawk AI Sandbox Manager
//
// HTTP server providing a REST API for ephemeral Docker container management.
// This service is the execution layer of the Hermes88 AI assistant system,
// allowing untrusted code to run in fully isolated containers with strict
// resource limits, network isolation, and automatic timeout enforcement.
//
// Architecture:
// - Standard library net/http (no external router dependencies)
// - Graceful shutdown on SIGTERM/SIGINT with 30-second drain timeout
// - Request logging middleware for observability
// - JSON API with consistent error response format
//
// Routes:
// - POST   /sandbox/create     - Create and start a new sandbox
// - GET    /sandbox/{id}/status - Get sandbox status and output
// - POST   /sandbox/{id}/kill   - Force-kill a running sandbox
// - GET    /sandbox/stats       - System utilization statistics
// - GET    /health              - Health check endpoint
//
// Copyright (c) 2024 Rhodawk AI - All rights reserved.

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

const (
	// version is the current build version of the sandbox manager.
	version = "1.0.0"

	// shutdownTimeout is the maximum time to wait for in-flight requests during shutdown.
	shutdownTimeout = 30 * time.Second
)

func main() {
	// Print startup banner with Rhodawk AI branding.
	printBanner()

	// Load configuration from environment variables.
	cfg := LoadConfig()
	log.Printf("[main] Configuration loaded: %s", cfg.String())

	// Initialize the Docker executor and sandbox manager.
	if err := InitExecutor(cfg); err != nil {
		log.Fatalf("[main] FATAL: Failed to initialize executor: %v", err)
	}
	log.Printf("[main] Docker executor initialized successfully")

	// Set up HTTP routes using the standard library multiplexer.
	mux := http.NewServeMux()

	// Register route handlers.
	mux.HandleFunc("/sandbox/create", handleCreateSandbox)
	mux.HandleFunc("/sandbox/stats", handleGetStats)
	mux.HandleFunc("/sandbox/", handleSandboxRoutes) // Catches /sandbox/{id}/status and /sandbox/{id}/kill
	mux.HandleFunc("/health", handleHealth)

	// Wrap with logging middleware.
	handler := loggingMiddleware(mux)

	// Create the HTTP server with timeouts to prevent slowloris attacks.
	server := &http.Server{
		Addr:         fmt.Sprintf(":%d", cfg.Port),
		Handler:      handler,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 60 * time.Second, // Longer write timeout for log streaming.
		IdleTimeout:  120 * time.Second,
	}

	// Start the server in a goroutine.
	go func() {
		log.Printf("[main] Sandbox Manager listening on port %d", cfg.Port)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("[main] FATAL: Server failed: %v", err)
		}
	}()

	// Wait for shutdown signal (SIGTERM or SIGINT).
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGTERM, syscall.SIGINT)
	sig := <-quit
	log.Printf("[main] Received signal %s, initiating graceful shutdown...", sig)

	// Graceful shutdown with timeout.
	ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()

	if err := server.Shutdown(ctx); err != nil {
		log.Printf("[main] WARNING: Server forced to shutdown: %v", err)
	}

	log.Printf("[main] Sandbox Manager shut down gracefully")
}

// printBanner displays the Rhodawk AI startup banner.
func printBanner() {
	banner := `
============================================================
  RHODAWK AI - Sandbox Manager v%s
  Ephemeral Container Execution Engine
  Part of the Hermes88 JARVIS-Grade AI System
============================================================
`
	fmt.Printf(banner, version)
}

// handleSandboxRoutes routes /sandbox/{id}/status and /sandbox/{id}/kill requests.
// Uses path parsing since we are using the standard library mux without pattern variables.
func handleSandboxRoutes(w http.ResponseWriter, r *http.Request) {
	// Parse the path: /sandbox/{id}/status or /sandbox/{id}/kill
	path := strings.TrimPrefix(r.URL.Path, "/sandbox/")
	parts := strings.Split(path, "/")

	if len(parts) < 2 {
		writeError(w, http.StatusNotFound, "invalid route")
		return
	}

	sandboxID := parts[0]
	action := parts[1]

	switch action {
	case "status":
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "GET required")
			return
		}
		handleGetStatus(w, r, sandboxID)
	case "kill":
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "POST required")
			return
		}
		handleKillSandbox(w, r, sandboxID)
	default:
		writeError(w, http.StatusNotFound, "unknown action: "+action)
	}
}

// handleCreateSandbox handles POST /sandbox/create requests.
// Validates the request body, creates a new sandbox, and returns its ID.
func handleCreateSandbox(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "POST required")
		return
	}

	// Decode the JSON request body.
	var req SandboxRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	defer r.Body.Close()

	// Validate required fields.
	if req.Command == "" {
		writeError(w, http.StatusBadRequest, "command is required")
		return
	}
	if req.TaskID == "" {
		writeError(w, http.StatusBadRequest, "task_id is required")
		return
	}

	// Create the sandbox.
	resp, err := CreateSandbox(req)
	if err != nil {
		// Check for capacity errors vs internal errors.
		if strings.Contains(err.Error(), "maximum concurrent") {
			writeError(w, http.StatusTooManyRequests, err.Error())
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	writeJSON(w, http.StatusCreated, resp)
}

// handleGetStatus handles GET /sandbox/{id}/status requests.
// Returns the current state of the sandbox, including output if completed.
func handleGetStatus(w http.ResponseWriter, r *http.Request, sandboxID string) {
	if sandboxID == "" {
		writeError(w, http.StatusBadRequest, "sandbox_id is required")
		return
	}

	status, err := GetSandboxStatus(sandboxID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	if status.Status == "not_found" {
		writeError(w, http.StatusNotFound, "sandbox not found: "+sandboxID)
		return
	}

	writeJSON(w, http.StatusOK, status)
}

// handleKillSandbox handles POST /sandbox/{id}/kill requests.
// Force-terminates the specified sandbox.
func handleKillSandbox(w http.ResponseWriter, r *http.Request, sandboxID string) {
	if sandboxID == "" {
		writeError(w, http.StatusBadRequest, "sandbox_id is required")
		return
	}

	err := KillSandbox(sandboxID)
	if err != nil {
		if strings.Contains(err.Error(), "not found") {
			writeError(w, http.StatusNotFound, err.Error())
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{
		"status":  "killed",
		"sandbox_id": sandboxID,
	})
}

// handleGetStats handles GET /sandbox/stats requests.
// Returns system utilization statistics.
func handleGetStats(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "GET required")
		return
	}

	stats := GetStats()
	writeJSON(w, http.StatusOK, stats)
}

// handleHealth handles GET /health requests.
// Returns a simple health check response with version and uptime.
func handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "GET required")
		return
	}

	health := map[string]interface{}{
		"status":  "healthy",
		"version": version,
		"service": "rhodawk-sandbox-manager",
		"uptime":  time.Since(startTime).String(),
	}

	writeJSON(w, http.StatusOK, health)
}

// startTime records when the server started, used for uptime reporting.
var startTime = time.Now()

// loggingMiddleware wraps an HTTP handler with request/response logging.
// Logs method, path, status code, duration, and client IP for every request.
func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()

		// Wrap the response writer to capture the status code.
		wrapped := &responseWriter{ResponseWriter: w, statusCode: http.StatusOK}

		// Call the next handler.
		next.ServeHTTP(wrapped, r)

		// Log the request.
		duration := time.Since(start)
		log.Printf("[http] %s %s %d %v %s",
			r.Method,
			r.URL.Path,
			wrapped.statusCode,
			duration,
			r.RemoteAddr,
		)
	})
}

// responseWriter wraps http.ResponseWriter to capture the status code.
type responseWriter struct {
	http.ResponseWriter
	statusCode int
}

// WriteHeader captures the status code before writing it.
func (rw *responseWriter) WriteHeader(code int) {
	rw.statusCode = code
	rw.ResponseWriter.WriteHeader(code)
}

// writeJSON serializes data as JSON and writes it to the response with the given status code.
func writeJSON(w http.ResponseWriter, statusCode int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)

	if err := json.NewEncoder(w).Encode(data); err != nil {
		log.Printf("[http] WARNING: Failed to encode JSON response: %v", err)
	}
}

// writeError writes a standardized JSON error response.
func writeError(w http.ResponseWriter, statusCode int, message string) {
	writeJSON(w, statusCode, ErrorResponse{
		Error: message,
		Code:  statusCode,
	})
}
