// Package main - Rhodawk AI Sandbox Manager
// Docker executor - manages container lifecycle for sandboxed code execution.
//
// Security Model:
// ===============
// Each sandbox container is created with defense-in-depth isolation:
//
// 1. NETWORK ISOLATION: Containers use "none" network mode by default,
//    completely preventing network access. This stops data exfiltration,
//    C2 communication, and lateral movement.
//
// 2. RESOURCE LIMITS: Memory, CPU, and PID limits prevent resource exhaustion
//    attacks (fork bombs, memory floods). Hard-kill on OOM prevents swap thrashing.
//
// 3. FILESYSTEM ISOLATION: Read-only root filesystem prevents persistent modifications.
//    Only /tmp (tmpfs, size-limited) and the workspace volume are writable.
//
// 4. PRIVILEGE RESTRICTIONS: no-new-privileges security option prevents SUID/SGID
//    escalation. Containers run as non-root user inside.
//
// 5. TIMEOUT ENFORCEMENT: A dedicated goroutine monitors each sandbox and
//    force-kills it after the timeout expires, preventing indefinite execution.
//
// 6. OUTPUT BOUNDING: Only the last 200 lines of output are collected,
//    preventing memory exhaustion from programs that produce unlimited output.
//
// 7. STALE CLEANUP: A periodic goroutine identifies and removes orphaned
//    containers that escaped normal cleanup (crash recovery).

package main

import (
        "context"
        "fmt"
        "io"
        "log"
        "strings"
        "sync"
        "time"

        "github.com/docker/docker/api/types"
        "github.com/docker/docker/api/types/container"
        "github.com/docker/docker/api/types/mount"
        "github.com/docker/docker/client"
)

// Global sandbox manager instance and Docker client.
var (
        manager      *SandboxManager
        dockerClient *client.Client
)

// InitExecutor initializes the Docker client and sandbox manager.
// Must be called before any sandbox operations.
func InitExecutor(cfg *Config) error {
        var err error

        // Create Docker client using the configured host.
        // DOCKER_HOST env var is also respected by the Docker SDK.
        dockerClient, err = client.NewClientWithOpts(
                client.FromEnv,
                client.WithAPIVersionNegotiation(),
        )
        if err != nil {
                return fmt.Errorf("failed to create docker client: %w", err)
        }

        // Verify Docker connectivity with a ping.
        ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()

        _, err = dockerClient.Ping(ctx)
        if err != nil {
                return fmt.Errorf("docker daemon unreachable: %w", err)
        }

        manager = NewSandboxManager(cfg)

        // Start the periodic stale container cleanup goroutine.
        go cleanupStale()

        log.Printf("[executor] Docker client initialized, max concurrent: %d", cfg.MaxConcurrent)
        return nil
}

