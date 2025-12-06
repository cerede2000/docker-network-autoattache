package main

import (
	"context"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/events"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
)

const (
	defaultLabelPrefix             = "managed.network."
	defaultDisconnectOthersKey     = "disconnectothers"
	defaultInternalKey             = "internal"
	defaultReconciliationInterval  = 30 * time.Second
	defaultDisconnectOthersDefault = false
	defaultRestartOnNetworkChange  = true
	defaultRestartTimeout          = 1
	defaultHealthCheckPort         = 8080
)

type NetworkManager struct {
	client                  *client.Client
	labelPrefix             string
	disconnectOthersKey     string
	internalKey             string
	reconciliationInterval  time.Duration
	disconnectOthersDefault bool
	restartOnNetworkChange  bool
	restartTimeout          int
	healthCheckPort         int
	lastEventTime           atomic.Int64
	isHealthy               atomic.Bool
	mu                      sync.RWMutex
	managedContainers       map[string]bool
	networkInternalState    map[string]bool
	reconciliationMutex     sync.Mutex
}

func main() {
	log.Println("Starting Docker Network Manager...")

	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		log.Fatalf("Failed to create Docker client: %v", err)
	}
	defer cli.Close()

	labelPrefix := getEnv("LABEL_PREFIX", defaultLabelPrefix)
	disconnectOthersKey := getEnv("DISCONNECT_OTHERS_KEY", defaultDisconnectOthersKey)
	internalKey := getEnv("INTERNAL_KEY", defaultInternalKey)
	reconciliationInterval := getDurationEnv("RECONCILIATION_INTERVAL", defaultReconciliationInterval)
	disconnectOthersDefault := getBoolEnv("DISCONNECT_OTHERS_DEFAULT", defaultDisconnectOthersDefault)
	restartOnNetworkChange := getBoolEnv("RESTART_ON_NETWORK_CHANGE", defaultRestartOnNetworkChange)
	restartTimeout := getIntEnv("RESTART_TIMEOUT", defaultRestartTimeout)
	healthCheckPort := getIntEnv("HEALTHCHECK_PORT", defaultHealthCheckPort)

	log.Printf("Configuration:")
	log.Printf("  Label prefix: %s", labelPrefix)
	log.Printf("  Disconnect others label: %s%s", labelPrefix, disconnectOthersKey)
	log.Printf("  Disconnect others default: %v", disconnectOthersDefault)
	log.Printf("  Internal network suffix: .%s", internalKey)
	log.Printf("  Reconciliation interval: %v", reconciliationInterval)
	log.Printf("  Restart on network change: %v", restartOnNetworkChange)
	log.Printf("  Restart timeout: %ds", restartTimeout)
	log.Printf("  Healthcheck port: %d", healthCheckPort)

	manager := &NetworkManager{
		client:                  cli,
		labelPrefix:             labelPrefix,
		disconnectOthersKey:     disconnectOthersKey,
		internalKey:             internalKey,
		reconciliationInterval:  reconciliationInterval,
		disconnectOthersDefault: disconnectOthersDefault,
		restartOnNetworkChange:  restartOnNetworkChange,
		restartTimeout:          restartTimeout,
		healthCheckPort:         healthCheckPort,
		managedContainers:       make(map[string]bool),
		networkInternalState:    make(map[string]bool),
	}

	manager.isHealthy.Store(true)
	manager.lastEventTime.Store(time.Now().Unix())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)

	go func() {
		<-sigChan
		log.Println("Received shutdown signal, stopping...")
		manager.isHealthy.Store(false)
		cancel()
	}()

	go manager.startHealthCheckServer()

	log.Println("Performing initial reconciliation...")
	runID := generateRunID()
	if err := manager.reconcileAllContainers(ctx, runID); err != nil {
		log.Printf("[%s] Warning: Initial reconciliation failed: %v", runID, err)
		manager.isHealthy.Store(false)
	}

	go manager.periodicReconciliation(ctx)

	log.Println("Starting event watcher...")
	if err := manager.watchEvents(ctx); err != nil {
		log.Fatalf("Event watcher failed: %v", err)
	}

	log.Println("Docker Network Manager stopped")
}

