# Azure VMSS Metrics Exporter

Python Prometheus exporter that periodically inventories every Azure VM Scale Set (VMSS) in one or more subscriptions and exposes their names, desired capacity, and actual child-VM counts as Prometheus gauges.

Azure Monitor does not provide a simple native subscription-wide metric for "VMSS name and current instance count". This exporter treats the problem as Azure inventory/state sampling: it uses **Azure Resource Graph** to query all configured subscriptions in one request pattern and exposes the result as cached gauges on `/metrics`.

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
│   └── grafana-dashboard-vmss.json       # importable Grafana dashboard
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
| `ARG_PAGE_SIZE` | _(library default)_ | Optional Resource Graph page size. |
| `ARG_MAX_RETRIES` | _(library default)_ | Optional retry count for transient errors. |
| `ARG_RETRY_BASE_DELAY_SECONDS` | _(library default)_ | Optional retry backoff base. |

Authentication uses a resilient credential chain implemented in
[`src/vmss_metrics_exporter/credentials.py`](src/vmss_metrics_exporter/credentials.py):

1. **Workload Identity** — tried first when the AKS webhook has injected the
   `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_FEDERATED_TOKEN_FILE` env vars.
2. **Managed Identity (user-assigned)** — via IMDS, using `AZURE_CLIENT_ID` if set.
3. **Managed Identity (system-assigned)** — via IMDS.
4. **`DefaultAzureCredential`** with WI/MI disabled — for local development (Azure CLI,
   environment variables, etc.).

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

Expected: at least one line ending with `up` and an empty `lastError`. Metrics flow into the linked Azure Monitor Workspace within ~1 minute.

## Grafana dashboard

[deploy/grafana-dashboard-vmss.json](deploy/grafana-dashboard-vmss.json) is an importable Grafana dashboard (`Azure VMSS Inventory & Trends`, UID `azure-vmss-exporter`). It includes:

- Overview stats: VMSS observed, total VM instances, total desired capacity, capacity-vs-actual drift, exporter freshness, error rate.
- VMSS instance count trend (per VMSS, step-after).
- Total instances by subscription and by region.
- Current snapshot bar gauge and inventory table with computed `drift` column.
- Collapsible capacity-vs-actual overlay (dashed capacity, solid actual).

Cascading template variables: data source → `job` → `subscription_id` → `location` → `resource_group` → `vmss_name`.

Import directly into Azure Managed Grafana or any Grafana 10+:

1. **Dashboards → New → Import**.
2. Upload the JSON.
3. Pick the Prometheus data source pointed at your Azure Monitor Workspace.

For sidecar-based Grafana (e.g. `kube-prometheus-stack`):

```bash
kubectl -n <grafana-ns> create configmap grafana-dashboard-vmss \
  --from-file=vmss.json=deploy/grafana-dashboard-vmss.json \
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
```

## Operational notes

- The exporter polls Azure every `POLL_INTERVAL_SECONDS` (default `300`). Prometheus scrapes return cached gauge values and do not trigger Azure API calls.
- Deleted VMSS label sets are removed from the exporter on the next successful collection.
- Azure Resource Graph has minor indexing latency; expect a brief delay before counts reflect the final state of rapid scale events.
- `azure_vmss_instance_count` is the **actual** child VM resource count; `azure_vmss_capacity` is the **desired** capacity. Use both to detect transitions and provisioning gaps.

## Development

```bash
pytest          # run unit tests
ruff check .    # lint
make validate   # both
```