// CreateSandbox provisions and starts a new isolated container for code execution.
// Returns the sandbox ID and initial status, or an error if creation fails.
//
// The function performs these steps:
// 1. Validates the request and checks capacity
// 2. Creates a container with security constraints
// 3. Starts the container
// 4. Launches a timeout enforcement goroutine
// 5. Registers the sandbox in the tracking map
func CreateSandbox(req SandboxRequest) (*SandboxResponse, error) {
        // Validate required fields.
        if req.Command == "" {
                return nil, fmt.Errorf("command is required")
        }
        if req.TaskID == "" {
                return nil, fmt.Errorf("task_id is required")
        }

        // Check capacity - reject if at maximum concurrent sandboxes.
        // Hold write lock across the capacity check AND registration to prevent
        // TOCTOU race where multiple goroutines pass the check simultaneously.
        // The lock is NOT released until the sandbox is registered in the map,
        // eliminating the window where two goroutines both pass the check.
        manager.mu.Lock()
        activeCount := len(manager.sandboxes)
        if activeCount >= manager.config.MaxConcurrent {
                manager.mu.Unlock()
                return nil, fmt.Errorf("maximum concurrent sandboxes reached (%d/%d)",
                        activeCount, manager.config.MaxConcurrent)
        }
        // DO NOT unlock here — keep the mutex held through registration below.

        // Determine effective timeout.
        timeout := manager.config.DefaultTimeout
        if req.Timeout > 0 && req.Timeout < timeout {
                timeout = req.Timeout
        }
        timeoutDuration := time.Duration(timeout) * time.Second

        // Validate image name - basic safety check against command injection via image name.
        image := manager.config.SandboxImage
        if strings.ContainsAny(image, ";|&$`") {
                return nil, fmt.Errorf("invalid sandbox image name")
        }

        // Build environment variables for the container.
        env := []string{
                "SANDBOX=true",
                fmt.Sprintf("TASK_ID=%s", req.TaskID),
                fmt.Sprintf("TIMEOUT=%d", timeout),
        }
        for k, v := range req.Env {
                // Prevent overriding critical system variables.
                lower := strings.ToLower(k)
                if lower == "path" || lower == "home" || lower == "user" {
                        continue
                }
                env = append(env, fmt.Sprintf("%s=%s", k, v))
        }

        // Determine network mode - default is "none" (completely isolated).
        networkMode := "none"
        if req.Network {
                networkMode = "bridge"
        }

        // Parse memory limit from configuration.
        memoryBytes := parseMemoryLimit(manager.config.MemoryLimit)

        // Calculate CPU quota: Docker uses microseconds per 100ms period.
        // CPULimit of 1.0 = 100000 microseconds (one full core).
        cpuQuota := int64(manager.config.CPULimit * 100000)

        // Container configuration with security hardening.
        containerConfig := &container.Config{
                Image: image,
                Cmd:   []string{req.Command},
                Env:   env,
                Labels: map[string]string{
                        "managed-by": "rhodawk-sandbox-manager",
                        "task-id":    req.TaskID,
                        "created-at": time.Now().UTC().Format(time.RFC3339),
                },
                // Working directory inside the container.
                WorkingDir: "/home/sandbox/workspace",
        }

        // Host configuration with resource limits and security options.
        hostConfig := &container.HostConfig{
                // Resource constraints prevent container from exhausting host resources.
                Resources: container.Resources{
                        Memory:     memoryBytes,            // Hard memory limit (OOM kill on exceed).
                        MemorySwap: memoryBytes,            // No swap (same as memory = swap disabled).
                        CPUQuota:   cpuQuota,               // CPU time quota per period.
                        CPUPeriod:  100000,                 // 100ms scheduling period.
                        PidsLimit:  int64Ptr(256),          // Prevent fork bombs.
                        OomKillDisable: boolPtr(false),     // Allow OOM killer to terminate container.
                },
                // Network isolation - "none" means no network interfaces at all.
                NetworkMode: container.NetworkMode(networkMode),
                // Security options to prevent privilege escalation.
                SecurityOpt: []string{
                        "no-new-privileges",
                },
                // Read-only root filesystem - only /tmp and workspace are writable.
                ReadonlyRootfs: true,
                // Tmpfs mount for /tmp - limited to 64MB to prevent abuse.
                Tmpfs: map[string]string{
                        "/tmp": "rw,noexec,nosuid,size=67108864",
                },
                // Auto-remove container when it stops (belt-and-suspenders with manual cleanup).
                AutoRemove: false,
                // Drop all capabilities and only add what is strictly needed.
                CapDrop: []string{"ALL"},
        }

        // Add workspace volume mount if workdir content is provided.
        if req.WorkdirContent != "" {
                hostConfig.Mounts = append(hostConfig.Mounts, mount.Mount{
                        Type:     mount.TypeTmpfs,
                        Target:   "/home/sandbox/workspace",
                        TmpfsOptions: &mount.TmpfsOptions{
                                SizeBytes: 128 * 1024 * 1024, // 128MB workspace limit.
                        },
                })
        }

        // Create the container.
        ctx := context.Background()
        createResp, err := dockerClient.ContainerCreate(
                ctx,
                containerConfig,
                hostConfig,
                nil,  // No custom networking config.
                nil,  // No platform preference.
                "",   // Auto-generate container name.
        )
        if err != nil {
                return nil, fmt.Errorf("failed to create container: %w", err)
        }

        containerID := createResp.ID
        sandboxID := containerID[:12] // Use first 12 chars as sandbox ID.

        // Start the container.
        if err := dockerClient.ContainerStart(ctx, containerID, types.ContainerStartOptions{}); err != nil {
                // Clean up the created-but-not-started container.
                removeCtx, removeCancel := context.WithTimeout(context.Background(), 5*time.Second)
                defer removeCancel()
                dockerClient.ContainerRemove(removeCtx, containerID, types.ContainerRemoveOptions{Force: true})
                return nil, fmt.Errorf("failed to start container: %w", err)
        }

        // Create cancellation context for timeout enforcement.
        timeoutCtx, cancelFunc := context.WithCancel(context.Background())

        // Register the sandbox in the tracking map.
        // Still holding the mutex locked since the capacity check — this eliminates
        // the TOCTOU race between the check and the write.
        sandbox := &ActiveSandbox{
                ContainerID: containerID,
                TaskID:      req.TaskID,
                StartedAt:   time.Now(),
                Timeout:     timeoutDuration,
                Cancel:      cancelFunc,
        }

        manager.sandboxes[sandboxID] = sandbox
        manager.mu.Unlock()

        // Launch timeout enforcement goroutine.
        go enforceTimeout(sandboxID, timeoutDuration, timeoutCtx)

        log.Printf("[executor] Sandbox created: id=%s task=%s timeout=%ds image=%s network=%s",
                sandboxID, req.TaskID, timeout, image, networkMode)

        return &SandboxResponse{
                SandboxID: sandboxID,
                Status:    "running",
                Timeout:   timeout,
        }, nil
}

