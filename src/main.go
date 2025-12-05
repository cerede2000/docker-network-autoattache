package main

import (
	"context"
	"fmt"
	"log"
	"math/rand"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/docker/docker/api/types"
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

	managedByLabelKey   = "managed-by"
	managedByLabelValue = "docker-network-manager"

	composeNetworkLabelKey = "com.docker.compose.network"
)

type NetworkManager struct {
	client                  *client.Client
	labelPrefix             string
	disconnectOthersKey     string
	internalKey             string
	reconciliationInterval  time.Duration
	disconnectOthersDefault bool

	mu                   sync.RWMutex
	managedContainers    map[string]bool
	networkInternalState map[string]bool

	reconciliationMutex sync.Mutex
}

func main() {
	rand.Seed(time.Now().UnixNano())

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

	log.Printf("Configuration:")
	log.Printf("  Label prefix: %s", labelPrefix)
	log.Printf("  Disconnect others label: %s%s", labelPrefix, disconnectOthersKey)
	log.Printf("  Disconnect others default: %v", disconnectOthersDefault)
	log.Printf("  Internal network suffix: .%s", internalKey)
	log.Printf("  Reconciliation interval: %v", reconciliationInterval)

	manager := &NetworkManager{
		client:                  cli,
		labelPrefix:             labelPrefix,
		disconnectOthersKey:     disconnectOthersKey,
		internalKey:             internalKey,
		reconciliationInterval:  reconciliationInterval,
		disconnectOthersDefault: disconnectOthersDefault,
		managedContainers:       make(map[string]bool),
		networkInternalState:    make(map[string]bool),
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)

	go func() {
		<-sigChan
		log.Println("Received shutdown signal, stopping...")
		cancel()
	}()

	log.Println("Performing initial reconciliation...")
	runID := generateRunID()
	if err := manager.reconcileAllContainers(ctx, runID); err != nil {
		log.Printf("[%s] Warning: Initial reconciliation failed: %v", runID, err)
	}

	go manager.periodicReconciliation(ctx)

	// Safety net: watch network connect/disconnect
	go func() {
		log.Println("Starting network event watcher...")
		if err := manager.watchNetworkEvents(ctx); err != nil && ctx.Err() == nil {
			log.Printf("Network event watcher error: %v", err)
		}
	}()

	log.Println("Starting container event watcher...")
	if err := manager.watchContainerEvents(ctx); err != nil {
		log.Fatalf("Container event watcher failed: %v", err)
	}

	log.Println("Docker Network Manager stopped")
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

	// First pass: collect all network requirements from running containers
	networkRequirements := make(map[string]bool)

	for _, c := range containers {
		containerInfo, err := m.client.ContainerInspect(ctx, c.ID)
		if err != nil || containerInfo.State == nil || !containerInfo.State.Running {
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
			log.Printf("[%s] Error reconciling container %s: %v", runID, shortID(c.ID), err)
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

//
// --- Event watchers ---
//

func (m *NetworkManager) watchContainerEvents(ctx context.Context) error {
	filter := filters.NewArgs()
	filter.Add("type", "container")
	filter.Add("event", "create") // KEY FIX for Traefik timing
	filter.Add("event", "start")
	filter.Add("event", "die")
	filter.Add("event", "update")

	eventsChan, errChan := m.client.Events(ctx, events.ListOptions{
		Filters: filter,
	})

	for {
		select {
		case event, ok := <-eventsChan:
			if !ok {
				if ctx.Err() != nil {
					return nil
				}
				return fmt.Errorf("container event stream closed")
			}
			go m.handleContainerEvent(ctx, event)

		case err, ok := <-errChan:
			if ok && err != nil {
				if ctx.Err() != nil {
					return nil
				}
				return fmt.Errorf("container event stream error: %w", err)
			}

		case <-ctx.Done():
			return nil
		}
	}
}

func (m *NetworkManager) watchNetworkEvents(ctx context.Context) error {
	filter := filters.NewArgs()
	filter.Add("type", "network")
	filter.Add("event", "connect")
	filter.Add("event", "disconnect")

	eventsChan, errChan := m.client.Events(ctx, events.ListOptions{
		Filters: filter,
	})

	for {
		select {
		case event, ok := <-eventsChan:
			if !ok {
				if ctx.Err() != nil {
					return nil
				}
				return fmt.Errorf("network event stream closed")
			}

			cid := event.Actor.Attributes["container"]
			if cid == "" {
				continue
			}

			runID := generateRunID()
			log.Printf("[%s] Network event: %s for container %s", runID, event.Action, shortID(cid))

			go func(containerID string, rid string) {
				time.Sleep(150 * time.Millisecond)
				if err := m.reconcileContainerWithConflictDetection(ctx, containerID, rid); err != nil {
					log.Printf("[%s] Error reconciling after network event for %s: %v", rid, shortID(containerID), err)
				}
			}(cid, runID)

		case err, ok := <-errChan:
			if ok && err != nil {
				if ctx.Err() != nil {
					return nil
				}
				return fmt.Errorf("network event stream error: %w", err)
			}

		case <-ctx.Done():
			return nil
		}
	}
}

func (m *NetworkManager) handleContainerEvent(ctx context.Context, event events.Message) {
	runID := generateRunID()

	switch event.Action {
	case "create":
		// No sleep: fix networks BEFORE start
		containerInfo, err := m.client.ContainerInspect(ctx, event.ID)
		containerName := shortID(event.ID)
		if err == nil && containerInfo.Name != "" {
			containerName = containerInfo.Name
		}

		log.Printf("[%s] Event: create for container %s (%s)", runID, containerName, shortID(event.ID))

		if err := m.reconcileContainerWithConflictDetection(ctx, event.ID, runID); err != nil {
			log.Printf("[%s] Error reconciling container on create %s (%s): %v",
				runID, containerName, shortID(event.ID), err)
		}

	case "start", "update":
		time.Sleep(400 * time.Millisecond)

		containerInfo, err := m.client.ContainerInspect(ctx, event.ID)
		containerName := shortID(event.ID)
		if err == nil && containerInfo.Name != "" {
			containerName = containerInfo.Name
		}

		log.Printf("[%s] Event: %s for container %s (%s)", runID, event.Action, containerName, shortID(event.ID))

		if err := m.reconcileContainerWithConflictDetection(ctx, event.ID, runID); err != nil {
			log.Printf("[%s] Error reconciling container %s (%s): %v",
				runID, containerName, shortID(event.ID), err)
		}

	case "die":
		containerInfo, err := m.client.ContainerInspect(ctx, event.ID)
		containerName := shortID(event.ID)
		if err == nil && containerInfo.Name != "" {
			containerName = containerInfo.Name
		}

		m.mu.Lock()
		delete(m.managedContainers, event.ID)
		m.mu.Unlock()

		log.Printf("[%s] Container %s (%s) stopped, removed from managed list",
			runID, containerName, shortID(event.ID))
	}
}

//
// --- Reconciliation logic ---
//

func (m *NetworkManager) reconcileContainerWithConflictDetection(ctx context.Context, containerID string, runID string) error {
	m.reconciliationMutex.Lock()
	defer m.reconciliationMutex.Unlock()

	containerInfo, err := m.client.ContainerInspect(ctx, containerID)
	if err != nil {
		return fmt.Errorf("failed to inspect container: %w", err)
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
					runID, netName, existingInternal, wantInternal, containerInfo.Name)
				hasConflict = true
			}
		}
	}
	m.mu.RUnlock()

	if hasConflict {
		log.Printf("[%s] Conflict detected for container %s (%s), triggering full reconciliation...",
			runID, containerInfo.Name, shortID(containerID))

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
	short := shortID(containerID)

	labels := containerInfo.Config.Labels
	networkLabels := m.extractNetworkLabelsWithInternal(labels)

	if len(networkLabels) == 0 {
		m.mu.Lock()
		delete(m.managedContainers, containerID)
		m.mu.Unlock()
		return nil
	}

	// Use global network state
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
		runID, containerName, short, getNetworkNames(resolvedNetworks))

	m.mu.Lock()
	m.managedContainers[containerID] = true
	m.mu.Unlock()

	shouldDisconnectOthers := m.shouldDisconnectOthers(labels)

	// currently connected networks
	currentNetworks := make(map[string]bool)
	if containerInfo.NetworkSettings != nil {
		for netName := range containerInfo.NetworkSettings.Networks {
			currentNetworks[netName] = true
		}
	}

	networksChanged := false

	// Ensure all labeled networks exist and are connected
	for netName, isInternal := range resolvedNetworks {
		if err := m.ensureNetworkExistsWithInternalFlag(ctx, netName, isInternal, runID); err != nil {
			log.Printf("[%s] Error ensuring network %s exists: %v", runID, netName, err)
			continue
		}

		if !currentNetworks[netName] {
			log.Printf("[%s] Connecting container %s (%s) to network %s",
				runID, containerName, short, netName)

			if err := m.client.NetworkConnect(ctx, netName, containerID, nil); err != nil {
				if !strings.Contains(err.Error(), "already exists in network") {
					log.Printf("[%s] Error connecting container %s (%s) to network %s: %v",
						runID, containerName, short, netName, err)
				}
			} else {
				networksChanged = true
			}
		}

		delete(currentNetworks, netName)
	}

	// Disconnect from OTHER networks if requested
	if shouldDisconnectOthers {
		for netName := range currentNetworks {
			log.Printf("[%s] Disconnecting container %s (%s) from unmanaged network %s",
				runID, containerName, short, netName)

			if err := m.client.NetworkDisconnect(ctx, netName, containerID, false); err != nil {
				if !strings.Contains(err.Error(), "is not connected to") {
					log.Printf("[%s] Error disconnecting container %s (%s) from network %s: %v",
						runID, containerName, short, netName, err)
				}
			} else {
				networksChanged = true
			}
		}
	}

	// If networks changed, force a REAL update event
	if networksChanged {
		log.Printf("[%s] Networks changed for container %s (%s), forcing real update event",
			runID, containerName, short)

		m.forceRealUpdateEvent(ctx, containerInfo, containerID, runID)
	}

	return nil
}

