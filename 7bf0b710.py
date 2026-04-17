#!/usr/bin/env python3
# falcon_check.py

import argparse, subprocess, json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

FALCON_NS   = "falcon-system"
FLUX_NS     = "flux-system"
LATEST_VERSIONS = {
    "falcon-kac":    "7.10.0",
    "falcon-sensor": "7.10.0",
}

COMPONENT_RESOURCES = {
    "kac": {
        "helm": ["falcon-kac"], "deployment": "falcon-kac",
        "daemonset": None, "webhook": "falcon-kac-webhook",
        "helmrelease": ["falcon-kac"], "kustomize": ["falcon-kac"],
    },
    "edr": {
        "helm": ["falcon-sensor"], "deployment": None,
        "daemonset": "falcon-sensor", "webhook": None,
        "helmrelease": ["falcon-sensor"], "kustomize": ["falcon-sensor"],
    },
    "both": {
        "helm": ["falcon-kac", "falcon-sensor"],
        "deployment": "falcon-kac", "daemonset": "falcon-sensor",
        "webhook": "falcon-kac-webhook",
        "helmrelease": ["falcon-kac", "falcon-sensor"],
        "kustomize": ["falcon-kac", "falcon-sensor"],
    },
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def kubectl(args, cluster):
    out = subprocess.run(
        ["kubectl"] + args + ["--context", cluster],
        capture_output=True, text=True)
    return out.stdout, out.stderr

def kube_json(args, cluster):
    stdout, _ = kubectl(args + ["-o", "json"], cluster)
    try:
        return json.loads(stdout)
    except Exception:
        return {}

def age_from_timestamp(ts_str):
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        days, hours = delta.days, delta.seconds // 3600
        mins = (delta.seconds % 3600) // 60
        return f"{days}d{hours}h" if days > 0 else f"{hours}h{mins}m"
    except Exception:
        return "unknown"

def hours_since(ts_str):
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - ts).total_seconds() / 3600, 1)
    except Exception:
        return None

def condition_status(conditions, ctype):
    for c in conditions:
        if c.get("type") == ctype:
            return c.get("status", "Unknown"), c.get("message", "")
    return "Unknown", ""

# ── Cluster Discovery ─────────────────────────────────────────────────────────

def get_clusters(env=None, region=None, clusters=None):
    raw = json.loads(subprocess.check_output(
        ["az", "aks", "list", "--query",
         "[].{name:name,rg:resourceGroup,location:location,"
         "tags:tags,k8sVersion:kubernetesVersion}",
         "-o", "json"], text=True))
    result = []
    for c in raw:
        if clusters and c["name"] not in clusters:
            continue
        if region and c["location"] != region:
            continue
        if env:
            tag_env = (c.get("tags") or {}).get("environment", "").lower()
            if tag_env != env.lower():
                continue
        result.append(c)
    return result

def get_kubeconfig(cluster_name, rg):
    subprocess.run(
        ["az", "aks", "get-credentials", "--name", cluster_name,
         "--resource-group", rg, "--overwrite-existing",
         "--only-show-errors"],
        capture_output=True)

# ── Standard Checks ───────────────────────────────────────────────────────────

def check_namespace(cluster):
    out, _ = kubectl(["get", "ns", FALCON_NS, "--ignore-not-found"], cluster)
    return "✅ exists" if FALCON_NS in out else "❌ MISSING"

def check_helm(cluster, releases):
    results = {}
    for r in releases:
        out = subprocess.run(
            ["helm", "status", r, "-n", FALCON_NS,
             "--kube-context", cluster, "--output", "json"],
            capture_output=True, text=True)
        try:
            data     = json.loads(out.stdout)
            status   = data.get("info", {}).get("status", "unknown")
            chart    = data.get("chart", {}).get("metadata", {})
            last_dep = data.get("info", {}).get("last_deployed", "")
            hrs      = hours_since(last_dep)
            age_str  = f"{hrs}h ago" if hrs is not None else "?"
            stale    = hrs is not None and hrs > 24
            icon     = "✅" if status == "deployed" else "❌"
            staleflag = " ⚠️ STALE (>24h)" if stale else ""
            results[r] = {
                "status":        f"{icon} {status}",
                "chart_version": chart.get("version", "?"),
                "app_version":   chart.get("appVersion", "?"),
                "last_deployed": last_dep[:19] if last_dep else "?",
                "deploy_age":    age_str + staleflag,
                "stale":         stale,
            }
        except Exception:
            results[r] = {
                "status": "❌ not found", "chart_version": "?",
                "app_version": "?", "last_deployed": "?",
                "deploy_age": "?", "stale": False,
            }
    return results

