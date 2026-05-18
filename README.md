# Azure VMSS and Managed Lustre Metrics Exporter

Python Prometheus exporter that periodically inventories every Azure VM Scale Set (VMSS) and Azure Managed Lustre filesystem in one or more subscriptions. It exposes VMSS names, desired capacity, actual child-VM counts, and Managed Lustre OST available/used/total capacity as Prometheus gauges.

Azure Monitor does not provide a simple native subscription-wide metric for "VMSS name and current instance count". This exporter treats the problem as Azure inventory/state sampling: it uses **Azure Resource Graph** to query all configured subscriptions in one request pattern and exposes the result as cached gauges on `/metrics`. For Azure Managed Lustre, Resource Graph discovers all `Microsoft.StorageCache/amlFilesystems` resources and Azure Monitor provides per-filesystem `OSTBytesAvailable`, `OSTBytesUsed`, and `OSTBytesTotal` samples.

## Repo layout

```
.
├── Dockerfile
├── Makefile                              # local + container + Kubernetes workflows
├── pyproject.toml
├── README.md
├── deploy/
│   ├── kubernetes.yaml                   # ServiceAccount + Deployment + Service (Workload Identity)
│   ├── ama-metrics-settings-configmap-v1.yaml  # Azure Managed Prometheus scrape config
│   ├── grafana-dashboard-vmss.json       # importable VMSS Grafana dashboard
│   ├── grafana-dashboard-lustre.json     # importable Managed Lustre Grafana dashboard
│   └── lustre-alert-rules.yaml           # Prometheus alert rules for low/stale Lustre capacity
├── src/vmss_metrics_exporter/
│   ├── azure_resource_graph.py
│   ├── collector.py
│   ├── config.py
│   ├── main.py
│   └── models.py
└── tests/
```

## What it exports

| Metric | Type | Meaning |
| --- | --- | --- |
| `azure_vmss_instance_count` | Gauge | Actual VMSS child virtual-machine count observed in Resource Graph. |
| `azure_vmss_capacity` | Gauge | Desired VMSS capacity from the parent VMSS `sku.capacity`. |
| `azure_vmss_info` | Gauge (info) | Static VMSS metadata. Value is always `1`. Labels include `vm_size` (`sku.name`) and `sku_tier` (`sku.tier`). Join with `* on (subscription_id, resource_group, vmss_name) group_left(vm_size, sku_tier)` to enrich the count metrics. |
| `azure_vmss_exporter_last_success_timestamp_seconds` | Gauge | Unix timestamp of the last successful collection. |
| `azure_vmss_exporter_collection_duration_seconds` | Gauge | Duration of the most recent collection attempt. |
| `azure_vmss_exporter_collection_errors_total` | Counter | Total collection errors observed by this process. |
| `azure_vmss_exporter_vmss_total` | Gauge | Number of VMSS observed in the most recent successful collection. |
| `azure_managed_lustre_ost_bytes_available` | Gauge | Azure Managed Lustre `OSTBytesAvailable` metric in bytes, one series per filesystem OST. |
| `azure_managed_lustre_ost_bytes_used` | Gauge | Azure Managed Lustre `OSTBytesUsed` metric in bytes, one series per filesystem OST when Azure Monitor returns it. |
| `azure_managed_lustre_ost_bytes_total` | Gauge | Azure Managed Lustre `OSTBytesTotal` metric in bytes, one series per filesystem OST when Azure Monitor returns it. |
| `azure_managed_lustre_ost_bytes_available_percent` | Gauge | Derived available percentage: `OSTBytesAvailable / OSTBytesTotal * 100`. Use this for normalized capacity alerts. |
| `azure_managed_lustre_ost_bytes_used_percent` | Gauge | Derived used percentage: `OSTBytesUsed / OSTBytesTotal * 100`. |
| `azure_managed_lustre_ost_sample_timestamp_seconds` | Gauge | Unix timestamp of the Azure Monitor sample backing each OST bytes-available series. Use this for stale-sample alerts. |
| `azure_managed_lustre_filesystem_total` | Gauge | Number of Azure Managed Lustre filesystems discovered in the latest collection. |
| `azure_managed_lustre_ost_total` | Gauge | Number of Azure Managed Lustre OST series observed in the latest collection. |
| `azure_managed_lustre_last_success_timestamp_seconds` | Gauge | Unix timestamp of the last successful Managed Lustre collection. |
| `azure_managed_lustre_collection_duration_seconds` | Gauge | Duration of the latest Managed Lustre collection attempt. |
| `azure_managed_lustre_collection_errors_total` | Counter | Total Managed Lustre collection errors observed by this process. |

