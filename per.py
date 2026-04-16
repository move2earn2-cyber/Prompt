#!/usr/bin/env python3
"""
falcon_kac_perf_assessment.py
Performance Impact Assessment – CrowdStrike Falcon KAC (Helm deployment)

Targets:
  - Deployment:  falcon-kac          (namespace: falcon-kac by default)
  - Pods:        falcon-kac-<hash>   (2 containers: falcon-ac + falcon-client)
  - Webhook:     ValidatingWebhookConfiguration with "falcon" in name
  - Helm labels: app.kubernetes.io/instance=<release>, helm.sh/chart=falcon-kac-*

Usage:
  # Step 1 – baseline BEFORE deploying
  python3 falcon_kac_perf_assessment.py --mode baseline --output /tmp/kac_baseline.json

  # Step 2 – post-deploy report (HTML + DOCX)
  python3 falcon_kac_perf_assessment.py --mode post \
      --baseline /tmp/kac_baseline.json \
      --output /tmp/kac_report \
      [--namespace falcon-kac] \
      [--release falcon-kac]

  # Continuous sampling
  python3 falcon_kac_perf_assessment.py --mode sample \
      --samples 10 --interval 30 --output /tmp/kac_samples.json

Dependencies:
  pip install jinja2 plotly kaleido python-docx
"""

import argparse
import json
import subprocess
import sys
import time
import datetime
import os
from pathlib import Path

try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

try:
    from jinja2 import Template
    HAS_JINJA = True
except ImportError:
    HAS_JINJA = False

try:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULTS  (override with CLI flags or --namespace / --release)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_NAMESPACE    = "falcon-kac"
DEFAULT_RELEASE      = "falcon-kac"
DEPLOY_NAME          = "falcon-kac"
WEBHOOK_PATTERN      = "falcon"

# Container names inside the KAC pod (from CrowdStrike helm chart)
CONTAINER_AC_NAME    = "falcon-ac"       # admission controller container
CONTAINER_CLIENT     = "falcon-client"   # client/proxy container