def check_pods(cluster):
    data = kube_json(["get", "pods", "-n", FALCON_NS], cluster)
    pods = []
    for p in data.get("items", []):
        name       = p["metadata"]["name"]
        phase      = p["status"].get("phase", "Unknown")
        start_time = p["status"].get("startTime", "")
        cs_list    = p["status"].get("containerStatuses", [])
        ready      = all(cs.get("ready") for cs in cs_list)
        restarts   = sum(cs.get("restartCount", 0) for cs in cs_list)
        images     = [c["image"] for c in p["spec"].get("containers", [])]
        oom_count  = 0
        oom_containers = []
        for cs in cs_list:
            last = cs.get("lastState", {}).get("terminated", {})
            if last.get("reason") == "OOMKilled":
                oom_count += 1
                oom_containers.append(cs.get("name", "?"))
        resources = []
        for c in p["spec"].get("containers", []):
            res = c.get("resources", {})
            req = res.get("requests", {})
            lim = res.get("limits", {})
            resources.append({
                "container":   c["name"],
                "cpu_request": req.get("cpu", "none"),
                "mem_request": req.get("memory", "none"),
                "cpu_limit":   lim.get("cpu", "none"),
                "mem_limit":   lim.get("memory", "none"),
            })
        icon = "✅" if phase == "Running" and ready else "❌"
        pods.append({
            "name":           name,
            "status":         f"{icon} {phase}",
            "ready":          ready,
            "restarts":       restarts,
            "oom_killed":     oom_count,
            "oom_containers": oom_containers,
            "images":         images,
            "node":           p["spec"].get("nodeName", "unknown"),
            "age":            age_from_timestamp(start_time),
            "resources":      resources,
        })
    return pods

def check_daemonset(cluster, ds_name):
    if not ds_name:
        return None
    data = kube_json(["get", "daemonset", ds_name, "-n", FALCON_NS], cluster)
    if not data:
        return {"status": "❌ not found"}
    s       = data.get("status", {})
    desired = s.get("desiredNumberScheduled", 0)
    ready   = s.get("numberReady", 0)
    icon    = "✅" if desired == ready else "❌"
    return {
        "status":      f"{icon} {ready}/{desired} ready",
        "desired":     desired,
        "ready":       ready,
        "scheduled":   s.get("currentNumberScheduled", 0),
        "unavailable": s.get("numberUnavailable", 0),
    }

def check_deployment(cluster, dep_name):
    if not dep_name:
        return None
    data = kube_json(["get", "deployment", dep_name, "-n", FALCON_NS], cluster)
    if not data:
        return {"status": "❌ not found"}
    desired = data.get("spec", {}).get("replicas", 0)
    s       = data.get("status", {})
    ready   = s.get("readyReplicas", 0)
    icon    = "✅" if desired == ready else "❌"
    return {
        "status":    f"{icon} {ready}/{desired} ready",
        "desired":   desired,
        "ready":     ready,
        "available": s.get("availableReplicas", 0),
    }

def check_node_coverage(cluster, ds_name):
    if not ds_name:
        return None
    nodes_data = kube_json(["get", "nodes"], cluster)
    all_nodes  = {n["metadata"]["name"] for n in nodes_data.get("items", [])}
    pods_data  = kube_json(
        ["get", "pods", "-n", FALCON_NS, "-l", f"app={ds_name}"], cluster)
    covered    = {p["spec"].get("nodeName") for p in pods_data.get("items", [])}
    uncovered  = all_nodes - covered
    return {
        "total_nodes":     len(all_nodes),
        "covered":         len(covered),
        "uncovered":       len(uncovered),
        "uncovered_nodes": list(uncovered),
        "status": "✅ Full coverage" if not uncovered
                  else f"❌ {len(uncovered)} node(s) uncovered",
    }