Labels on `azure_vmss_instance_count` and `azure_vmss_capacity`:

- `subscription_id`
- `resource_group`
- `vmss_name`
- `location`
- `orchestration_mode` (`Uniform` / `Flexible`)

Labels on `azure_vmss_info` (all five above, **plus**):

- `vm_size` — the VMSS SKU name (e.g. `Standard_D4s_v3`, `Standard_D96s_v6`). For Flexible scale sets that mix sizes, this reflects the VMSS-level `sku.name` (often `Mix` or empty → reported as `unknown`).
- `sku_tier` — the VMSS SKU tier (typically `Standard`).

The info-metric pattern keeps `vm_size` *out of the count gauge labelsets*, so resizing a VMSS does not break the historical time series of `azure_vmss_instance_count` / `azure_vmss_capacity`. Example PromQL to show instance counts with VM size:

Labels on Managed Lustre per-OST capacity metrics:

- `subscription_id`
- `resource_group`
- `filesystem_name`
- `location`
- `ostnum` — Azure Monitor `ostnum` dimension from the OST capacity metrics.

```promql
azure_vmss_instance_count
  * on (subscription_id, resource_group, vmss_name) group_left(vm_size, sku_tier)
    azure_vmss_info
```

## Why Azure Resource Graph

The exporter answers a periodic inventory question:

> For every VMSS in these subscriptions, what is its name and how many instances does it have right now?

Resource Graph fits because it:

- Queries every VMSS subscription-wide in one paged request, instead of fanning out ARM calls per scale set.
- Can be polled on its own cadence (default 5 min) decoupled from the Prometheus scrape interval.
- Returns both the parent VMSS (`sku.capacity`) and the child VM resources, so `azure_vmss_capacity` and `azure_vmss_instance_count` can be compared to detect scale transitions, failed allocations, or deletions.

## Configuration

The exporter reads configuration from environment variables (and optionally a local `.env` file when running outside a container).

| Variable | Default | Meaning |
| --- | --- | --- |
| `AZURE_SUBSCRIPTION_IDS` | _(required)_ | Comma-separated subscription IDs to query. |
| `POLL_INTERVAL_SECONDS` | `300` | How often to call Azure Resource Graph. |
| `HOST` | `0.0.0.0` | Bind host for the HTTP server. |
| `PORT` | `8000` | Bind port for `/metrics`. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `VMSS_METRICS_AUTH_MODE` | `auto` | Auth selection: `auto`, `workload_identity`, or `service_principal`. In Kubernetes, set this in the Deployment manifest. |
| `ARG_PAGE_SIZE` | _(library default)_ | Optional Resource Graph page size. |
| `ARG_MAX_RETRIES` | _(library default)_ | Optional retry count for transient errors. |
| `ARG_RETRY_BASE_DELAY_SECONDS` | _(library default)_ | Optional retry backoff base. |
| `ENABLE_MANAGED_LUSTRE_METRICS` | `true` | Discover all Azure Managed Lustre filesystems and query Azure Monitor for `OSTBytesAvailable`, `OSTBytesUsed`, and `OSTBytesTotal`. |
| `LUSTRE_POLL_INTERVAL_SECONDS` | `60` | How often to query Azure Monitor for Managed Lustre metrics. This is intentionally independent from VMSS inventory polling for faster capacity alerting. |
| `LUSTRE_METRICS_LOOKBACK_MINUTES` | `15` | Azure Monitor lookback window used to find the latest non-null OST sample. |
| `LUSTRE_METRICS_INTERVAL` | `PT1M` | Azure Monitor metric granularity for Managed Lustre queries. |
| `LUSTRE_METRICS_MAX_WORKERS` | `4` | Maximum concurrent per-filesystem Azure Monitor metric queries. |