func (m *NetworkManager) startHealthCheckServer() {
	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		if !m.isHealthy.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintf(w, `{"status":"unhealthy","message":"Service is not running properly"}`)
			return
		}

		lastEvent := m.lastEventTime.Load()
		timeSinceLastEvent := time.Since(time.Unix(lastEvent, 0))

		if lastEvent > 0 && timeSinceLastEvent > 5*time.Minute {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintf(w, `{"status":"unhealthy","message":"No events received for %v"}`, timeSinceLastEvent)
			return
		}

		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		_, err := m.client.Ping(ctx)
		if err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintf(w, `{"status":"unhealthy","message":"Docker connection failed: %v"}`, err)
			return
		}

		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, `{"status":"healthy","last_event":"%v"}`, time.Unix(lastEvent, 0).Format(time.RFC3339))
	})

	mux.HandleFunc("/ready", func(w http.ResponseWriter, r *http.Request) {
		if m.isHealthy.Load() {
			w.WriteHeader(http.StatusOK)
			fmt.Fprintf(w, `{"status":"ready"}`)
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintf(w, `{"status":"not ready"}`)
		}
	})

	mux.HandleFunc("/live", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, `{"status":"alive"}`)
	})

	addr := fmt.Sprintf(":%d", m.healthCheckPort)
	log.Printf("Starting healthcheck server on %s", addr)

	server := &http.Server{
		Addr:    addr,
		Handler: mux,
	}

	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Printf("Healthcheck server error: %v", err)
		m.isHealthy.Store(false)
	}
}

func (m *NetworkManager) periodicReconciliation(ctx context.Context) {
	ticker := time.NewTicker(m.reconciliationInterval)
	defer ticker.Stop()

	log.Printf("Starting periodic reconciliation loop (every %v)", m.reconciliationInterval)

	for {
		select {
		case <-ticker.C:
			runID := generateRunID()
			log.Printf("[%s] Running periodic reconciliation...", runID)
			if err := m.reconcileAllContainers(ctx, runID); err != nil {
				log.Printf("[%s] Periodic reconciliation error: %v", runID, err)
			}
		case <-ctx.Done():
			log.Println("Stopping periodic reconciliation")
			return
		}
	}
}

func (m *NetworkManager) reconcileAllContainers(ctx context.Context, runID string) error {
	m.reconciliationMutex.Lock()
	defer m.reconciliationMutex.Unlock()

	containers, err := m.client.ContainerList(ctx, container.ListOptions{})
	if err != nil {
		return fmt.Errorf("failed to list containers: %w", err)
	}

	log.Printf("[%s] Found %d containers to check", runID, len(containers))

	networkRequirements := make(map[string]bool)

	for _, c := range containers {
		containerInfo, err := m.client.ContainerInspect(ctx, c.ID)
		if err != nil || !containerInfo.State.Running {
			continue
		}

		labels := containerInfo.Config.Labels
		networkLabels := m.extractNetworkLabelsWithInternal(labels)

		for netName, isInternal := range networkLabels {
			if existing, exists := networkRequirements[netName]; exists {
				if existing != isInternal {
					log.Printf("[%s] ⚠️  CONFLICT: Network %s requested as internal=%v and internal=%v - choosing internal=true for security",
						runID, netName, existing, isInternal)
					networkRequirements[netName] = true
				}
			} else {
				networkRequirements[netName] = isInternal
			}
		}
	}

	m.mu.Lock()
	m.networkInternalState = networkRequirements
	m.mu.Unlock()

	managedCount := 0
	skippedCount := 0
	errorCount := 0

	for _, c := range containers {
		if err := m.reconcileContainer(ctx, c.ID, runID); err != nil {
			log.Printf("[%s] Error reconciling container %s: %v", runID, c.ID[:12], err)
			errorCount++
		} else {
			m.mu.RLock()
			if m.managedContainers[c.ID] {
				managedCount++
			} else {
				skippedCount++
			}
			m.mu.RUnlock()
		}
	}

	log.Printf("[%s] Reconciliation complete: %d managed, %d skipped, %d errors",
		runID, managedCount, skippedCount, errorCount)

	return nil
}

func (m *NetworkManager) watchEvents(ctx context.Context) error {
	filter := filters.NewArgs()
	filter.Add("type", "container")
	filter.Add("event", "start")
	filter.Add("event", "die")
	filter.Add("event", "update")

	eventsChan, errChan := m.client.Events(ctx, events.ListOptions{
		Filters: filter,
	})

	for {
		select {
		case event := <-eventsChan:
			go m.handleEvent(ctx, event)
		case err := <-errChan:
			if err != nil {
				return fmt.Errorf("event stream error: %w", err)
			}
		case <-ctx.Done():
			return nil
		}
	}
}

