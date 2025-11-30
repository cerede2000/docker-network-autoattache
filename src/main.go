package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/events"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
)

const (
	defaultLabelPrefix           = "managed.network."
	defaultDisconnectOthersKey   = "disconnectothers"
	defaultInternalKey           = "internal"
	defaultReconciliationInterval = 30 * time.Second
	defaultDisconnectOthersDefault = false // Par défaut : garde les autres réseaux
)

type NetworkManager struct {
	client                 *client.Client
	labelPrefix            string
	disconnectOthersKey    string
	internalKey            string
	reconciliationInterval time.Duration
	disconnectOthersDefault bool // Comportement par défaut si le label n'est pas présent
	mu                     sync.RWMutex
	managedContainers      map[string]bool
	networkInternalState   map[string]bool
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

	log.Printf("Configuration:")
	log.Printf("  Label prefix: %s", labelPrefix)
	log.Printf("  Disconnect others label: %s%s", labelPrefix, disconnectOthersKey)
	log.Printf("  Disconnect others default: %v", disconnectOthersDefault)
	log.Printf("  Internal network suffix: .%s", internalKey)
	log.Printf("  Reconciliation interval: %v", reconciliationInterval)

	manager := &NetworkManager{
		client:                 cli,
		labelPrefix:            labelPrefix,
		disconnectOthersKey:    disconnectOthersKey,
		internalKey:            internalKey,
		reconciliationInterval: reconciliationInterval,
		disconnectOthersDefault: disconnectOthersDefault,
		managedContainers:      make(map[string]bool),
		networkInternalState:   make(map[string]bool),
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
	if err := manager.reconcileAllContainers(ctx); err != nil {
		log.Printf("Warning: Initial reconciliation failed: %v", err)
	}

	go manager.periodicReconciliation(ctx)

	log.Println("Starting event watcher...")
	if err := manager.watchEvents(ctx); err != nil {
		log.Fatalf("Event watcher failed: %v", err)
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
			log.Println("Running periodic reconciliation...")
			if err := m.reconcileAllContainers(ctx); err != nil {
				log.Printf("Periodic reconciliation error: %v", err)
			}
		case <-ctx.Done():
			log.Println("Stopping periodic reconciliation")
			return
		}
	}
}