Authentication uses a resilient credential chain implemented in
[`src/vmss_metrics_exporter/credentials.py`](src/vmss_metrics_exporter/credentials.py):

1. **Service Principal** — used when `VMSS_METRICS_AUTH_MODE=service_principal`, or
  in `auto` mode when `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and
  `AZURE_CLIENT_SECRET` are all set. This is deterministic for Kubernetes
  deployments that intentionally mount an SP secret, even if the AKS Workload
  Identity webhook also injects WI environment variables.
2. **Workload Identity** — used when `VMSS_METRICS_AUTH_MODE=workload_identity`, or
  in `auto` mode when SP is not fully configured and the AKS webhook has injected the
   `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_FEDERATED_TOKEN_FILE` env vars.
   This step is constructed explicitly (not via `DefaultAzureCredential`) so the
   resilient wrapper can catch its hard auth errors and continue.
3. **`DefaultAzureCredential`** with workload-identity excluded — covers everything
   else in the normal azure-identity order: Managed Identity via IMDS (using
   `AZURE_CLIENT_ID` if set), Azure CLI, PowerShell, VS Code, etc. During
   construction the `AZURE_FEDERATED_TOKEN_FILE` env var is temporarily cleared so
   its internal `ManagedIdentityCredential` uses IMDS instead of silently reusing the
   broken WI token-exchange shortcut.

Supported authentication modes:

| Scenario | Required environment/configuration | Notes |
| --- | --- | --- |
| Service Principal | `VMSS_METRICS_AUTH_MODE=service_principal` plus `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET` | Uses `DefaultAzureCredential` with only `EnvironmentCredential` enabled. Good for local dev, CI, non-AKS deployments, or Kubernetes deployments that mount an SP Secret. |
| AKS Workload Identity | `VMSS_METRICS_AUTH_MODE=workload_identity` plus `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_FEDERATED_TOKEN_FILE` injected by the webhook | Preferred for AKS. Tried first so the exporter can log and cache WI success. |
| Auto | `VMSS_METRICS_AUTH_MODE=auto` or unset | SP wins when complete SP env vars exist; otherwise WI is used when webhook env vars exist; otherwise DAC fallback is used. |
| Managed Identity | IMDS available; optionally `AZURE_CLIENT_ID` for user-assigned MI | In AKS fallback scenarios, the identity must be attached to the node VMSS for IMDS to issue tokens. |
| Developer credentials | Azure CLI, Azure PowerShell, VS Code, etc. | Useful for local interactive runs when SP env vars are not set. |

> In AKS, prefer setting `VMSS_METRICS_AUTH_MODE` in `deploy/kubernetes.yaml`. If you
> choose `service_principal`, provide `AZURE_CLIENT_SECRET` through a Kubernetes Secret
> or external secret provider — do not hardcode it in a manifest.

Unlike a plain `DefaultAzureCredential` / `ChainedTokenCredential`, this chain
**continues on hard authentication failures** (for example AADSTS700211 federated-
credential mismatch, AADSTS53003 conditional-access block, or a missing token file)
rather than aborting after the first credential errors. The first credential that
returns a token is cached for subsequent calls. If it later starts failing, the chain
is automatically re-walked.

> **Note for the MI fallback to actually succeed**: the user-assigned managed identity
> referenced by `AZURE_CLIENT_ID` must also be assigned to the **AKS node VMSS** so
> IMDS can vend a token for it (`az vmss identity assign --identities <MI_ID> …`).
> Workload Identity alone — without the identity attached to the VMSS — only works
> through the federated-token flow.

## Local quick start

Prerequisites:

- Python 3.10+
- Azure CLI signed in with Reader access (or equivalent) to the target subscription(s)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

az login
az account set --subscription <subscription-id>

export AZURE_SUBSCRIPTION_IDS=<subscription-id-1>,<subscription-id-2>

# Optional non-interactive Service Principal auth instead of Azure CLI.
# If all three are set, SP auth takes precedence over AKS Workload Identity.
# export AZURE_CLIENT_ID=<app-or-managed-identity-client-id>
# export AZURE_TENANT_ID=<tenant-id>
# export AZURE_CLIENT_SECRET=<service-principal-secret>

# One-shot inventory summary (no HTTP server):
vmss-metrics-exporter --once

# Long-running exporter:
vmss-metrics-exporter
curl http://localhost:8000/metrics
```