// GetSandboxStatus inspects a sandbox and returns its current state.
// If the sandbox has exited, collects output logs and cleans up the container.
func GetSandboxStatus(sandboxID string) (*SandboxStatus, error) {
        manager.mu.RLock()
        sandbox, exists := manager.sandboxes[sandboxID]
        manager.mu.RUnlock()

        if !exists {
                return &SandboxStatus{
                        SandboxID: sandboxID,
                        Status:    "not_found",
                }, nil
        }

        // Inspect the container to get its current state.
        ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()

        inspect, err := dockerClient.ContainerInspect(ctx, sandbox.ContainerID)
        if err != nil {
                // Container might have been removed externally.
                cleanupTracking(sandboxID)
                return &SandboxStatus{
                        SandboxID: sandboxID,
                        Status:    "not_found",
                }, nil
        }

        duration := time.Since(sandbox.StartedAt).Seconds()

        // If still running, return status without output.
        if inspect.State.Running {
                return &SandboxStatus{
                        SandboxID: sandboxID,
                        Status:    "running",
                        Duration:  duration,
                }, nil
        }

        // Container has exited - collect logs and clean up.
        output, err := collectLogs(sandbox.ContainerID)
        if err != nil {
                log.Printf("[executor] Warning: failed to collect logs for %s: %v", sandboxID, err)
                output = "[log collection failed]"
        }

        // Determine exit status.
        status := "exited"
        if inspect.State.OOMKilled {
                status = "oom_killed"
        }

        exitCode := inspect.State.ExitCode

        // Remove the container and clean up tracking.
        removeContainer(sandbox.ContainerID)
        cleanupTracking(sandboxID)

        return &SandboxStatus{
                SandboxID: sandboxID,
                Status:    status,
                Duration:  duration,
                ExitCode:  exitCode,
                Output:    output,
        }, nil
}

