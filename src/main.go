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

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/events"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
)

const (
	defaultLabelPrefix          = "managed.network."
	defaultDisconnectDefaultKey = "disconnectdefault"
	defaultInternalKey          = "internal"
)

type NetworkManager struct {
	client              *client.Client
	labelPrefix         string
	disconnectDefaultKey string
	internalKey         string
	mu                  sync.RWMutex
	managedContainers   map[string]bool // Track containers we're managing
}

func main() {
	log.Println("Starting Docker Network Manager...")

	// Initialize Docker client
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		log.Fatalf("Failed to create Docker client: %v", err)
	}
	defer cli.Close()

	// Get configuration from environment
	labelPrefix := getEnv("LABEL_PREFIX", defaultLabelPrefix)
	disconnectDefaultKey := getEnv("DISCONNECT_DEFAULT_KEY", defaultDisconnectDefaultKey)
	internalKey := getEnv("INTERNAL_KEY", defaultInternalKey)

	log.Printf("Configuration:")
	log.Printf("  Label prefix: %s", labelPrefix)
	log.Printf("  Disconnect default key: %s%s", labelPrefix, disconnectDefaultKey)
	log.Printf("  Internal network key: %s%s", labelPrefix, internalKey)

	manager := &NetworkManager{
		client:              cli,
		labelPrefix:         labelPrefix,
		disconnectDefaultKey: disconnectDefaultKey,
		internalKey:         internalKey,
		managedContainers:   make(map[string]bool),
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Handle graceful shutdown
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)

	go func() {
		<-sigChan
		log.Println("Received shutdown signal, stopping...")
		cancel()
	}()

	// Initial reconciliation of all existing containers
	log.Println("Performing initial reconciliation...")
	if err := manager.reconcileAllContainers(ctx); err != nil {
		log.Printf("Warning: Initial reconciliation failed: %v", err)
	}

	// Start watching for container events
	log.Println("Starting event watcher...")
	if err := manager.watchEvents(ctx); err != nil {
		log.Fatalf("Event watcher failed: %v", err)
	}

	log.Println("Docker Network Manager stopped")
}

func (m *NetworkManager) reconcileAllContainers(ctx context.Context) error {
	containers, err := m.client.ContainerList(ctx, container.ListOptions{})
	if err != nil {
		return fmt.Errorf("failed to list containers: %w", err)
	}

	log.Printf("Found %d containers to reconcile", len(containers))

	for _, c := range containers {
		if err := m.reconcileContainer(ctx, c.ID); err != nil {
			log.Printf("Error reconciling container %s: %v", c.ID[:12], err)
		}
	}

	return nil
}

func (m *NetworkManager) watchEvents(ctx context.Context) error {
	// Filter for container events
	filter := filters.NewArgs()
	filter.Add("type", "container")
	filter.Add("event", "start")
	filter.Add("event", "die")
	filter.Add("event", "update") // Catch label updates

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
		// Give Docker a moment to fully start the container
		time.Sleep(500 * time.Millisecond)
		
		if err := m.reconcileContainer(ctx, event.ID); err != nil {
			log.Printf("Error handling event for container %s: %v", event.ID[:12], err)
		}
	case "die":
		m.mu.Lock()
		delete(m.managedContainers, event.ID)
		m.mu.Unlock()
	}
}