## Makefile targets

All targets honor `IMAGE=<repo>` and `TAG=<tag>` overrides. See `make help` for the full list.

| Target | Purpose |
| --- | --- |
| `make install` | `pip install -e '.[dev]'` into the active venv. |
| `make test` | Run unit tests (`pytest -q`). |
| `make lint` | Run `ruff check .`. |
| `make validate` | `test` + `lint`. |
| `make once` | Run one Resource Graph collection and print a summary. |
| `make run` | Run the exporter locally from Python. |
| `make image` / `make image-no-cache` | Build the container image. |
| `make push` | Push the image. |
| `make docker-run` | Run the container locally on `PORT=$PORT` with `SUBSCRIPTION_IDS=$SUBSCRIPTION_IDS`. |
| `make deploy` | `kubectl apply -f deploy/kubernetes.yaml`. |
| `make deploy-image` | Set the deployment container image to `$IMAGE:$TAG`. |
| `make rollout` | Wait for the deployment rollout to complete. |
| `make logs` | Tail exporter logs from Kubernetes. |
| `make port-forward` | Port-forward the Service to `localhost:$PORT`. |

Typical container release flow:

```bash
make image push IMAGE=myrepo/vmss-metrics-exporter TAG=v1
make deploy
make deploy-image IMAGE=myrepo/vmss-metrics-exporter TAG=v1
make rollout
```

## Deploy on AKS with Microsoft Entra Workload Identity

The sample [deploy/kubernetes.yaml](deploy/kubernetes.yaml) creates:

- `ServiceAccount` `default/workidentity-sa` annotated with `azure.workload.identity/client-id`
- `Deployment` `vmss-metrics-exporter` (labelled `azure.workload.identity/use: "true"`)
- `Service` `vmss-metrics-exporter` exposing port `8000` (port name `metrics`)

Pod environment is set to a default subscription ID; update it for your environment before applying.
It also sets `VMSS_METRICS_AUTH_MODE=workload_identity` by default. To use Service
Principal auth instead, create a Kubernetes Secret with `AZURE_CLIENT_ID`,
`AZURE_TENANT_ID`, and `AZURE_CLIENT_SECRET`, change `VMSS_METRICS_AUTH_MODE` to
`service_principal`, and uncomment the SP `secretKeyRef` env entries in the manifest.

### 1. Enable Workload Identity on the AKS cluster

```bash
az aks update -g <aks-rg> -n <aks-name> \
  --enable-oidc-issuer \
  --enable-workload-identity
```

Capture the cluster's OIDC issuer URL — you will need it verbatim (trailing slash matters):

```bash
AKS_ISSUER=$(az aks show -g <aks-rg> -n <aks-name> --query oidcIssuerProfile.issuerUrl -o tsv)
echo "$AKS_ISSUER"
```

### 2. Create or pick a user-assigned managed identity

```bash
az identity create -g <identity-rg> -n <identity-name>
CLIENT_ID=$(az identity show -g <identity-rg> -n <identity-name> --query clientId -o tsv)
```

### 3. Create the federated identity credential

The federated credential's **issuer**, **subject**, and **audiences** must match exactly what the pod presents:

```bash
az identity federated-credential create \
  --name sa-federation \
  --identity-name <identity-name> \
  --resource-group <identity-rg> \
  --issuer "$AKS_ISSUER" \
  --subject "system:serviceaccount:default:workidentity-sa" \
  --audiences "api://AzureADTokenExchange"
```

> A mismatched issuer URL produces `AADSTS700211: No matching federated identity record found`. If you see that error, double-check the issuer URL is the AKS-managed OIDC issuer (not a previously-used self-hosted blob endpoint).