# ─────────────────────────────────────────────────────────────────────────────
# KUBECTL HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def run_kubectl(args: list, timeout: int = 30):
    cmd = ["kubectl"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", f"kubectl timeout after {timeout}s", 1
    except FileNotFoundError:
        return "", "kubectl not found in PATH", 1


def kubectl_json(args: list):
    stdout, stderr, rc = run_kubectl(args + ["-o", "json"])
    if rc != 0:
        print(f"  [WARN] {stderr.strip()}", file=sys.stderr)
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def kubectl_raw(path: str) -> str:
    stdout, _, _ = run_kubectl(["get", "--raw", path])
    return stdout


# ─────────────────────────────────────────────────────────────────────────────
# CLUSTER IDENTITY
# ─────────────────────────────────────────────────────────────────────────────
def collect_cluster_info() -> dict:
    info = {
        "cluster_name":  "unknown",
        "context":       "unknown",
        "server_url":    "unknown",
        "k8s_version":   "unknown",
        "k8s_platform":  "unknown",
        "node_count":    "N/A",
        "worker_count":  "N/A",
    }

    stdout, _, rc = run_kubectl(["config", "current-context"])
    if rc == 0:
        info["context"] = stdout.strip()

    cfg = kubectl_json(["config", "view"])
    if cfg:
        for ctx in cfg.get("contexts", []):
            if ctx.get("name") == info["context"]:
                info["cluster_name"] = ctx.get("context", {}).get("cluster", info["context"])
                break
        for cl in cfg.get("clusters", []):
            if cl.get("name") == info["cluster_name"]:
                info["server_url"] = cl.get("cluster", {}).get("server", "unknown")
                break

    ver = kubectl_json(["version"])
    if ver:
        sv  = ver.get("serverVersion", {})
        info["k8s_version"] = sv.get("gitVersion", "unknown")
        gv  = sv.get("gitVersion", "").lower()
        if   "eks"       in gv: info["k8s_platform"] = "Amazon EKS"
        elif "gke"       in gv: info["k8s_platform"] = "Google GKE"
        elif "aks"       in gv: info["k8s_platform"] = "Azure AKS"
        elif "openshift" in gv: info["k8s_platform"] = "OpenShift"
        else:
            nodes_obj = kubectl_json(["get", "nodes"])
            if nodes_obj:
                for n in nodes_obj.get("items", []):
                    pid = n.get("spec", {}).get("providerID", "")
                    if   "eks"   in pid: info["k8s_platform"] = "Amazon EKS";   break
                    elif "gce"   in pid: info["k8s_platform"] = "Google GKE";   break
                    elif "azure" in pid: info["k8s_platform"] = "Azure AKS";    break
                    elif n.get("metadata", {}).get("labels", {}).get("node.openshift.io/os_id"):
                        info["k8s_platform"] = "OpenShift"; break
                else:
                    info["k8s_platform"] = "Vanilla / On-Prem"

    nodes_obj = kubectl_json(["get", "nodes"])
    if nodes_obj:
        items = nodes_obj.get("items", [])
        info["node_count"]   = len(items)
        info["worker_count"] = sum(
            1 for n in items
            if not n.get("metadata", {}).get("labels", {}).get("node-role.kubernetes.io/control-plane")
            and not n.get("metadata", {}).get("labels", {}).get("node-role.kubernetes.io/master")
        )

    return info


# ─────────────────────────────────────────────────────────────────────────────
# HELM RELEASE INFO
# ─────────────────────────────────────────────────────────────────────────────
def collect_helm_release(namespace: str, release: str) -> dict:
    """
    Pull Helm release metadata via `helm status` and `helm get values`.
    Falls back gracefully if Helm CLI is not available.
    """
    info = {
        "release":     release,
        "namespace":   namespace,
        "chart":       "unknown",
        "chart_version": "unknown",
        "app_version": "unknown",
        "status":      "unknown",
        "deployed_at": "unknown",
        "values_summary": {},
    }

    # helm status
    try:
        r = subprocess.run(
            ["helm", "status", release, "-n", namespace, "--output", "json"],
            capture_output=True, text=True, timeout=20
        )
        if r.returncode == 0:
            hs = json.loads(r.stdout)
            info["chart"]         = hs.get("chart", {}).get("metadata", {}).get("name", "unknown")
            info["chart_version"] = hs.get("chart", {}).get("metadata", {}).get("version", "unknown")
            info["app_version"]   = hs.get("chart", {}).get("metadata", {}).get("appVersion", "unknown")
            info["status"]        = hs.get("info", {}).get("status", "unknown")
            info["deployed_at"]   = hs.get("info", {}).get("last_deployed", "unknown")
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # helm get values — extract resource config if set
    try:
        r = subprocess.run(
            ["helm", "get", "values", release, "-n", namespace, "--output", "json"],
            capture_output=True, text=True, timeout=20
        )
        if r.returncode == 0:
            vals = json.loads(r.stdout)
            # Surface the most performance-relevant values
            info["values_summary"] = {
                "replicas":          vals.get("replicas", "default"),
                "resources_ac":      vals.get("resources", {}).get("falcon-ac", {}),
                "resources_client":  vals.get("resources", {}).get("falcon-client", {}),
                "failurePolicy":     vals.get("webhook", {}).get("failurePolicy", "not set"),
                "timeoutSeconds":    vals.get("webhook", {}).get("timeoutSeconds", "not set"),
                "priorityClassName": vals.get("priorityClassName", "not set"),
            }
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    return info


# ─────────────────────────────────────────────────────────────────────────────
# KAC-SPECIFIC METRIC COLLECTION
# ─────────────────────────────────────────────────────────────────────────────
def collect_kac_deployment(namespace: str) -> dict:
    """Full deployment object for falcon-kac."""
    dep = kubectl_json(["get", "deployment", DEPLOY_NAME, "-n", namespace])
    if not dep:
        return {}
    status = dep.get("status", {})
    spec   = dep.get("spec",   {})
    # Extract per-container resource requests/limits from pod spec
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    container_resources = {}
    for c in containers:
        name = c.get("name", "unknown")
        res  = c.get("resources", {})
        container_resources[name] = {
            "requests": res.get("requests", {}),
            "limits":   res.get("limits",   {}),
        }
    return {
        "namespace":           namespace,
        "name":                DEPLOY_NAME,
        "desired":             spec.get("replicas", 1),
        "ready":               status.get("readyReplicas", 0),
        "available":           status.get("availableReplicas", 0),
        "updated":             status.get("updatedReplicas", 0),
        "unavailable":         status.get("unavailableReplicas", 0),
        "container_resources": container_resources,
        "conditions": [
            {"type": c["type"], "status": c["status"], "reason": c.get("reason", "")}
            for c in status.get("conditions", [])
        ],
    }


def collect_kac_pods(namespace: str) -> list:
    """
    Per-pod and per-container resource usage for falcon-kac pods.
    Uses kubectl top pods --containers for per-container breakdown.
    """
    pods = []

    # Pod-level totals
    stdout, _, rc = run_kubectl(["top", "pods", "-n", namespace, "--no-headers"])
    pod_totals = {}
    if rc == 0:
        for line in stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                pod_totals[parts[0]] = {
                    "cpu_cores": _parse_cpu(parts[1]),
                    "mem_mib":   _parse_mem(parts[2]),
                }

    # Per-container breakdown
    stdout, _, rc = run_kubectl(
        ["top", "pods", "-n", namespace, "--containers", "--no-headers"]
    )
    pod_containers = {}
    if rc == 0:
        for line in stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                pname, cname, cpu_raw, mem_raw = parts[0], parts[1], parts[2], parts[3]
                if pname not in pod_containers:
                    pod_containers[pname] = []
                pod_containers[pname].append({
                    "container": cname,
                    "cpu_cores": _parse_cpu(cpu_raw),
                    "mem_mib":   _parse_mem(mem_raw),
                })

    # Get pod metadata (node, restart counts, age)
    pods_obj = kubectl_json(["get", "pods", "-n", namespace,
                             "-l", f"app.kubernetes.io/instance={DEFAULT_RELEASE}"])
    pod_meta = {}
    if pods_obj:
        for p in pods_obj.get("items", []):
            pname = p["metadata"]["name"]
            cs    = p.get("status", {}).get("containerStatuses", [])
            pod_meta[pname] = {
                "node":     p.get("spec", {}).get("nodeName", "unknown"),
                "phase":    p.get("status", {}).get("phase", "unknown"),
                "restarts": sum(c.get("restartCount", 0) for c in cs),
                "age":      p.get("metadata", {}).get("creationTimestamp", ""),
            }

    # Merge
    all_pod_names = set(list(pod_totals.keys()) + list(pod_meta.keys()))
    for pname in all_pod_names:
        if not pname.startswith("falcon-kac"):
            continue
        totals = pod_totals.get(pname, {"cpu_cores": 0, "mem_mib": 0})
        meta   = pod_meta.get(pname, {})
        pods.append({
            "pod":        pname,
            "namespace":  namespace,
            "node":       meta.get("node", "unknown"),
            "phase":      meta.get("phase", "unknown"),
            "restarts":   meta.get("restarts", 0),
            "cpu_cores":  totals["cpu_cores"],
            "mem_mib":    totals["mem_mib"],
            "containers": pod_containers.get(pname, []),
        })

    return pods


def collect_webhook_config() -> dict:
    vwcs = kubectl_json(["get", "validatingwebhookconfigurations"])
    if not vwcs:
        return {}
    for item in vwcs.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        if WEBHOOK_PATTERN in name.lower():
            webhooks = item.get("webhooks", [])
            return {
                "name": name,
                "webhooks": [
                    {
                        "name":             wh.get("name", ""),
                        "failurePolicy":    wh.get("failurePolicy", "unknown"),
                        "timeoutSeconds":   wh.get("timeoutSeconds", "default"),
                        "matchPolicy":      wh.get("matchPolicy", "Equivalent"),
                        "sideEffects":      wh.get("sideEffects", "None"),
                        "namespaceSelector": bool(wh.get("namespaceSelector")),
                        "objectSelector":   bool(wh.get("objectSelector")),
                        "rules":            len(wh.get("rules", [])),
                        "admissionReviewVersions": wh.get("admissionReviewVersions", []),
                    }
                    for wh in webhooks
                ],
            }
    return {}


def collect_kac_admission_latency() -> dict:
    """Parse apiserver /metrics for webhook latency specific to falcon-kac."""
    raw = kubectl_raw("/metrics")
    lat = {"p50": None, "p95": None, "p99": None, "sample_count": 0, "mean_ms": None}
    if not raw:
        return lat
    buckets, total_count, total_sum = {}, 0, 0.0
    for line in raw.splitlines():
        if "apiserver_admission_webhook_admission_duration_seconds" not in line:
            continue
        if WEBHOOK_PATTERN not in line.lower():
            continue
        if line.startswith("#"):
            continue
        if "_bucket{" in line:
            try:
                le_s = line.index('le="') + 4
                le_e = line.index('"', le_s)
                le_v = line[le_s:le_e]
                val  = float(line.split()[-1])
                if le_v != "+Inf":
                    buckets[float(le_v)] = val
            except (ValueError, IndexError):
                pass
        elif "_count{" in line:
            try:   total_count = int(float(line.split()[-1]))
            except (ValueError, IndexError): pass
        elif "_sum{" in line:
            try:   total_sum = float(line.split()[-1])
            except (ValueError, IndexError): pass
    if total_count > 0:
        lat["sample_count"] = total_count
        lat["mean_ms"] = round(total_sum / total_count * 1000, 2)
    if buckets and total_count > 0:
        for pct, key in [(0.5, "p50"), (0.95, "p95"), (0.99, "p99")]:
            target = pct * total_count
            for bound in sorted(buckets):
                if buckets[bound] >= target:
                    lat[key] = round(bound * 1000, 2)
                    break
    return lat


def collect_kac_hpa(namespace: str) -> dict:
    """Check if a HorizontalPodAutoscaler exists for falcon-kac."""
    hpa = kubectl_json(["get", "hpa", DEPLOY_NAME, "-n", namespace])
    if not hpa:
        return {}
    spec   = hpa.get("spec",   {})
    status = hpa.get("status", {})
    return {
        "min_replicas": spec.get("minReplicas", 1),
        "max_replicas": spec.get("maxReplicas", 1),
        "current":      status.get("currentReplicas", 0),
        "desired":      status.get("desiredReplicas", 0),
        "metrics":      [m.get("type") for m in spec.get("metrics", [])],
    }


def collect_resource_quota(namespace: str) -> dict:
    """Check ResourceQuota in the falcon-kac namespace."""
    rqs = kubectl_json(["get", "resourcequota", "-n", namespace])
    if not rqs or not rqs.get("items"):
        return {}
    out = {}
    for rq in rqs.get("items", []):
        name   = rq["metadata"]["name"]
        status = rq.get("status", {})
        out[name] = {
            "hard": status.get("hard", {}),
            "used": status.get("used", {}),
        }
    return out


def collect_events(namespace: str) -> list:
    evts = kubectl_json(["get", "events", "-n", namespace,
                         "--sort-by=.lastTimestamp"])
    if not evts:
        return []
    out = []
    for e in evts.get("items", [])[-20:]:
        out.append({
            "type":     e.get("type", "Normal"),
            "reason":   e.get("reason", ""),
            "message":  e.get("message", "")[:150],
            "count":    e.get("count", 1),
            "last_ts":  e.get("lastTimestamp", ""),
            "object":   e.get("involvedObject", {}).get("name", ""),
        })
    return [e for e in out if e["type"] == "Warning"] or out[-5:]


# ─────────────────────────────────────────────────────────────────────────────
# PARSING HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _parse_cpu(raw: str) -> float:
    raw = raw.strip()
    if raw.endswith("m"):
        return float(raw[:-1]) / 1000
    try:   return float(raw)
    except ValueError: return 0.0


def _parse_mem(raw: str) -> float:
    raw = raw.strip()
    if   raw.endswith("Ki"): return float(raw[:-2]) / 1024
    elif raw.endswith("Mi"): return float(raw[:-2])
    elif raw.endswith("Gi"): return float(raw[:-2]) * 1024
    elif raw.endswith("Ti"): return float(raw[:-2]) * 1024 * 1024
    try:   return float(raw) / (1024 * 1024)
    except ValueError: return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOT  (phase-aware — baseline skips all KAC-specific calls)
# ─────────────────────────────────────────────────────────────────────────────
def collect_baseline_snapshot(namespace: str, release: str) -> dict:
    """
    Collect ONLY cluster-wide metrics that exist BEFORE falcon-kac is installed.
    Intentionally skips: helm status, kac deployment, kac pods, webhook, hpa, quota.
    """
    print(f"\n[BASELINE] {datetime.datetime.now().isoformat()}")
    print("  NOTE: Only collecting cluster-wide metrics — falcon-kac not yet installed.")

    snap = {
        "label":             "baseline",
        "timestamp":         datetime.datetime.now().isoformat(),
        "namespace":         namespace,
        "release":           release,
        # KAC fields intentionally empty at baseline
        "cluster_info":      {},
        "helm_release":      {},
        "kac_deployment":    {},
        "kac_pods":          [],
        "webhook_config":    {},
        "admission_latency": {},
        "hpa":               {},
        "resource_quota":    {},
        "events":            [],
        # Cluster-wide fields populated at baseline
        "node_resources":    [],
        "node_capacity":     [],
        "all_pods":          [],
        "apiserver_latency": {},
    }

    print("  → Cluster identity ...")
    snap["cluster_info"] = collect_cluster_info()
    ci = snap["cluster_info"]
    print(f"     Cluster : {ci['cluster_name']}  |  Platform: {ci['k8s_platform']}  |  K8s: {ci['k8s_version']}")
    print(f"     Nodes   : {ci.get('node_count','?')} total / {ci.get('worker_count','?')} workers")

    print("  → Node resource usage (kubectl top nodes) ...")
    snap["node_resources"] = collect_node_resources()

    print("  → Node capacity / allocatable ...")
    snap["node_capacity"] = collect_node_capacity()

    print("  → All pod resource usage (kubectl top pods -A) ...")
    snap["all_pods"] = collect_pod_resources()

    print("  → API server baseline latency from /metrics ...")
    snap["apiserver_latency"] = collect_apiserver_baseline_latency()

    print(f"\n✅  Baseline captured ({len(snap['node_resources'])} nodes, "
          f"{len(snap['all_pods'])} pods total)")
    print("     Now run: helm upgrade --install falcon-kac ...")
    print("     Then re-run with --mode post to generate the comparison report.\n")

    return snap


def collect_post_snapshot(namespace: str, release: str) -> dict:
    """
    Collect full metrics AFTER falcon-kac is installed.
    Includes all KAC-specific resources + cluster-wide metrics for delta computation.
    """
    print(f"\n[POST-DEPLOY] {datetime.datetime.now().isoformat()}")

    snap = {
        "label":             "post",
        "timestamp":         datetime.datetime.now().isoformat(),
        "namespace":         namespace,
        "release":           release,
        "cluster_info":      {},
        "helm_release":      {},
        "kac_deployment":    {},
        "kac_pods":          [],
        "webhook_config":    {},
        "admission_latency": {},
        "hpa":               {},
        "resource_quota":    {},
        "events":            [],
        "node_resources":    [],
        "node_capacity":     [],
        "all_pods":          [],
        "apiserver_latency": {},
    }

    print("  → Cluster identity ...")
    snap["cluster_info"] = collect_cluster_info()
    ci = snap["cluster_info"]
    print(f"     Cluster : {ci['cluster_name']}  |  Platform: {ci['k8s_platform']}  |  K8s: {ci['k8s_version']}")

    print(f"  → Helm release [{release}] in namespace [{namespace}] ...")
    snap["helm_release"] = collect_helm_release(namespace, release)
    hr = snap["helm_release"]
    if hr.get("status") not in ("deployed", "unknown"):
        print(f"  [WARN] Helm release status is '{hr.get('status')}' — "
              "KAC may not be fully deployed yet.")
    print(f"     Chart: {hr['chart']}-{hr['chart_version']}  "
          f"|  Status: {hr['status']}  |  AppVersion: {hr['app_version']}")

    print("  → Waiting for KAC deployment to be available ...")
    _wait_for_kac_ready(namespace)

    print("  → KAC Deployment status ...")
    snap["kac_deployment"] = collect_kac_deployment(namespace)

    print("  → KAC pod resources (kubectl top pods --containers) ...")
    snap["kac_pods"] = collect_kac_pods(namespace)

    print("  → ValidatingWebhookConfiguration ...")
    snap["webhook_config"] = collect_webhook_config()

    print("  → Admission webhook latency from apiserver /metrics ...")
    snap["admission_latency"] = collect_kac_admission_latency()

    print("  → Node resource usage (post-deploy) ...")
    snap["node_resources"] = collect_node_resources()

    print("  → Node capacity / allocatable ...")
    snap["node_capacity"] = collect_node_capacity()

    print("  → All pod resource usage (post-deploy) ...")
    snap["all_pods"] = collect_pod_resources()

    print("  → API server latency (post-deploy) ...")
    snap["apiserver_latency"] = collect_apiserver_baseline_latency()

    print("  → HPA (if configured) ...")
    snap["hpa"] = collect_kac_hpa(namespace)

    print("  → ResourceQuota ...")
    snap["resource_quota"] = collect_resource_quota(namespace)

    print("  → Events ...")
    snap["events"] = collect_events(namespace)

    return snap


def collect_samples(n: int, interval_s: int, namespace: str, release: str) -> list:
    samples = []
    for i in range(n):
        print(f"\n── Sample {i+1}/{n} ──────────────────────────────────────────")
        samples.append(collect_post_snapshot(namespace, release))
        if i < n - 1:
            print(f"  Sleeping {interval_s}s ...")
            time.sleep(interval_s)
    return samples


def _wait_for_kac_ready(namespace: str, timeout_s: int = 120):
    """
    Poll kubectl rollout status for up to timeout_s seconds.
    Non-fatal — just warns if the deployment isn't ready in time.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        stdout, _, rc = run_kubectl(
            ["rollout", "status", "deployment", DEPLOY_NAME,
             "-n", namespace, "--timeout=10s"]
        )
        if rc == 0 and "successfully rolled out" in stdout.lower():
            print(f"     KAC deployment is ready.")
            return
        remaining = int(deadline - time.time())
        print(f"     Waiting for KAC rollout ... ({remaining}s remaining)")
        time.sleep(10)
    print(f"  [WARN] KAC deployment not ready after {timeout_s}s — "
          "metrics may be incomplete.")


def collect_node_resources() -> list:
    stdout, stderr, rc = run_kubectl(["top", "nodes", "--no-headers"])
    nodes = []
    if rc != 0:
        print(f"  [WARN] top nodes failed: {stderr.strip()}", file=sys.stderr)
        return nodes
    for line in stdout.strip().splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        nodes.append({
            "name":      parts[0],
            "cpu_cores": _parse_cpu(parts[1]),
            "mem_mib":   _parse_mem(parts[3]),
        })
    return nodes


def collect_pod_resources(namespace: str = "") -> list:
    args = ["top", "pods", "--no-headers"]
    args += ["-n", namespace] if namespace else ["-A"]
    stdout, stderr, rc = run_kubectl(args)
    pods = []
    if rc != 0:
        print(f"  [WARN] top pods failed: {stderr.strip()}", file=sys.stderr)
        return pods
    for line in stdout.strip().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if namespace:
            ns, pod, cpu_raw, mem_raw = namespace, parts[0], parts[1], parts[2]
        else:
            ns, pod, cpu_raw, mem_raw = parts[0], parts[1], parts[2], parts[3]
        pods.append({
            "namespace": ns, "pod": pod,
            "cpu_cores": _parse_cpu(cpu_raw),
            "mem_mib":   _parse_mem(mem_raw),
        })
    return pods


def collect_node_capacity() -> list:
    nodes_obj = kubectl_json(["get", "nodes"])
    if not nodes_obj:
        return []
    result = []
    for item in nodes_obj.get("items", []):
        name        = item["metadata"]["name"]
        allocatable = item.get("status", {}).get("allocatable", {})
        capacity    = item.get("status", {}).get("capacity", {})
        result.append({
            "name":                name,
            "allocatable_cpu":     _parse_cpu(allocatable.get("cpu", "0")),
            "allocatable_mem_mib": _parse_mem(allocatable.get("memory", "0Ki")),
            "capacity_cpu":        _parse_cpu(capacity.get("cpu", "0")),
            "capacity_mem_mib":    _parse_mem(capacity.get("memory", "0Ki")),
        })
    return result


def collect_apiserver_baseline_latency() -> dict:
    """Collect general API server POST/CREATE p99 latency as a cluster baseline."""
    raw = kubectl_raw("/metrics")
    result = {"post_p99_bound_ms": None, "create_p99_bound_ms": None}
    if not raw:
        return result
    for line in raw.splitlines():
        if "apiserver_request_duration_seconds_bucket" not in line:
            continue
        if line.startswith("#"):
            continue
        for verb in ("POST", "CREATE"):
            if f'verb="{verb}"' not in line:
                continue
            try:
                le_s = line.index('le="') + 4
                le_e = line.index('"', le_s)
                le_v = line[le_s:le_e]
                if le_v != "+Inf":
                    key = f"{verb.lower()}_p99_bound_ms"
                    result[key] = round(float(le_v) * 1000, 2)
            except (ValueError, IndexError):
                pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI  (updated to call the right snapshot function per mode)
# ─────────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Falcon KAC (Helm) Performance Impact Assessment",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Workflow:
  Step 1 — Capture baseline BEFORE helm install (no falcon-kac required):
    python3 falcon_kac_perf_assessment.py --mode baseline \\
        --output /tmp/kac_baseline.json

  Step 2 — Install falcon-kac via Helm:
    helm upgrade --install falcon-kac crowdstrike/falcon-kac \\
        -n falcon-kac --create-namespace \\
        --set falcon.cid=$FALCON_CID \\
        --set image.repository=$KAC_IMAGE_REPO \\
        --set image.tag=$KAC_IMAGE_TAG

  Step 3 — Generate comparison report (writes .html + .docx):
    python3 falcon_kac_perf_assessment.py --mode post \\
        --baseline /tmp/kac_baseline.json \\
        --output /tmp/kac_report \\
        --namespace falcon-kac \\
        --release falcon-kac

  Optional — Continuous sampling after install:
    python3 falcon_kac_perf_assessment.py --mode sample \\
        --samples 12 --interval 60 --output /tmp/kac_samples.json
        """,
    )
    p.add_argument("--mode",      required=True, choices=["baseline", "post", "sample"],
                   help=("baseline: cluster snapshot BEFORE falcon-kac install\\n"
                         "post:     full report AFTER falcon-kac install\\n"
                         "sample:   repeated post-install sampling"))
    p.add_argument("--baseline",  default=None,
                   help="Path to baseline JSON (required for --mode post)")
    p.add_argument("--output",    required=True,
                   help="Output path. For 'post': base name without extension "
                        "(script appends .html and .docx)")
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE,
                   help=f"KAC Helm namespace (default: {DEFAULT_NAMESPACE})")
    p.add_argument("--release",   default=DEFAULT_RELEASE,
                   help=f"Helm release name (default: {DEFAULT_RELEASE})")
    p.add_argument("--samples",   type=int, default=6,
                   help="Number of samples for --mode sample (default: 6)")
    p.add_argument("--interval",  type=int, default=60,
                   help="Seconds between samples (default: 60)")
    p.add_argument("--chart-dir", default=None,
                   help="Directory for chart PNGs (default: same dir as --output)")
    p.add_argument("--wait",      type=int, default=120,
                   help="Seconds to wait for KAC rollout in post mode (default: 120)")
    return p.parse_args()


