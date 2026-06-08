# MetricMesh Helm chart (MM-11.5)

Deploys MetricMesh to Kubernetes: the FastAPI **API**, one Celery **worker**
Deployment per queue (`fast`/`slow`/`alerts`), the **Beat** scheduler (singleton),
and **Flower** — with liveness/readiness probes, HPA, and externalized secrets.
TimescaleDB and Redis are bundled for a self-contained demo and can be turned off
to point at managed services.

## Prerequisites

- A Kubernetes cluster + `kubectl`, and Helm 3/4.
- The app image built and pushed to a registry your cluster can pull:

  ```bash
  docker build -t <registry>/metricmesh:0.1.0 .
  docker push <registry>/metricmesh:0.1.0
  ```

  (For local clusters you can side-load instead, e.g. `kind load docker-image` /
  `minikube image load`.)
- For HPA, the **metrics-server** must be installed in the cluster.

## Install

```bash
helm install mm ./deploy/helm/metricmesh \
  --namespace metricmesh --create-namespace \
  --set image.repository=<registry>/metricmesh \
  --set image.tag=0.1.0
```

Reach it:

```bash
kubectl -n metricmesh port-forward svc/mm-api 8000:8000
curl http://localhost:8000/health
```

## Validate without a cluster

```bash
helm lint ./deploy/helm/metricmesh
helm template mm ./deploy/helm/metricmesh        # render all manifests
```

## Configuration

See `values.yaml` for the full list. Common overrides:

| Setting | Purpose |
|---|---|
| `image.repository` / `image.tag` | app image (build from the repo `Dockerfile`) |
| `api.replicas`, `api.hpa.*` | API scaling |
| `workers[*].{replicas,concurrency,hpa}` | per-queue worker scaling |
| `timescaledb.enabled` / `redis.enabled` | set `false` to use managed services |
| `secrets.apiKeys`, `secrets.tenantApiKeys` | enable auth / multi-tenancy (MM-9.1/9.3) |
| `app.*` feature flags | rate limit, consensus, audit, scoring mode — all default to the app's safe defaults |

### Secrets

Non-secret config is rendered into a **ConfigMap**; credentials (DB URLs, API
keys, webhook URLs) into a **Secret** (`mm-secrets`), injected via `envFrom`.
For production, override `secrets.*` / `timescaledb.password`, or replace the
Secret with one managed by [external-secrets](https://external-secrets.io/) /
[sealed-secrets](https://sealed-secrets.netlify.app/). The app can also read each
field from a mounted secrets dir (`SECRETS_DIR`, MM-9.4) if you prefer file-based
secret injection.

## Known limitation — worker metrics

In docker-compose, the API and workers share a host volume so the single
`/metrics` endpoint aggregates every process via `prometheus_client`
multiprocess mode. In Kubernetes, pods don't share a filesystem, so each API pod
exposes only its own metrics (pod template carries `prometheus.io/scrape`
annotations). **Per-worker metrics are not merged into the API's `/metrics`
here.** To collect worker metrics in-cluster, run a metrics exporter sidecar per
worker pod (or a Pushgateway) and scrape those — a deliberate follow-up, not
wired up by this chart.

## TLS / ingress

This chart exposes ClusterIP Services only. Terminate TLS and route external
traffic at an Ingress / gateway in front of `mm-api` (NFR-SEC5).