### 4. Grant Azure RBAC

The exporter calls Azure Resource Graph, which evaluates **Reader** at subscription scope. `Monitoring Reader` is **not** sufficient — it does not grant `Microsoft.Compute/virtualMachineScaleSets/*/read`.

```bash
for SUB in <subscription-id-1> <subscription-id-2>; do
  az role assignment create \
    --assignee "$CLIENT_ID" \
    --role "Reader" \
    --scope "/subscriptions/$SUB"
done
```

  `Reader` at subscription scope is also the simplest supported role for Managed Lustre collection because the exporter must discover AMLFS resources and read Azure Monitor metrics for each discovered filesystem.

### 5. Apply the manifest

Update [deploy/kubernetes.yaml](deploy/kubernetes.yaml) so the SA annotation `azure.workload.identity/client-id` matches your `$CLIENT_ID`, set `AZURE_SUBSCRIPTION_IDS` in the deployment env, then:

```bash
make deploy
make deploy-image IMAGE=<your-repo>/vmss-metrics-exporter TAG=<tag>
make rollout
```

### 6. Verify Workload Identity end-to-end

```bash
POD=$(kubectl get pod -n default -l app.kubernetes.io/name=vmss-metrics-exporter -o jsonpath='{.items[0].metadata.name}')

# Webhook-injected env (token file must be non-empty)
kubectl exec -n default "$POD" -- sh -c 'env | grep ^AZURE_ ; test -s "$AZURE_FEDERATED_TOKEN_FILE" && echo token-file-ok'

# Should show successful collection and no AADSTS / AuthorizationFailed errors
kubectl logs -n default "$POD" --tail=50

# Read the exporter directly from inside the pod
kubectl exec -n default "$POD" -- python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())" \
  | grep -E 'azure_vmss_(instance_count|capacity|exporter_(last_success|collection_errors|vmss_total))'
```

Common failure signatures:

| Symptom in logs | Likely cause |
| --- | --- |
| `AADSTS700211: No matching federated identity record` | Issuer/subject/audience mismatch on the federated credential. |
| `AADSTS53003: Access has been blocked by Conditional Access policies` | Tenant CA policy applies to workload identities; admin must exclude the SP. |
| `AuthorizationFailed` on Resource Graph | Missing/insufficient RBAC; assign `Reader` at subscription scope. |
| `WorkloadIdentityCredential authentication unavailable` | Pod missing `azure.workload.identity/use: "true"` label; webhook did not mutate. |

## Scrape with Azure Monitor managed Prometheus