def check_node_taints(cluster):
    data = kube_json(["get", "nodes"], cluster)
    taint_report = []
    for n in data.get("items", []):
        name     = n["metadata"]["name"]
        taints   = n.get("spec", {}).get("taints", [])
        blocking = [t for t in taints if t.get("effect") in ("NoSchedule","NoExecute")]
        if blocking:
            taint_report.append({
                "node":   name,
                "taints": [f"{t.get('key')}={t.get('value','')}: {t.get('effect')}"
                           for t in blocking]
            })
    if not taint_report:
        return {"status": "✅ No blocking taints", "nodes_with_taints": []}
    return {
        "status":            f"⚠️ {len(taint_report)} node(s) with blocking taints",
        "nodes_with_taints": taint_report,
    }

def check_webhook(cluster, webhook_name):
    if not webhook_name:
        return None
    out, _ = kubectl(
        ["get", "mutatingwebhookconfiguration",
         webhook_name, "--ignore-not-found"], cluster)
    return "✅ registered" if webhook_name in out else "❌ NOT registered"

def check_events(cluster):
    data  = kube_json(
        ["get", "events", "-n", FALCON_NS,
         "--field-selector", "type=Warning",
         "--sort-by=.lastTimestamp"], cluster)
    items = data.get("items", [])[-8:]
    if not items:
        return [{"type": "✅ None", "reason": "-", "message": "-", "count": 0}]
    return [{
        "type":    "⚠️ Warning",
        "reason":  e.get("reason", "?"),
        "message": e.get("message", "?")[:120],
        "count":   e.get("count", 1),
    } for e in items]

# ── Version Drift ─────────────────────────────────────────────────────────────

def check_version_drift(pods, helm_results):
    drift = {}
    running_images = list({
        img.split(":")[-1]
        for p in pods
        for img in p.get("images", [])
    })
    for release, expected in LATEST_VERSIONS.items():
        if any(expected not in img for img in running_images) and running_images:
            drift[f"sensor_drift_{release}"] = {
                "status":   "⚠️ Drift detected",
                "running":  running_images,
                "expected": expected,
            }
        else:
            drift[f"sensor_drift_{release}"] = {
                "status":   "✅ Up to date",
                "running":  running_images,
                "expected": expected,
            }
    for release, info in (helm_results or {}).items():
        deployed_chart = info.get("chart_version", "?")
        expected_chart = LATEST_VERSIONS.get(release, "?")
        if deployed_chart != expected_chart and expected_chart != "?":
            drift[f"chart_drift_{release}"] = {
                "status":   "⚠️ Chart drift",
                "deployed": deployed_chart,
                "expected": expected_chart,
            }
        else:
            drift[f"chart_drift_{release}"] = {
                "status":   "✅ Matches expected",
                "deployed": deployed_chart,
                "expected": expected_chart,
            }
    return drift

# ── FluxCD Checks ─────────────────────────────────────────────────────────────

def check_helmrelease(cluster, releases):
    results = {}
    for r in releases:
        data = kube_json(["get", "helmrelease", r, "-n", FLUX_NS], cluster)
        if not data:
            results[r] = {"status": "❌ not found"}
            continue
        conditions     = data.get("status", {}).get("conditions", [])
        ready_s, ready_msg = condition_status(conditions, "Ready")
        last_applied   = data.get("status", {}).get("lastAppliedRevision", "?")
        last_attempted = data.get("status", {}).get("lastAttemptedRevision", "?")
        reconcile_ts   = next(
            (c.get("lastTransitionTime","") for c in conditions if c.get("type")=="Ready"), "")
        hrs     = hours_since(reconcile_ts)
        age_str = f"{hrs}h ago" if hrs is not None else "?"
        suspended = data.get("spec", {}).get("suspend", False)
        icon = "✅" if ready_s == "True" and not suspended else "❌"
        results[r] = {
            "status":             f"{icon} {'SUSPENDED' if suspended else ready_s}",
            "last_applied_rev":   last_applied,
            "last_attempted_rev": last_attempted,
            "last_reconcile":     reconcile_ts[:19] if reconcile_ts else "?",
            "reconcile_age":      age_str,
            "suspended":          suspended,
            "message":            ready_msg[:120],
        }
    return results