func (m *NetworkManager) handleEvent(ctx context.Context, event events.Message) {
	runID := generateRunID()

	m.lastEventTime.Store(time.Now().Unix())

	switch event.Action {
	case "start", "update":
		time.Sleep(150 * time.Millisecond)

		containerInfo, err := m.client.ContainerInspect(ctx, event.ID)
		containerName := event.ID[:12]
		if err == nil && containerInfo.Name != "" {
			containerName = containerInfo.Name
		}

		log.Printf("[%s] Event: %s for container %s (%s)", runID, event.Action, containerName, event.ID[:12])

		if err := m.reconcileContainerWithConflictDetection(ctx, event.ID, runID); err != nil {
			log.Printf("[%s] Error reconciling container %s (%s): %v", runID, containerName, event.ID[:12], err)
		}

	case "die":
		containerInfo, err := m.client.ContainerInspect(ctx, event.ID)
		containerName := event.ID[:12]
		if err == nil && containerInfo.Name != "" {
			containerName = containerInfo.Name
		}

		m.mu.Lock()
		delete(m.managedContainers, event.ID)
		m.mu.Unlock()

		log.Printf("[%s] Container %s (%s) stopped, removed from managed list",
			runID, containerName, event.ID[:12])
	}
}

func (m *NetworkManager) reconcileContainerWithConflictDetection(ctx context.Context, containerID string, runID string) error {
	m.reconciliationMutex.Lock()
	defer m.reconciliationMutex.Unlock()

	containerInfo, err := m.client.ContainerInspect(ctx, containerID)
	if err != nil {
		return fmt.Errorf("failed to inspect container: %w", err)
	}

	containerName := containerInfo.Name
	shortID := containerID[:12]

	if !containerInfo.State.Running {
		return nil
	}

	labels := containerInfo.Config.Labels
	networkLabels := m.extractNetworkLabelsWithInternal(labels)

	if len(networkLabels) == 0 {
		m.mu.Lock()
		delete(m.managedContainers, containerID)
		m.mu.Unlock()
		return nil
	}

	hasConflict := false
	m.mu.RLock()
	for netName, wantInternal := range networkLabels {
		if existingInternal, exists := m.networkInternalState[netName]; exists {
			if existingInternal != wantInternal {
				log.Printf("[%s] ⚠️  CONFLICT detected for network %s: existing=%v, requested=%v (container: %s)",
					runID, netName, existingInternal, wantInternal, containerName)
				hasConflict = true
			}
		}
	}
	m.mu.RUnlock()

	if hasConflict {
		log.Printf("[%s] Conflict detected for container %s (%s), triggering full reconciliation...",
			runID, containerName, shortID)

		m.reconciliationMutex.Unlock()
		err := m.reconcileAllContainers(ctx, runID)
		m.reconciliationMutex.Lock()
		return err
	}

	m.mu.Lock()
	for netName, isInternal := range networkLabels {
		m.networkInternalState[netName] = isInternal
	}
	m.mu.Unlock()

	return m.reconcileContainer(ctx, containerID, runID)
}