[deploy/ama-metrics-settings-configmap-v1.yaml](deploy/ama-metrics-settings-configmap-v1.yaml) contains a custom scrape job for the [Azure Monitor managed Prometheus addon](https://learn.microsoft.com/azure/azure-monitor/containers/prometheus-metrics-overview):

- ConfigMap **must** be named `ama-metrics-prometheus-config` and live in `kube-system` — that is the only path the addon reads.
- Uses `role: pod` so each replica is scraped directly; preserves `namespace`, `pod`, and `node` labels.
- Keeps only pods labelled `app.kubernetes.io/name=vmss-metrics-exporter` in `default` with container port name `metrics`.

Prerequisite — managed Prometheus addon enabled on the cluster:

```bash
az aks update -g <aks-rg> -n <aks-name> --enable-azure-monitor-metrics
```

Apply and reload:

```bash
kubectl apply -f deploy/ama-metrics-settings-configmap-v1.yaml
kubectl rollout restart -n kube-system deployment/ama-metrics
kubectl rollout status  -n kube-system deployment/ama-metrics
```

Verify the target is healthy (collector exposes Prometheus' targets API on IPv6 localhost inside the pod):

```bash
AMA_POD=$(kubectl get pod -n kube-system -l rsName=ama-metrics -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n kube-system "$AMA_POD" -c prometheus-collector -- \
  sh -c 'curl -sS "http://[::1]:9090/api/v1/targets?state=active"' \
  | python3 -c "import json,sys;d=json.load(sys.stdin);[print(t['labels'].get('pod'), t['scrapeUrl'], t['health'], t.get('lastError','')) for t in d['data']['activeTargets'] if t['labels'].get('job')=='azure_metrics_vmss']"
```

Expected: at least one line ending with `up` and an empty `lastError`. The sample config scrapes every `30s` so Managed Lustre alerts can evaluate quickly while exporter scrapes remain cached and cheap.

## Managed Lustre real-time alerting

Managed Lustre capacity alerts should use a faster path than VMSS inventory:

- **Exporter poll**: `LUSTRE_POLL_INTERVAL_SECONDS=60` by default. VMSS still uses `POLL_INTERVAL_SECONDS=300`.
- **Azure Monitor metric granularity**: `LUSTRE_METRICS_INTERVAL=PT1M` because the OST capacity metrics support one-minute samples.
- **Prometheus scrape**: `deploy/ama-metrics-settings-configmap-v1.yaml` uses `scrape_interval: 30s`.
- **Alert freshness**: use both exporter freshness and Azure Monitor sample timestamp freshness.

The exporter intentionally exposes separate Lustre health metrics so alerts do not rely on VMSS collector state:

```promql
# Exporter-side Lustre collection freshness
time() - azure_managed_lustre_last_success_timestamp_seconds

# Azure Monitor sample freshness per OST
time() - azure_managed_lustre_ost_sample_timestamp_seconds
```

[deploy/lustre-alert-rules.yaml](deploy/lustre-alert-rules.yaml) contains starter Prometheus alert rules:

- `AzureManagedLustreCollectorStale` — no successful Lustre collection for more than 3 minutes.
- `AzureManagedLustreSampleStale` — an OST sample is older than 5 minutes.
- `AzureManagedLustreCollectionErrors` — collection errors are occurring.
- `AzureManagedLustreOstAvailablePercentLow` — an OST has less than 10% available for 5 minutes.
- `AzureManagedLustreOstAvailablePercentCritical` — an OST has less than 5% available for 1 minute.
- `AzureManagedLustreOstBytesAvailableLow` — an OST has less than 1 TiB available for 5 minutes.
- `AzureManagedLustreOstBytesAvailableCritical` — an OST has less than 100 GiB available for 1 minute.

Prefer the 10% / 5% thresholds for primary capacity alerting because they normalize OSTs of different sizes. Keep or tune the 1 TiB / 100 GiB absolute thresholds as a safety net for workloads where a fixed amount of free space is operationally important. For Azure Managed Prometheus, create equivalent Azure Monitor managed Prometheus rule groups from these PromQL expressions; for self-managed Prometheus or kube-prometheus-stack, load the YAML as a standard rule file.

## Grafana dashboard

[deploy/grafana-dashboard-vmss.json](deploy/grafana-dashboard-vmss.json) is an importable Grafana dashboard (`Azure VMSS Inventory & Trends`, UID `azure-vmss-exporter`). It includes:

- Overview stats: VMSS observed, total VM instances, total desired capacity, capacity-vs-actual drift, exporter freshness, error rate.
- VMSS instance count trend (per VMSS, step-after).
- Total instances by subscription and by region.
- Current snapshot bar gauge and inventory table with computed `drift` column.
- Collapsible capacity-vs-actual overlay (dashed capacity, solid actual).

Cascading template variables: data source → `job` → `subscription_id` → `location` → `resource_group` → `vmss_name`.

[deploy/grafana-dashboard-lustre.json](deploy/grafana-dashboard-lustre.json) is a separate importable Grafana dashboard (`Azure Managed Lustre OST Capacity`, UID `azure-managed-lustre-ost-capacity`). It includes:

- Overview stats: filesystems observed, OST series observed, total OST bytes available, lowest OST available percentage, Lustre collector freshness, max Azure Monitor sample age, Lustre error rate, and collection duration.
- Total bytes available trend by filesystem.
- Bottom-N per-OST trend for the OSTs closest to capacity pressure, ranked by `min` or `avg` available percentage.
- Rollups by resource group, subscription, and region.
- Current snapshot bar gauge focused on the lowest available-percentage OSTs and a table with available/used/total bytes plus available percentage.

Lustre dashboard variables: data source → `job` → `subscription_id` → `location` → `resource_group` → `filesystem_name` → `ostnum`.

Import directly into Azure Managed Grafana or any Grafana 10+:

1. **Dashboards → New → Import**.
2. Upload the JSON.
3. Pick the Prometheus data source pointed at your Azure Monitor Workspace.

For sidecar-based Grafana (e.g. `kube-prometheus-stack`):

```bash
kubectl -n <grafana-ns> create configmap grafana-dashboard-vmss \
  --from-file=vmss.json=deploy/grafana-dashboard-vmss.json \
  --from-file=lustre.json=deploy/grafana-dashboard-lustre.json \
  --dry-run=client -o yaml \
  | kubectl label -f - --local -o yaml grafana_dashboard=1 \
  | kubectl apply -f -
```

(Use whatever label your sidecar watches for.)

## PromQL examples

```promql
# Current VMSS instance counts
azure_vmss_instance_count

# Total VMSS instances by subscription
sum by (subscription_id) (azure_vmss_instance_count)

# VMSS where desired capacity differs from actual VM count
azure_vmss_capacity != azure_vmss_instance_count

# Instances by region
sum by (subscription_id, location) (azure_vmss_instance_count)

# Instance count enriched with VM size and SKU tier
azure_vmss_instance_count
  * on (subscription_id, resource_group, vmss_name) group_left(vm_size, sku_tier)
    azure_vmss_info

# Instances grouped by VM size
sum by (vm_size) (
  azure_vmss_instance_count
    * on (subscription_id, resource_group, vmss_name) group_left(vm_size)
      azure_vmss_info
)

# Exporter freshness
time() - azure_vmss_exporter_last_success_timestamp_seconds

# Collection error rate
rate(azure_vmss_exporter_collection_errors_total[5m])

# Managed Lustre OST bytes available by filesystem and OST
azure_managed_lustre_ost_bytes_available

# Managed Lustre OST available percentage by filesystem and OST
azure_managed_lustre_ost_bytes_available_percent

# Managed Lustre OST used and total bytes by filesystem and OST
azure_managed_lustre_ost_bytes_used
azure_managed_lustre_ost_bytes_total

# Total Managed Lustre bytes available by filesystem
sum by (subscription_id, resource_group, filesystem_name) (
  azure_managed_lustre_ost_bytes_available
)

# Managed Lustre collection error rate
rate(azure_managed_lustre_collection_errors_total[5m])

# Managed Lustre collection freshness in seconds
time() - azure_managed_lustre_last_success_timestamp_seconds

# Managed Lustre Azure Monitor sample age by OST
time() - azure_managed_lustre_ost_sample_timestamp_seconds

# OSTs below 1 TiB available
azure_managed_lustre_ost_bytes_available < 1099511627776

# OSTs below 5% available
azure_managed_lustre_ost_bytes_available_percent < 5
```

## Operational notes

- The exporter polls VMSS inventory every `POLL_INTERVAL_SECONDS` (default `300`) and Managed Lustre metrics every `LUSTRE_POLL_INTERVAL_SECONDS` (default `60`). Prometheus scrapes return cached gauge values and do not trigger Azure API calls.
- Deleted VMSS label sets are removed from the exporter on the next successful collection.
- Deleted Azure Managed Lustre filesystems or OST label sets are removed from the exporter on the next fully successful Managed Lustre collection. During partial Azure Monitor failures, existing Lustre series are retained and freshness/error metrics indicate the issue, avoiding silent loss of capacity signals.
- Azure Resource Graph has minor indexing latency; expect a brief delay before counts reflect the final state of rapid scale events.
- Azure Managed Lustre metric values come from Azure Monitor and can lag behind resource discovery briefly; the exporter uses a lookback window and skips OST series with no non-null datapoint rather than emitting misleading zeroes.
- `azure_vmss_instance_count` is the **actual** child VM resource count; `azure_vmss_capacity` is the **desired** capacity. Use both to detect transitions and provisioning gaps.

## Development

```bash
pytest          # run unit tests
ruff check .    # lint
make validate   # both
```