def check_kustomization(cluster, names):
    results = {}
    for name in names:
        data = kube_json(["get", "kustomization", name, "-n", FLUX_NS], cluster)
        if not data:
            results[name] = {"status": "❌ not found"}
            continue
        conditions   = data.get("status", {}).get("conditions", [])
        ready_s, ready_msg = condition_status(conditions, "Ready")
        last_applied = data.get("status", {}).get("lastAppliedRevision", "?")
        reconcile_ts = next(
            (c.get("lastTransitionTime","") for c in conditions if c.get("type")=="Ready"), "")
        hrs     = hours_since(reconcile_ts)
        age_str = f"{hrs}h ago" if hrs is not None else "?"
        suspended = data.get("spec", {}).get("suspend", False)
        icon = "✅" if ready_s == "True" and not suspended else "❌"
        results[name] = {
            "status":           f"{icon} {'SUSPENDED' if suspended else ready_s}",
            "last_applied_rev": last_applied,
            "last_reconcile":   reconcile_ts[:19] if reconcile_ts else "?",
            "reconcile_age":    age_str,
            "suspended":        suspended,
            "message":          ready_msg[:120],
        }
    return results

def check_git_repo(cluster):
    data    = kube_json(["get", "gitrepository", "-n", FLUX_NS], cluster)
    results = []
    for item in data.get("items", []):
        name       = item["metadata"]["name"]
        conditions = item.get("status", {}).get("conditions", [])
        ready_s, ready_msg = condition_status(conditions, "Ready")
        last_fetch = item.get("status", {}).get("artifact", {}).get("lastUpdateTime", "")
        revision   = item.get("status", {}).get("artifact", {}).get("revision", "?")
        hrs        = hours_since(last_fetch)
        age_str    = f"{hrs}h ago" if hrs is not None else "?"
        icon       = "✅" if ready_s == "True" else "❌"
        results.append({
            "name":         name,
            "status":       f"{icon} {ready_s}",
            "revision":     revision,
            "last_fetched": last_fetch[:19] if last_fetch else "?",
            "fetch_age":    age_str,
            "message":      ready_msg[:120],
        })
    return results if results else [{"name": "none", "status": "❌ No GitRepositories found"}]

def check_helm_repo_source(cluster):
    data    = kube_json(["get", "helmrepository", "-n", FLUX_NS], cluster)
    results = []
    for item in data.get("items", []):
        name       = item["metadata"]["name"]
        conditions = item.get("status", {}).get("conditions", [])
        ready_s, ready_msg = condition_status(conditions, "Ready")
        last_fetch = item.get("status", {}).get("artifact", {}).get("lastUpdateTime", "")
        hrs        = hours_since(last_fetch)
        age_str    = f"{hrs}h ago" if hrs is not None else "?"
        icon       = "✅" if ready_s == "True" else "❌"
        results.append({
            "name":        name,
            "status":      f"{icon} {ready_s}",
            "last_synced": last_fetch[:19] if last_fetch else "?",
            "sync_age":    age_str,
            "message":     ready_msg[:120],
        })
    return results if results else [{"name": "none", "status": "❌ No HelmRepositories found"}]

# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_checks_for_cluster(cluster_info, component, checks):
    cluster = cluster_info["name"]
    rg      = cluster_info["rg"]
    res     = COMPONENT_RESOURCES.get(component, COMPONENT_RESOURCES["both"])
    get_kubeconfig(cluster, rg)

    report = {
        "cluster":     cluster,
        "location":    cluster_info["location"],
        "env":         (cluster_info.get("tags") or {}).get("environment", "unknown"),
        "k8s_version": cluster_info.get("k8sVersion", "?"),
        "checked_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    run_all_checks = "all" in checks

    if run_all_checks or "namespace"      in checks:
        report["namespace"]      = check_namespace(cluster)
    if run_all_checks or "helm"           in checks:
        report["helm"]           = check_helm(cluster, res["helm"])
    if run_all_checks or "pods"           in checks:
        report["pods"]           = check_pods(cluster)
    if run_all_checks or "daemonset"      in checks:
        report["daemonset"]      = check_daemonset(cluster, res["daemonset"])
    if run_all_checks or "deployment"     in checks:
        report["deployment"]     = check_deployment(cluster, res["deployment"])
    if run_all_checks or "node_coverage"  in checks:
        report["node_coverage"]  = check_node_coverage(cluster, res["daemonset"])
    if run_all_checks or "node_taints"    in checks:
        report["node_taints"]    = check_node_taints(cluster)
    if run_all_checks or "webhook"        in checks:
        report["webhook"]        = check_webhook(cluster, res["webhook"])
    if run_all_checks or "events"         in checks:
        report["events"]         = check_events(cluster)
    if run_all_checks or "version_drift"  in checks:
        helm_data = report.get("helm") or check_helm(cluster, res["helm"])
        pods_data = report.get("pods") or check_pods(cluster)
        report["version_drift"] = check_version_drift(pods_data, helm_data)
    if run_all_checks or "helmrelease"    in checks:
        report["helmrelease"]    = check_helmrelease(cluster, res["helmrelease"])
    if run_all_checks or "kustomization"  in checks:
        report["kustomization"]  = check_kustomization(cluster, res["kustomize"])
    if run_all_checks or "git_repo"       in checks:
        report["git_repo"]       = check_git_repo(cluster)
    if run_all_checks or "helm_repo"      in checks:
        report["helm_repo"]      = check_helm_repo_source(cluster)

    report["overall"] = (
        "❌ Issues Found" if "❌" in json.dumps(report) or "⚠️" in json.dumps(report)
        else "✅ Healthy"
    )
    return report

def run_all(component="both", checks=None, env=None, region=None, clusters=None):
    if checks is None:
        checks = ["all"]
    cluster_list = get_clusters(env=env, region=region, clusters=clusters)
    if not cluster_list:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(run_checks_for_cluster, c, component, checks): c
            for c in cluster_list
        }
        for f in as_completed(futures):
            results.append(f.result())
    return sorted(results, key=lambda x: (x["env"], x["cluster"]))

# ── CLI Entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Falcon Multi-Cluster Checker")
    parser.add_argument("--component", choices=["kac","edr","both"], default="both")
    parser.add_argument("--checks", nargs="+",
        choices=["all","namespace","helm","pods","daemonset","deployment",
                 "node_coverage","node_taints","webhook","events",
                 "version_drift","helmrelease","kustomization","git_repo","helm_repo"],
        default=["all"])
    parser.add_argument("--env")
    parser.add_argument("--region")
    parser.add_argument("--clusters", nargs="+")
    parser.add_argument("--export-csv")
    args = parser.parse_args()

    results = run_all(args.component, args.checks, args.env,
                      args.region, args.clusters)

    for r in results:
        print(f"\n{'='*55}")
        print(f"  {r['overall']}  {r['cluster']}  [{r['env']}]  "
              f"{r['location']}  k8s={r['k8s_version']}")
        print(f"  Checked: {r['checked_at']}")
        for k, v in r.items():
            if k in ("cluster","location","env","k8s_version","checked_at","overall"):
                continue
            print(f"\n  [{k.upper()}]")
            if isinstance(v, list):
                for item in v:
                    print(f"    {item if not isinstance(item,dict) else json.dumps(item)}")
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    print(f"    {kk}: {vv}")
            else:
                print(f"    {v}")

    if args.export_csv:
        import pandas as pd
        flat = [{
            "Cluster":     r["cluster"], "Env": r["env"],
            "Region":      r["location"], "K8s Version": r["k8s_version"],
            "Overall":     r.get("overall",""), "Namespace": r.get("namespace",""),
            "Webhook":     r.get("webhook",""), "Checked At": r["checked_at"],
        } for r in results]
        pd.DataFrame(flat).to_csv(args.export_csv, index=False)
        print(f"\n📄 CSV saved: {args.export_csv}")
