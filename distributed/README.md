# Distributed Setup Guide: Multi-Node Load Balancing & Consul Failover

This directory contains template configurations for scaling the Unified LLM Triage Gateway stack to a production-grade, multi-node distributed system.

## Directory Structure
- [consul/server-config.json](file:///home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/consul/server-config.json): Core configuration for bootstrapping a 3-node Consul server quorum.
- [consul/triage-router.json](file:///home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/consul/triage-router.json): Consul client registration check for the FastAPI `llm-triage-router` microservice.
- [consul/litellm-gateway.json](file:///home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/consul/litellm-gateway.json): Consul client registration check for the `litellm-gateway`.
- [haproxy/haproxy.cfg.ctmpl](file:///home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/haproxy/haproxy.cfg.ctmpl): dynamic HAProxy configuration template read by `consul-template`.
- [haproxy/consul-template.hcl](file:///home/gpav/Vrac/LAB/AI/LLM-Routing/distributed/haproxy/consul-template.hcl): Daemon settings linking Consul catalog changes to HAProxy config updates.

## Setup Walkthrough

### 1. Bootstrapping the Consul Server Cluster
On three dedicated nodes (e.g., `192.168.1.10`, `192.168.1.11`, `192.168.1.12`), start Consul Server using the configuration under `consul/server-config.json` (remember to customize `node_name` and `retry_join` per node).
```bash
consul agent -config-file=/etc/consul.d/server-config.json
```

### 2. Configuring Consul Clients & Services on Application Nodes
On the application nodes where the FastAPI router and LiteLLM run, configure a Consul Client. Register the services by copying `triage-router.json` and `litellm-gateway.json` to `/etc/consul.d/` and reloading the client.
```bash
consul reload
```

### 3. Deploying HAProxy with Consul-Template
On your Edge Load Balancer node:
1. Install HAProxy and `consul-template`.
2. Copy `haproxy.cfg.ctmpl` and `consul-template.hcl` to `/etc/haproxy/` and `/etc/consul-template.d/` respectively.
3. Start the `consul-template` daemon:
   ```bash
   systemctl start consul-template
   ```
This will automatically generate `/etc/haproxy/haproxy.cfg` and trigger a zero-downtime reload of HAProxy whenever a triage router or LiteLLM gateway status changes.