func (m *NetworkManager) reconcileContainer(ctx context.Context, containerID string) error {
	// Get container details
	containerInfo, err := m.client.ContainerInspect(ctx, containerID)
	if err != nil {
		return fmt.Errorf("failed to inspect container: %w", err)
	}

	// Skip if container is not running
	if !containerInfo.State.Running {
		return nil
	}

	labels := containerInfo.Config.Labels
	
	// Extract network labels
	networkLabels := m.extractNetworkLabels(labels)
	
	// If no network labels, skip management
	if len(networkLabels) == 0 {
		return nil
	}

	log.Printf("Managing container %s (%s)", containerInfo.Name, containerID[:12])

	// Track this container
	m.mu.Lock()
	m.managedContainers[containerID] = true
	m.mu.Unlock()

	// Check if we should disconnect default network
	shouldDisconnectDefault := m.shouldDisconnectDefault(labels)
	
	// Check if internal networks should be created
	isInternal := m.isInternalNetwork(labels)

	// Get currently connected networks
	currentNetworks := make(map[string]bool)
	for netName := range containerInfo.NetworkSettings.Networks {
		currentNetworks[netName] = true
	}

	// Ensure all labeled networks exist and are connected
	for netName := range networkLabels {
		// Create network if it doesn't exist
		if err := m.ensureNetworkExists(ctx, netName, isInternal); err != nil {
			log.Printf("Error ensuring network %s exists: %v", netName, err)
			continue
		}

		// Connect if not already connected
		if !currentNetworks[netName] {
			log.Printf("Connecting container %s to network %s", containerID[:12], netName)
			if err := m.client.NetworkConnect(ctx, netName, containerID, nil); err != nil {
				log.Printf("Error connecting container %s to network %s: %v", containerID[:12], netName, err)
			}
		}
		
		// Mark as managed
		delete(currentNetworks, netName)
	}

	// Disconnect from networks that are no longer in labels
	for netName := range currentNetworks {
		// Check if this is a default network and we should keep it
		if m.isDefaultNetwork(netName, containerInfo) && !shouldDisconnectDefault {
			continue
		}

		// Disconnect if it was previously managed or if we should disconnect default
		if shouldDisconnectDefault && m.isDefaultNetwork(netName, containerInfo) {
			log.Printf("Disconnecting container %s from default network %s", containerID[:12], netName)
			if err := m.client.NetworkDisconnect(ctx, netName, containerID, false); err != nil {
				log.Printf("Error disconnecting container %s from network %s: %v", containerID[:12], netName, err)
			}
		}
	}

	return nil
}

func (m *NetworkManager) extractNetworkLabels(labels map[string]string) map[string]bool {
	networks := make(map[string]bool)
	
	for key, value := range labels {
		if !strings.HasPrefix(key, m.labelPrefix) {
			continue
		}

		// Remove prefix
		suffix := strings.TrimPrefix(key, m.labelPrefix)
		
		// Skip special keys
		if suffix == m.disconnectDefaultKey || suffix == m.internalKey {
			continue
		}

		// Check if label is set to true
		if strings.ToLower(value) == "true" {
			networks[suffix] = true
		}
	}

	return networks
}

func (m *NetworkManager) shouldDisconnectDefault(labels map[string]string) bool {
	key := m.labelPrefix + m.disconnectDefaultKey
	value, exists := labels[key]
	return exists && strings.ToLower(value) == "true"
}

func (m *NetworkManager) isInternalNetwork(labels map[string]string) bool {
	key := m.labelPrefix + m.internalKey
	value, exists := labels[key]
	return exists && strings.ToLower(value) == "true"
}

func (m *NetworkManager) isDefaultNetwork(netName string, containerInfo types.ContainerJSON) bool {
	// Default networks are typically named after the compose project or "bridge"
	// We'll consider it default if it matches the compose project or is "bridge"
	if netName == "bridge" || netName == "host" || netName == "none" {
		return true
	}

	// Check if it's the compose default network (usually projectname_default)
	projectName := containerInfo.Config.Labels["com.docker.compose.project"]
	if projectName != "" && netName == projectName+"_default" {
		return true
	}

	return false
}

func (m *NetworkManager) ensureNetworkExists(ctx context.Context, networkName string, internal bool) error {
	// Check if network exists
	networks, err := m.client.NetworkList(ctx, network.ListOptions{
		Filters: filters.NewArgs(filters.Arg("name", networkName)),
	})
	if err != nil {
		return fmt.Errorf("failed to list networks: %w", err)
	}

	// Network exists, verify it's the right one (exact match)
	for _, net := range networks {
		if net.Name == networkName {
			return nil
		}
	}

	// Create network
	log.Printf("Creating network %s (internal: %v)", networkName, internal)
	
	_, err = m.client.NetworkCreate(ctx, networkName, network.CreateOptions{
		Driver:   "bridge",
		Internal: internal,
		Labels: map[string]string{
			"managed-by": "docker-network-manager",
		},
	})
	
	if err != nil {
		return fmt.Errorf("failed to create network: %w", err)
	}

	return nil
}

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}