func (m *NetworkManager) reconcileAllContainers(ctx context.Context) error {
	containers, err := m.client.ContainerList(ctx, container.ListOptions{})
	if err != nil {
		return fmt.Errorf("failed to list containers: %w", err)
	}

	log.Printf("Found %d containers to check", len(containers))

	// First pass: collect all network requirements
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
					log.Printf("⚠️  CONFLICT: Network %s requested as internal=%v and internal=%v - choosing internal=true for security", 
						netName, existing, isInternal)
					networkRequirements[netName] = true
				}
			} else {
				networkRequirements[netName] = isInternal
			}
		}
	}

	// Update network internal state cache
	m.mu.Lock()
	m.networkInternalState = networkRequirements
	m.mu.Unlock()

	// Second pass: reconcile containers with resolved network states
	managedCount := 0
	skippedCount := 0
	errorCount := 0

	for _, c := range containers {
		if err := m.reconcileContainer(ctx, c.ID); err != nil {
			log.Printf("Error reconciling container %s: %v", c.ID[:12], err)
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

	log.Printf("Reconciliation complete: %d managed, %d skipped, %d errors", 
		managedCount, skippedCount, errorCount)

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
	switch event.Action {
	case "start", "update":
		time.Sleep(500 * time.Millisecond)
		
		log.Printf("Event: %s for container %s", event.Action, event.ID[:12])
		
		if err := m.reconcileAllContainers(ctx); err != nil {
			log.Printf("Error during event reconciliation: %v", err)
		}
		
	case "die":
		m.mu.Lock()
		delete(m.managedContainers, event.ID)
		m.mu.Unlock()
		log.Printf("Container %s stopped, removed from managed list", event.ID[:12])
	}
}

func (m *NetworkManager) reconcileContainer(ctx context.Context, containerID string) error {
	containerInfo, err := m.client.ContainerInspect(ctx, containerID)
	if err != nil {
		return fmt.Errorf("failed to inspect container: %w", err)
	}

	containerName := containerInfo.Name
	
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

	log.Printf("Managing container %s (%s) - Networks: %v", 
		containerName, containerID[:12], getNetworkNames(resolvedNetworks))

	m.mu.Lock()
	m.managedContainers[containerID] = true
	m.mu.Unlock()

	shouldDisconnectOthers := m.shouldDisconnectOthers(labels)

	// Get currently connected networks
	currentNetworks := make(map[string]bool)
	for netName := range containerInfo.NetworkSettings.Networks {
		currentNetworks[netName] = true
	}

	// Ensure all labeled networks exist and are connected
	for netName, isInternal := range resolvedNetworks {
		if err := m.ensureNetworkExistsWithInternalFlag(ctx, netName, isInternal); err != nil {
			log.Printf("Error ensuring network %s exists: %v", netName, err)
			continue
		}

		if !currentNetworks[netName] {
			log.Printf("Connecting container %s to network %s", containerID[:12], netName)
			if err := m.client.NetworkConnect(ctx, netName, containerID, nil); err != nil {
				if !strings.Contains(err.Error(), "already exists in network") {
					log.Printf("Error connecting container %s to network %s: %v", 
						containerID[:12], netName, err)
				}
			}
		}
		
		delete(currentNetworks, netName)
	}

	// Disconnect from OTHER networks if requested
	if shouldDisconnectOthers {
		for netName := range currentNetworks {
			log.Printf("Disconnecting container %s from unmanaged network %s", 
				containerID[:12], netName)
			if err := m.client.NetworkDisconnect(ctx, netName, containerID, false); err != nil {
				if !strings.Contains(err.Error(), "is not connected to") {
					log.Printf("Error disconnecting container %s from network %s: %v", 
						containerID[:12], netName, err)
				}
			}
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
		// Si le label est présent, on utilise sa valeur
		return strings.ToLower(value) == "true"
	}
	
	// Si le label n'est pas présent, on utilise la valeur par défaut
	return m.disconnectOthersDefault
}

func (m *NetworkManager) ensureNetworkExistsWithInternalFlag(ctx context.Context, networkName string, shouldBeInternal bool) error {
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
		log.Printf("Creating network %s (internal: %v)", networkName, shouldBeInternal)
		
		_, err = m.client.NetworkCreate(ctx, networkName, network.CreateOptions{
			Driver:   "bridge",
			Internal: shouldBeInternal,
			Labels: map[string]string{
				"managed-by": "docker-network-manager",
			},
		})
		
		if err != nil {
			return fmt.Errorf("failed to create network: %w", err)
		}
		return nil
	}

	currentlyInternal := existingNetwork.Internal
	
	if currentlyInternal != shouldBeInternal {
		log.Printf("Network %s needs internal flag conversion: %v -> %v", 
			networkName, currentlyInternal, shouldBeInternal)
		
		netDetails, err := m.client.NetworkInspect(ctx, existingNetwork.ID, network.InspectOptions{})
		if err != nil {
			return fmt.Errorf("failed to inspect network: %w", err)
		}

		containersToReconnect := make([]string, 0)
		for containerID := range netDetails.Containers {
			containersToReconnect = append(containersToReconnect, containerID)
		}

		if len(containersToReconnect) > 0 {
			log.Printf("Network %s has %d connected containers, preparing safe conversion...", 
				networkName, len(containersToReconnect))
			
			tempNetworkName := "temp-safety-" + networkName
			log.Printf("Creating temporary safety network: %s", tempNetworkName)
			
			tempNet, err := m.client.NetworkCreate(ctx, tempNetworkName, network.CreateOptions{
				Driver: "bridge",
				Labels: map[string]string{
					"managed-by": "docker-network-manager",
					"temporary":  "true",
				},
			})
			if err != nil {
				log.Printf("Warning: Failed to create temporary network: %v", err)
			}

			if tempNet.ID != "" {
				for _, containerID := range containersToReconnect {
					log.Printf("Connecting container %s to temporary network", containerID[:12])
					if err := m.client.NetworkConnect(ctx, tempNet.ID, containerID, nil); err != nil {
						log.Printf("Warning: Failed to connect container %s to temp network: %v", 
							containerID[:12], err)
					}
				}
			}

			for _, containerID := range containersToReconnect {
				log.Printf("Disconnecting container %s from network %s", containerID[:12], networkName)
				if err := m.client.NetworkDisconnect(ctx, existingNetwork.ID, containerID, false); err != nil {
					log.Printf("Warning: Failed to disconnect container %s: %v", containerID[:12], err)
				}
			}
		}

		log.Printf("Removing network %s for recreation", networkName)
		if err := m.client.NetworkRemove(ctx, existingNetwork.ID); err != nil {
			return fmt.Errorf("failed to remove network for conversion: %w", err)
		}

		log.Printf("Recreating network %s with internal=%v", networkName, shouldBeInternal)
		newNet, err := m.client.NetworkCreate(ctx, networkName, network.CreateOptions{
			Driver:   "bridge",
			Internal: shouldBeInternal,
			Labels: map[string]string{
				"managed-by": "docker-network-manager",
			},
		})
		if err != nil {
			return fmt.Errorf("failed to recreate network: %w", err)
		}

		for _, containerID := range containersToReconnect {
			log.Printf("Reconnecting container %s to network %s", containerID[:12], networkName)
			if err := m.client.NetworkConnect(ctx, newNet.ID, containerID, nil); err != nil {
				log.Printf("Error reconnecting container %s: %v", containerID[:12], err)
			}
		}

		if len(containersToReconnect) > 0 {
			tempNetworkName := "temp-safety-" + networkName
			for _, containerID := range containersToReconnect {
				m.client.NetworkDisconnect(ctx, tempNetworkName, containerID, false)
			}
			if err := m.client.NetworkRemove(ctx, tempNetworkName); err != nil {
				log.Printf("Warning: Failed to cleanup temporary network %s: %v", tempNetworkName, err)
			} else {
				log.Printf("Cleaned up temporary network: %s", tempNetworkName)
			}
		}

		log.Printf("Successfully converted network %s to internal=%v", networkName, shouldBeInternal)
	}

	return nil
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