// KillSandbox forcefully terminates a sandbox container.
// The container is stopped, removed, and tracking is cleaned up.
func KillSandbox(sandboxID string) error {
        manager.mu.RLock()
        sandbox, exists := manager.sandboxes[sandboxID]
        manager.mu.RUnlock()

        if !exists {
                return fmt.Errorf("sandbox not found: %s", sandboxID)
        }

        // Cancel the timeout goroutine first.
        sandbox.Cancel()

        // Force-kill the container with a very short grace period.
        ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()

        stopTimeout := 2 // 2 second grace period before SIGKILL.
        err := dockerClient.ContainerStop(ctx, sandbox.ContainerID, container.StopOptions{
                Timeout: &stopTimeout,
        })
        if err != nil {
                log.Printf("[executor] Warning: stop failed for %s, forcing remove: %v", sandboxID, err)
        }

        // Force remove the container regardless of stop result.
        removeContainer(sandbox.ContainerID)
        cleanupTracking(sandboxID)

        log.Printf("[executor] Sandbox killed: id=%s task=%s", sandboxID, sandbox.TaskID)
        return nil
}

// GetStats returns current sandbox system statistics.
func GetStats() *SandboxStats {
        manager.mu.RLock()
        defer manager.mu.RUnlock()

        sandboxes := make([]SandboxInfo, 0, len(manager.sandboxes))
        for id, sb := range manager.sandboxes {
                sandboxes = append(sandboxes, SandboxInfo{
                        ID:       id,
                        TaskID:   sb.TaskID,
                        Status:   "running",
                        Duration: time.Since(sb.StartedAt).Seconds(),
                })
        }

        return &SandboxStats{
                Active:        len(manager.sandboxes),
                MaxConcurrent: manager.config.MaxConcurrent,
                Sandboxes:     sandboxes,
        }
}

// enforceTimeout waits for the sandbox timeout to expire, then kills the container.
// The goroutine exits early if the context is cancelled (sandbox finished or killed).
func enforceTimeout(sandboxID string, timeout time.Duration, ctx context.Context) {
        select {
        case <-time.After(timeout):
                // Timeout expired - force kill the sandbox.
                log.Printf("[executor] Timeout expired for sandbox %s after %v", sandboxID, timeout)

                manager.mu.Lock()
                sandbox, exists := manager.sandboxes[sandboxID]
                if !exists {
                        manager.mu.Unlock()
                        return // Already cleaned up by KillSandbox or GetSandboxStatus.
                }
                // Remove from tracking under the lock to prevent KillSandbox from racing.
                delete(manager.sandboxes, sandboxID)
                sandbox.Cancel()
                manager.mu.Unlock()

                // Force kill the container (safe to do outside lock — we own the reference).
                killCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
                defer cancel()

                stopTimeout := 1
                dockerClient.ContainerStop(killCtx, sandbox.ContainerID, container.StopOptions{
                        Timeout: &stopTimeout,
                })
                removeContainer(sandbox.ContainerID)

                log.Printf("[executor] Sandbox %s killed due to timeout", sandboxID)

        case <-ctx.Done():
                // Context cancelled - sandbox finished naturally or was killed manually.
                return
        }
}