func (m *NetworkManager) reconcileContainer(ctx context.Context, containerID string, runID string) error {
	containerInfo, err := m.client.ContainerInspect(ctx, containerID)
	if err != nil {
		return fmt.Errorf("failed to inspect container: %w", err)
	}

	containerName := containerInfo.Name
	shortID := containerID[:12]

	if !containerInfo.State.Running {
		return nil
	}

	labels := containerInfo.Config.Labels
	networkLabels := m.extractNetworkLabelsWithInternal(labels)

	if len(networkLabels) == 0 {
		m.mu.Lock()
		delete(m.managedContainers, containerID)
		m.mu.Unlock()
		return nil
	}

	m.mu.RLock()
	resolvedNetworks := make(map[string]bool)
	for netName := range networkLabels {
		if internalState, exists := m.networkInternalState[netName]; exists {
			resolvedNetworks[netName] = internalState
		} else {
			resolvedNetworks[netName] = networkLabels[netName]
		}
	}
	m.mu.RUnlock()

	log.Printf("[%s] Managing container %s (%s) - Networks: %v",
		runID, containerName, shortID, getNetworkNames(resolvedNetworks))

	m.mu.Lock()
	m.managedContainers[containerID] = true
	m.mu.Unlock()

	shouldDisconnectOthers := m.shouldDisconnectOthers(labels)

	currentNetworks := make(map[string]bool)
	for netName := range containerInfo.NetworkSettings.Networks {
		currentNetworks[netName] = true
	}

	networksChanged := false

	for netName, isInternal := range resolvedNetworks {
		if err := m.ensureNetworkExistsWithInternalFlag(ctx, netName, isInternal, runID); err != nil {
			log.Printf("[%s] Error ensuring network %s exists: %v", runID, netName, err)
			continue
		}

		if !currentNetworks[netName] {
			log.Printf("[%s] Connecting container %s (%s) to network %s",
				runID, containerName, shortID, netName)
			if err := m.client.NetworkConnect(ctx, netName, containerID, nil); err != nil {
				if !strings.Contains(err.Error(), "already exists in network") {
					log.Printf("[%s] Error connecting container %s (%s) to network %s: %v",
						runID, containerName, shortID, netName, err)
				}
			} else {
				networksChanged = true
			}
		}

		delete(currentNetworks, netName)
	}

	if shouldDisconnectOthers {
		for netName := range currentNetworks {
			log.Printf("[%s] Disconnecting container %s (%s) from unmanaged network %s",
				runID, containerName, shortID, netName)
			if err := m.client.NetworkDisconnect(ctx, netName, containerID, false); err != nil {
				if !strings.Contains(err.Error(), "is not connected to") {
					log.Printf("[%s] Error disconnecting container %s (%s) from network %s: %v",
						runID, containerName, shortID, netName, err)
				}
			} else {
				networksChanged = true
			}
		}
	}

	if networksChanged {
		traefikEnabled := strings.ToLower(labels["traefik.enable"]) == "true"

		if traefikEnabled && m.restartOnNetworkChange {
			log.Printf("[%s] Networks changed for container %s (%s) with Traefik enabled, performing quick restart",
				runID, containerName, shortID)

			timeout := m.restartTimeout
			if err := m.client.ContainerRestart(ctx, containerID, container.StopOptions{Timeout: &timeout}); err != nil {
				log.Printf("[%s] Warning: Failed to restart container %s (%s): %v",
					runID, containerName, shortID, err)
			} else {
				log.Printf("[%s] Successfully restarted container %s (%s) - Traefik will rescan",
					runID, containerName, shortID)
			}
		} else {
			log.Printf("[%s] Networks changed for container %s (%s)",
				runID, containerName, shortID)
		}
	}

	return nil
}

func (m *NetworkManager) extractNetworkLabelsWithInternal(labels map[string]string) map[string]bool {
	networks := make(map[string]bool)

	for key, value := range labels {
		if !strings.HasPrefix(key, m.labelPrefix) {
			continue
		}

		suffix := strings.TrimPrefix(key, m.labelPrefix)

		if suffix == m.disconnectOthersKey {
			continue
		}

		if strings.HasSuffix(suffix, "."+m.internalKey) {
			netName := strings.TrimSuffix(suffix, "."+m.internalKey)
			if strings.ToLower(value) == "true" {
				networks[netName] = true
			}
			continue
		}

		if strings.ToLower(value) == "true" {
			if _, exists := networks[suffix]; !exists {
				networks[suffix] = false
			}
		}
	}

	return networks
}

func (m *NetworkManager) shouldDisconnectOthers(labels map[string]string) bool {
	key := m.labelPrefix + m.disconnectOthersKey
	value, exists := labels[key]

	if exists {
		return strings.ToLower(value) == "true"
	}

	return m.disconnectOthersDefault
}