def main():
    args = parse_args()
    ns, rel = args.namespace, args.release

    if args.mode == "baseline":
        snap = collect_baseline_snapshot(ns, rel)
        Path(args.output).write_text(json.dumps(snap, indent=2))
        print(f"✅  Baseline saved to: {args.output}")

    elif args.mode == "sample":
        samples = collect_samples(args.samples, args.interval, ns, rel)
        Path(args.output).write_text(json.dumps(samples, indent=2))
        print(f"\n✅  {len(samples)} samples saved to: {args.output}")

    elif args.mode == "post":
        if not args.baseline:
            print("[ERROR] --baseline <path> is required for --mode post"); sys.exit(1)
        bpath = Path(args.baseline)
        if not bpath.exists():
            print(f"[ERROR] Baseline not found: {args.baseline}"); sys.exit(1)

        baseline = json.loads(bpath.read_text())
        post     = collect_post_snapshot(ns, rel)
        delta    = compute_delta(baseline, post)
        assess   = assess_risk(delta, post)

        base_out  = str(args.output).removesuffix(".html").removesuffix(".docx")
        chart_dir = args.chart_dir or str(Path(base_out).parent)
        charts    = generate_charts(post, delta, chart_dir)

        generate_html_report(baseline, post, delta, assess, charts, base_out + ".html")
        generate_docx_report(baseline, post, delta, assess, charts, base_out + ".docx")

        print("\n── Summary ────────────────────────────────────────────────")
        ci = post.get("cluster_info", {})
        hr = post.get("helm_release", {})
        print(f"  Cluster   : {ci.get('cluster_name')} ({ci.get('k8s_platform')})")
        print(f"  Release   : {hr.get('release')} "
              f"chart={hr.get('chart_version')} status={hr.get('status')}")
        print(f"  Risk      : {assess['risk_level']}")
        for f in assess["findings"]:
            print(f"  {f}")
        print(f"  HTML → {base_out}.html")
        print(f"  DOCX → {base_out}.docx")
        print("───────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()