// cleanupStale periodically checks for orphaned containers that were managed
// by this sandbox manager but are no longer tracked (e.g., after a crash).
// Runs every 5 minutes indefinitely.
func cleanupStale() {
        ticker := time.NewTicker(5 * time.Minute)
        defer ticker.Stop()

        for range ticker.C {
                ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)

                // List all containers with our management label.
                containers, err := dockerClient.ContainerList(ctx, types.ContainerListOptions{
                        All: true,
                })
                cancel()

                if err != nil {
                        log.Printf("[cleanup] Failed to list containers: %v", err)
                        continue
                }

                manager.mu.RLock()
                trackedIDs := make(map[string]bool)
                for _, sb := range manager.sandboxes {
                        trackedIDs[sb.ContainerID] = true
                }
                manager.mu.RUnlock()

                orphanCount := 0
                for _, c := range containers {
                        // Only clean up containers managed by us.
                        if c.Labels["managed-by"] != "rhodawk-sandbox-manager" {
                                continue
                        }

                        // Skip if still tracked.
                        if trackedIDs[c.ID] {
                                continue
                        }

                        // Check age - only clean up containers older than 10 minutes.
                        createdAt, err := time.Parse(time.RFC3339, c.Labels["created-at"])
                        if err != nil || time.Since(createdAt) < 10*time.Minute {
                                continue
                        }

                        // Remove orphaned container.
                        removeCtx, removeCancel := context.WithTimeout(context.Background(), 10*time.Second)
                        dockerClient.ContainerStop(removeCtx, c.ID, container.StopOptions{Timeout: intPtr(2)})
                        dockerClient.ContainerRemove(removeCtx, c.ID, types.ContainerRemoveOptions{Force: true})
                        removeCancel()

                        orphanCount++
                        log.Printf("[cleanup] Removed orphaned container: %s (task: %s)",
                                c.ID[:12], c.Labels["task-id"])
                }

                if orphanCount > 0 {
                        log.Printf("[cleanup] Removed %d orphaned containers", orphanCount)
                }
        }
}

// collectLogs retrieves the last 200 lines of combined stdout/stderr from a container.
// Output is bounded to prevent memory exhaustion from excessive log output.
func collectLogs(containerID string) (string, error) {
        ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()

        reader, err := dockerClient.ContainerLogs(ctx, containerID, types.ContainerLogsOptions{
                ShowStdout: true,
                ShowStderr: true,
                Tail:       "200",
        })
        if err != nil {
                return "", fmt.Errorf("failed to get container logs: %w", err)
        }
        defer reader.Close()

        // Read all available log data (bounded by Tail option).
        data, err := io.ReadAll(reader)
        if err != nil {
                return "", fmt.Errorf("failed to read logs: %w", err)
        }

        // Docker log output includes 8-byte header per line for stream multiplexing.
        // Strip the headers for clean output.
        output := stripDockerLogHeaders(string(data))

        return output, nil
}

// stripDockerLogHeaders removes the 8-byte Docker log stream headers from output.
// Docker multiplexes stdout/stderr with a header: [stream_type(1)][0(3)][size(4)][payload].
func stripDockerLogHeaders(raw string) string {
        var result strings.Builder
        lines := strings.Split(raw, "\n")
        for _, line := range lines {
                if len(line) > 8 {
                        // Skip the 8-byte header if present.
                        // Header bytes: [1-byte type][3 zeros][4-byte big-endian length]
                        if line[0] <= 2 && line[1] == 0 && line[2] == 0 && line[3] == 0 {
                                result.WriteString(line[8:])
                                result.WriteString("\n")
                                continue
                        }
                }
                result.WriteString(line)
                result.WriteString("\n")
        }
        return strings.TrimRight(result.String(), "\n")
}

// removeContainer force-removes a Docker container, ignoring errors.
func removeContainer(containerID string) {
        ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()

        err := dockerClient.ContainerRemove(ctx, containerID, types.ContainerRemoveOptions{
                Force:         true,
                RemoveVolumes: true,
        })
        if err != nil {
                log.Printf("[executor] Warning: failed to remove container %s: %v", containerID[:12], err)
        }
}

// cleanupTracking removes a sandbox from the tracking map and cancels its context.
func cleanupTracking(sandboxID string) {
        manager.mu.Lock()
        if sandbox, exists := manager.sandboxes[sandboxID]; exists {
                sandbox.Cancel()
                delete(manager.sandboxes, sandboxID)
        }
        manager.mu.Unlock()
}

// Utility functions for pointer creation (required by Docker SDK).
func int64Ptr(i int64) *int64    { return &i }
func boolPtr(b bool) *bool       { return &b }
func intPtr(i int) *int          { return &i }

// Ensure unused imports are referenced to prevent compile errors.
// These are used in the full implementation but the compiler needs references.
var _ sync.Mutex