func (m *NetworkManager) ensureNetworkExistsWithInternalFlag(ctx context.Context, networkName string, shouldBeInternal bool, runID string) error {
	networks, err := m.client.NetworkList(ctx, network.ListOptions{
		Filters: filters.NewArgs(filters.Arg("name", networkName)),
	})
	if err != nil {
		return fmt.Errorf("failed to list networks: %w", err)
	}

	var existingNetwork *network.Summary
	for i, net := range networks {
		if net.Name == networkName {
			existingNetwork = &networks[i]
			break
		}
	}

	if existingNetwork == nil {
		log.Printf("[%s] Creating network %s (internal: %v)", runID, networkName, shouldBeInternal)

		_, err = m.client.NetworkCreate(ctx, networkName, network.CreateOptions{
			Driver:   "bridge",
			Internal: shouldBeInternal,
			Labels: map[string]string{
				"managed-by":                 "docker-network-manager",
				"com.docker.compose.network": networkName,
			},
		})

		if err != nil {
			return fmt.Errorf("failed to create network: %w", err)
		}
		return nil
	}

	currentlyInternal := existingNetwork.Internal

	if currentlyInternal != shouldBeInternal {
		log.Printf("[%s] Network %s needs internal flag conversion: %v -> %v",
			runID, networkName, currentlyInternal, shouldBeInternal)

		netDetails, err := m.client.NetworkInspect(ctx, existingNetwork.ID, network.InspectOptions{})
		if err != nil {
			return fmt.Errorf("failed to inspect network: %w", err)
		}

		containersToReconnect := make([]string, 0)
		for containerID := range netDetails.Containers {
			containersToReconnect = append(containersToReconnect, containerID)
		}

		if len(containersToReconnect) > 0 {
			log.Printf("[%s] Network %s has %d connected containers, preparing safe conversion...",
				runID, networkName, len(containersToReconnect))

			tempNetworkName := "temp-safety-" + networkName
			log.Printf("[%s] Creating temporary safety network: %s", runID, tempNetworkName)

			tempNet, err := m.client.NetworkCreate(ctx, tempNetworkName, network.CreateOptions{
				Driver: "bridge",
				Labels: map[string]string{
					"managed-by": "docker-network-manager",
					"temporary":  "true",
				},
			})
			if err != nil {
				log.Printf("[%s] Warning: Failed to create temporary network: %v", runID, err)
			}

			if tempNet.ID != "" {
				for _, containerID := range containersToReconnect {
					log.Printf("[%s] Connecting container %s to temporary network", runID, containerID[:12])
					if err := m.client.NetworkConnect(ctx, tempNet.ID, containerID, nil); err != nil {
						log.Printf("[%s] Warning: Failed to connect container %s to temp network: %v",
							runID, containerID[:12], err)
					}
				}
			}

			for _, containerID := range containersToReconnect {
				log.Printf("[%s] Disconnecting container %s from network %s", runID, containerID[:12], networkName)
				if err := m.client.NetworkDisconnect(ctx, existingNetwork.ID, containerID, false); err != nil {
					log.Printf("[%s] Warning: Failed to disconnect container %s: %v", runID, containerID[:12], err)
				}
			}
		}

		log.Printf("[%s] Removing network %s for recreation", runID, networkName)
		if err := m.client.NetworkRemove(ctx, existingNetwork.ID); err != nil {
			return fmt.Errorf("failed to remove network for conversion: %w", err)
		}

		log.Printf("[%s] Recreating network %s with internal=%v", runID, networkName, shouldBeInternal)
		newNet, err := m.client.NetworkCreate(ctx, networkName, network.CreateOptions{
			Driver:   "bridge",
			Internal: shouldBeInternal,
			Labels: map[string]string{
				"managed-by":                 "docker-network-manager",
				"com.docker.compose.network": networkName,
			},
		})
		if err != nil {
			return fmt.Errorf("failed to recreate network: %w", err)
		}

		for _, containerID := range containersToReconnect {
			log.Printf("[%s] Reconnecting container %s to network %s", runID, containerID[:12], networkName)
			if err := m.client.NetworkConnect(ctx, newNet.ID, containerID, nil); err != nil {
				log.Printf("[%s] Error reconnecting container %s: %v", runID, containerID[:12], err)
			}
		}

		if len(containersToReconnect) > 0 {
			tempNetworkName := "temp-safety-" + networkName
			for _, containerID := range containersToReconnect {
				m.client.NetworkDisconnect(ctx, tempNetworkName, containerID, false)
			}
			if err := m.client.NetworkRemove(ctx, tempNetworkName); err != nil {
				log.Printf("[%s] Warning: Failed to cleanup temporary network %s: %v", runID, tempNetworkName, err)
			} else {
				log.Printf("[%s] Cleaned up temporary network: %s", runID, tempNetworkName)
			}
		}

		log.Printf("[%s] Successfully converted network %s to internal=%v", runID, networkName, shouldBeInternal)
	}

	return nil
}

func generateRunID() string {
	const charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	b := make([]byte, 6)
	for i := range b {
		b[i] = charset[rand.Intn(len(charset))]
	}
	return string(b)
}

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}

func getDurationEnv(key string, defaultValue time.Duration) time.Duration {
	if value := os.Getenv(key); value != "" {
		if duration, err := time.ParseDuration(value); err == nil {
			return duration
		}
	}
	return defaultValue
}

func getBoolEnv(key string, defaultValue bool) bool {
	if value := os.Getenv(key); value != "" {
		return strings.ToLower(value) == "true"
	}
	return defaultValue
}

func getIntEnv(key string, defaultValue int) int {
	if value := os.Getenv(key); value != "" {
		var result int
		if _, err := fmt.Sscanf(value, "%d", &result); err == nil {
			return result
		}
	}
	return defaultValue
}

func getNetworkNames(networks map[string]bool) []string {
	names := make([]string, 0, len(networks))
	for name, isInternal := range networks {
		if isInternal {
			names = append(names, name+" (internal)")
		} else {
			names = append(names, name)
		}
	}
	return names
}