//
// --- Labels helpers ---
//

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

//
// --- Force real update event ---
//

func (m *NetworkManager) forceRealUpdateEvent(ctx context.Context, containerInfo types.ContainerJSON, containerID string, runID string) {
	if containerInfo.HostConfig == nil {
		return
	}

	orig := containerInfo.HostConfig.RestartPolicy
	temp := orig

	if orig.Name != "no" {
		temp.Name = "no"
	} else {
		temp.Name = "unless-stopped"
	}

	if _, err := m.client.ContainerUpdate(ctx, containerID, container.UpdateConfig{
		RestartPolicy: temp,
	}); err != nil {
		log.Printf("[%s] Warning: Failed to apply temp restart policy for %s: %v",
			runID, shortID(containerID), err)
		return
	}

	if _, err := m.client.ContainerUpdate(ctx, containerID, container.UpdateConfig{
		RestartPolicy: orig,
	}); err != nil {
		log.Printf("[%s] Warning: Failed to restore restart policy for %s: %v",
			runID, shortID(containerID), err)
		return
	}

	log.Printf("[%s] Successfully forced update event for container %s",
		runID, shortID(containerID))
}

//
// --- Network ensure/convert + compose label fix ---
//

func (m *NetworkManager) ensureNetworkExistsWithInternalFlag(ctx context.Context, networkName string, shouldBeInternal bool, runID string) error {
	desiredLabels := map[string]string{
		managedByLabelKey:        managedByLabelValue,
		composeNetworkLabelKey:   networkName, // IMPORTANT for docker compose expectations
	}

	nets, err := m.client.NetworkList(ctx, network.ListOptions{
		Filters: filters.NewArgs(filters.Arg("name", networkName)),
	})
	if err != nil {
		return fmt.Errorf("failed to list networks: %w", err)
	}

	var existing *network.Summary
	for i := range nets {
		if nets[i].Name == networkName {
			existing = &nets[i]
			break
		}
	}

	if existing == nil {
		log.Printf("[%s] Creating network %s (internal: %v) with compose label",
			runID, networkName, shouldBeInternal)

		_, err = m.client.NetworkCreate(ctx, networkName, network.CreateOptions{
			Driver:   "bridge",
			Internal: shouldBeInternal,
			Labels:   desiredLabels,
		})
		if err != nil {
			return fmt.Errorf("failed to create network: %w", err)
		}
		return nil
	}

	// Check whether we need to recreate:
	// - internal flag mismatch
	// - compose network label missing/incorrect
	currentlyInternal := existing.Internal
	currentLabels := existing.Labels
	if currentLabels == nil {
		currentLabels = map[string]string{}
	}

	currentComposeValue := currentLabels[composeNetworkLabelKey]
	composeLabelMismatch := currentComposeValue != networkName

	internalMismatch := currentlyInternal != shouldBeInternal

	if !internalMismatch && !composeLabelMismatch {
		return nil
	}

	// We cannot update network labels in place -> must recreate safely.
	if composeLabelMismatch {
		log.Printf("[%s] Network %s has incorrect compose label %s=%q (expected %q) -> will recreate safely",
			runID, networkName, composeNetworkLabelKey, currentComposeValue, networkName)
	}
	if internalMismatch {
		log.Printf("[%s] Network %s needs internal flag conversion: %v -> %v",
			runID, networkName, currentlyInternal, shouldBeInternal)
	}

	// Inspect network to list connected containers
	netDetails, err := m.client.NetworkInspect(ctx, existing.ID, network.InspectOptions{})
	if err != nil {
		return fmt.Errorf("failed to inspect network: %w", err)
	}

	containersToReconnect := make([]string, 0)
	for containerID := range netDetails.Containers {
		containersToReconnect = append(containersToReconnect, containerID)
	}

	// If containers connected, attach them to a temporary safety network first
	var tempNetID string
	tempNetworkName := "temp-safety-" + networkName

	if len(containersToReconnect) > 0 {
		log.Printf("[%s] Network %s has %d connected containers, preparing safe recreation...",
			runID, networkName, len(containersToReconnect))

		log.Printf("[%s] Creating temporary safety network: %s", runID, tempNetworkName)
		tempNet, err := m.client.NetworkCreate(ctx, tempNetworkName, network.CreateOptions{
			Driver: "bridge",
			Labels: map[string]string{
				managedByLabelKey: managedByLabelValue,
				"temporary":       "true",
			},
		})
		if err != nil {
			log.Printf("[%s] Warning: Failed to create temporary network: %v", runID, err)
		} else {
			tempNetID = tempNet.ID
		}

		if tempNetID != "" {
			for _, containerID := range containersToReconnect {
				log.Printf("[%s] Connecting container %s to temporary network", runID, shortID(containerID))
				if err := m.client.NetworkConnect(ctx, tempNetID, containerID, nil); err != nil {
					log.Printf("[%s] Warning: Failed to connect container %s to temp network: %v",
						runID, shortID(containerID), err)
				}
			}
		}

		for _, containerID := range containersToReconnect {
			log.Printf("[%s] Disconnecting container %s from network %s", runID, shortID(containerID), networkName)
			if err := m.client.NetworkDisconnect(ctx, existing.ID, containerID, false); err != nil {
				log.Printf("[%s] Warning: Failed to disconnect container %s: %v",
					runID, shortID(containerID), err)
			}
		}
	}

	// Remove and recreate the network with correct Internal + labels
	log.Printf("[%s] Removing network %s for recreation", runID, networkName)
	if err := m.client.NetworkRemove(ctx, existing.ID); err != nil {
		return fmt.Errorf("failed to remove network for recreation: %w", err)
	}

	log.Printf("[%s] Recreating network %s (internal=%v) with correct labels",
		runID, networkName, shouldBeInternal)

	newNet, err := m.client.NetworkCreate(ctx, networkName, network.CreateOptions{
		Driver:   "bridge",
		Internal: shouldBeInternal,
		Labels:   desiredLabels,
	})
	if err != nil {
		return fmt.Errorf("failed to recreate network: %w", err)
	}

	// Reconnect containers
	for _, containerID := range containersToReconnect {
		log.Printf("[%s] Reconnecting container %s to network %s", runID, shortID(containerID), networkName)
		if err := m.client.NetworkConnect(ctx, newNet.ID, containerID, nil); err != nil {
			log.Printf("[%s] Error reconnecting container %s: %v",
				runID, shortID(containerID), err)
		}
	}

	// Cleanup temp network
	if len(containersToReconnect) > 0 && tempNetID != "" {
		for _, containerID := range containersToReconnect {
			_ = m.client.NetworkDisconnect(ctx, tempNetID, containerID, false)
		}
		if err := m.client.NetworkRemove(ctx, tempNetID); err != nil {
			log.Printf("[%s] Warning: Failed to cleanup temporary network %s: %v",
				runID, tempNetworkName, err)
		} else {
			log.Printf("[%s] Cleaned up temporary network: %s", runID, tempNetworkName)
		}
	}

	log.Printf("[%s] Successfully recreated network %s with internal=%v and %s=%s",
		runID, networkName, shouldBeInternal, composeNetworkLabelKey, networkName)

	return nil
}

//
// --- Helpers ---
//

func generateRunID() string {
	const charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	b := make([]byte, 6)
	for i := range b {
		b[i] = charset[rand.Intn(len(charset))]
	}
	return string(b)
}

func shortID(id string) string {
	if len(id) >= 12 {
		return id[:12]
	}
	return id
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
