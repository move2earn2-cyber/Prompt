# CrowdStrike Falcon Deployment on AKS  
## Helm vs Falcon Operator – Technical Justification

---

## **1. Objective**

This document evaluates two deployment patterns for CrowdStrike Falcon on **Azure Kubernetes Service (AKS)**:

- Deploying `FalconNodeSensor` and `FalconAdmission` via **Helm charts** (`falcon-sensor` and `falcon-kac`).  
- Deploying them via the **Falcon Operator** (`FalconNodeSensor` and `FalconAdmission` CRs).  

The goal is to select the **recommended deployment model** for AKS‑only workloads, managed by **GitOps with FluxCD**, and to understand how the decision changes if we later add `FalconImageAnalyzer`.  

All assessments assume:
- AKS clusters governed by **GitOps** (FluxCD).  
- No need for `autoUpdate`, image mirroring, or frequent CrowdStrike CID/API credential changes.  
- Clear separation between `FalconNodeSensor` and `FalconAdmission` concerns.  
- AKS‑only scope (no OpenShift or multi‑platform requirements today).  

---

## **2. Deployment model overview**

### **2.1 Helm charts model (`falcon-sensor`, `falcon-kac`, and `falcon-image-analyzer`)**

CrowdStrike publishes **Helm 3 charts** for Falcon on Kubernetes:

- `falcon-sensor`: deploys the Falcon Linux sensor as a **DaemonSet** on nodes, with configurable image, tag, `node.backend`, and logging flags.  
- `falcon-kac`: deploys the Kubernetes Admission Controller (`KAC`) as a **Deployment + MutatingWebhookConfiguration`.  
- `falcon-image-analyzer`: deploys the Falcon Image Analyzer (`FalconImageAnalyzer`) as a **Deployment + validating/mutating webhook` for AKS, EKS, GKE, and other Kubernetes distributions.  

On AKS, these charts are consumed via:

- A `HelmRelease` CR for `falcon-sensor` in FluxCD.  
- A `HelmRelease` CR for `falcon-kac` in FluxCD.  
- Optionally, a `HelmRelease` CR for `falcon-image-analyzer` when added.  

FluxCD’s Helm controller:

- Watches `HelmChart` and `HelmRelease` sources.  
- Re‑renders manifests when chart or values change.  
- Re‑applies them to the cluster, correcting drift and managing upgrades and rollbacks.  

This is a **pure Helm + GitOps** model, where Helm is a templating engine and FluxCD is the reconciliation controller.

### **2.2 Falcon Operator model (`FalconNodeSensor`, `FalconAdmission`, `FalconImageAnalyzer`)**

The Falcon Operator is a **CRD‑based controller** for Kubernetes that reconciles:

- `FalconNodeSensor`  
- `FalconAdmission`  
- `FalconImageAnalyzer`  
- `FalconContainer`  

into running DaemonSets, Deployments, and webhook configurations.  

Deployment steps:

1. Install the **Falcon Operator** controller (via `falcon-operator.yaml` or a custom setup; CrowdStrike does not provide an official Helm chart for the Operator itself).  
2. Apply `FalconNodeSensor`, `FalconAdmission`, and optionally `FalconImageAnalyzer` CRs describing the desired configuration (CID, image, backend, etc.).  
3. The Operator reconciles these CRs into Kubernetes resources; optionally, it can auto‑update the sensor image or mirror images when those features are enabled.  

In a FluxCD‑managed setup, the **CR YAML** (and potentially the Operator manifest) is the source of truth, and FluxCD pushes it to clusters; the Operator enforces the state in the cluster.  

This is a **CRD‑driven lifecycle model**, where CrowdStrike owns much of the “how to manage Falcon” logic.

---

## **3. Advantages and characteristics of each model**

### **3.1 Helm charts**

#### **3.1.1 Operational simplicity**

- **No extra in‑cluster controller**  
  Helm is a **templating system**; once deployed, there is no persistent Falcon‑specific controller running in the cluster.  
  The only controllers are native Kubernetes (DaemonSet, Deployment, etc.) and FluxCD’s Helm controller.  

- **Clear, independent lifecycles**  
  - `falcon-sensor` controls the node‑sensor.  
  - `falcon-kac` controls the admission controller.  
  - `falcon-image-analyzer` controls the image‑scan component (if added).  
  - Each can be versioned, upgraded, and rolled back independently, and their states are visible through `HelmRelease` and native Kubernetes objects.  

- **Low abstraction depth**  
  - Everything maps directly to standard Kubernetes resources: DaemonSet, Deployment, Service, etc.  
  - Values are plain YAML, visible via `helm template` or `kubectl describe` on `HelmRelease`.  

CrowdStrike explicitly documents the `falcon-sensor` and `falcon-kac` charts for Kubernetes clusters, including AKS, and positions them as a **community‑driven automation layer** for Falcon agents, not a heavyweight operator‑style deployment.

---

#### **3.1.2 Fit with GitOps (FluxCD)**

FluxCD’s Helm controller is designed to manage `HelmRelease` CRs, polling the Helm repository and applying the correct manifests when:

- The Helm chart version changes.  
- The values in Git change.  

This provides **automatic reconciliation** and **drift correction** across clusters, aligning with your existing GitOps workflows.  

Because you already use FluxCD, Helm does not introduce a new reconciliation layer; it uses the one you already operate.

---

#### **3.1.3 More control and customization at the Kubernetes layer**

Helm charts are **template‑based**, so CrowdStrike and the community can expose almost any Kubernetes pod‑level field in `values.yaml`:

- `tolerations`, `nodeAffinity`, `podAffinity`, `nodeSelector`  
- `hostNetwork`, `dnsPolicy`, `priorityClass`, `initContainers`, `extraVolumes`, `extraLabels`, `extraAnnotations`  

For example, in `falcon-kac` and `falcon-image-analyzer` you can directly configure:

```yaml
admissionConfig:
  tolerations:
    - key: role
      operator: Equal
      value: security
      effect: NoSchedule
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
          - key: node-role.kubernetes.io/worker
            operator: Exists

imageAnalyzerConfig:
  tolerations:
    - key: role
      operator: Equal
      value: security
      effect: NoSchedule
```

This gives you **full Kubernetes‑native customization** for scheduling, networking, and security‑context tuning, without needing CrowdStrike to expose each field in a CRD first.  

---

#### **3.1.4 Ease of control and debugging**

- `helm template` shows the exact manifests generated for a given values file.  
- `helm status` / `HelmRelease` inspection shows what was last applied.  
- Rollback is a `git revert` followed by FluxCD reconciliation, or a `helm rollback` locally.  

This makes debugging straightforward: you can inspect the input (values, chart version) and output (manifests, pods) without extra tooling.  

---

### **3.2 Falcon Operator**

#### **3.2.1 Unified CRD‑driven model**

The Falcon Operator exposes:

- `FalconNodeSensor` for node‑sensor configuration.  
- `FalconAdmission` for admission controller configuration.  
- `FalconImageAnalyzer` for image‑scan configuration.  
- `FalconContainer` for sidecar sensors.  

These CRDs allow you to:

- **Template once, reuse many times**:  
  CR specs can be versioned in Git and applied consistently across clusters.  
- **Act at the CRD level**:  
  Security or platform teams can write controllers or scripts that watch `FalconNodeSensor`/`FalconAdmission`/`FalconImageAnalyzer` status instead of low‑level pods and deployments.  
- **Coordinate multiple components**:  
  A single `FalconDeployment`‑style pattern (in newer Falcon Operator versions) can orchestrate several Falcon components from one CR.  

This is especially valuable if you expect to run multiple Falcon components and manage many clusters, where a **single, vendor‑owned CRD‑based API** for Falcon simplifies lifecycle governance.

---

#### **3.2.2 Built‑in Falcon‑specific lifecycle logic**

The Operator encodes CrowdStrike’s understanding of:

- **Sensor rollout**:  
  Re‑deploying the DaemonSet when backend (`kernel` vs `bpf`) or other runtime flags change.  
- **Admission controller lifecycle**:  
  Re‑deploying the admission controller when webhook config, image, or related fields change.  
- **Optional auto‑updates**:  
  When enabled, the Operator can auto‑update the sensor image to newer versions, with configurable `autoUpdate` behavior (`normal` / `force` / `off`).  
- **Image mirroring**:  
  On platforms like AKS, the Operator can pull images from `registry.crowdstrike.com` and push them to your own registry (e.g., ACR), then update the DaemonSet/Deployment specs to point to your registry.  

These are **application‑specific automation features** that Helm alone does not provide; you would have to implement them in your CI/CD layer.

---

#### **3.2.3 Stronger multi‑cluster and multi‑platform story**

- The Operator is **certified and recommended for OpenShift**, and is the only supported path there for Falcon components.  
- If you later run **AKS + OpenShift**, you can keep the same CRD model (`FalconNodeSensor`, `FalconAdmission`, etc.) across both platforms, and only the Operator deployment differs.  

This is valuable for enterprise‑style security platforms that standardize on a single, CRD‑based control plane for Falcon across all Kubernetes environments.

---

#### **3.2.4 Operator limitation: lack of `tolerations` for `FalconAdmission` and `FalconImageAnalyzer`**

A significant limitation of the current Falcon Operator is that:

- `FalconAdmission` and `FalconImageAnalyzer` **do not allow you to configure Kubernetes `tolerations` via the CRD**.  
- CrowdStrike has acknowledged this limitation; there are open issues and PRs to add `tolerations` support for these components, but it is not yet available.  

This means:

- There is no built‑in way to run the Kubernetes Admission Controller or Image Analyzer only on **tainted node‑pools** (e.g., `role=security:NoSchedule`) through the Operator alone.  
- You must rely solely on `nodeSelector` or `priorityClass`‑style options, which are less flexible than full `tolerations`/`nodeAffinity`.  

By contrast, the Helm charts for `falcon-kac` and `falcon-image-analyzer` allow you to set `tolerations` and `nodeAffinity` directly in `values.yaml`, giving you full control over scheduling and node‑pool isolation without waiting for Operator releases.  

---

#### **3.2.5 Operator is not required to run `FalconImageAnalyzer` on AKS**

CrowdStrike also publishes a **dedicated Helm chart `falcon-image-analyzer`** that is tested and documented for AKS, EKS, and other Kubernetes distributions.  

- You can deploy `FalconImageAnalyzer` via Helm exactly like `falcon-sensor` and `falcon-kac`.  
- The Helm values document AKS‑specific settings (e.g., `azure.enabled`, `image.repo`, `image.tag`, `crowdstrikeConfig.clusterName`, exclusions).  

In other words, the **Operator is not required** to run `FalconImageAnalyzer` on AKS. It is only required if you want to manage that component via the `FalconImageAnalyzer` CR and benefit from CRD‑driven lifecycle features.

---

## **4. Does Helm provide more customization than the Operator?**

**Yes, Helm currently provides more low‑level Kubernetes‑layer customization** than the Falcon Operator does, while the Operator provides more Falcon‑specific lifecycle logic when you use it.

### **4.1 Where Helm is more customizable**

- Helm exposes a **wider set of Kubernetes fields** via `values.yaml`:
  - `tolerations`, `nodeAffinity`, `podAffinity` (for `falcon-kac` and `falcon-image-analyzer`).  
  - `hostNetwork`, `dnsPolicy`, `initContainers`, `extraVolumes`, and annotations for network policies, CSI, IAM, cloud‑provider hooks.  
- You can adjust these knobs **without waiting for CrowdStrike to update the Operator’s CRD schema**.  
- Shared values (e.g., `global.imageRegistry`, `falcon.cid`, `tolerations`) can be kept in one base file and reused by all Helm releases, giving you a Git‑driven “centralized template” feel similar to what the Operator offers.  

This makes Helm **more flexible and customizable** for AKS‑specific node‑pool strategies and security‑isolation patterns.

### **4.2 Where the Operator is more opinionated**

- The Falcon Operator exposes a **curated subset** of options through `Falcon*` CRs:
  - `backend`, `autoUpdate`, `falcon.cloud_region`, `admissionConfig`/`imageAnalyzerConfig` flags, and limited affinity‑style options.  
- It does **not yet expose `tolerations`** for `FalconAdmission` and `FalconImageAnalyzer`, which many teams use for node‑pool isolation.  

This makes the Operator more **opinionated and constrained** at the Kubernetes layer, but more **automated** at the Falcon‑lifecycle layer (if you actually use `autoUpdate`, mirroring, and CRD‑based policy).

---

## **5. Helm + FluxCD already “watch changes” effectively**

You are correct that **Helm + FluxCD already watch changes** and automate reconciliation; the Operator does not uniquely solve this problem for your current use case.

- FluxCD’s Helm controller:
  - Watches `HelmChart` and `HelmRelease` sources.  
  - Re‑reconciles releases when chart or values change.  
  - Detects and corrects drift, applying the correct manifests across clusters.  
- This replaces the need for an additional Falcon‑specific controller just to react to Git changes, as long as you do not need `autoUpdate`, mirroring, or advanced CRD‑driven automation.  

So while the Operator can **watch `FalconNodeSensor`/`FalconAdmission`/`FalconImageAnalyzer` CRs** and act on them, this is only valuable if you build a **security platform layer** that listens to those CRs, enforces policies at the CRD level, and auto‑remediates.  

If you do not build that layer, **Helm + FluxCD already provides the same “watch‑and‑reconcile” behavior**, just at the `HelmRelease`/`values.yaml` layer instead of the CRD layer.

---

## **6. When to reconsider the Falcon Operator**

You should **re‑evaluate** the Falcon Operator if:

- You add multiple Falcon components (`FalconImageAnalyzer`, `FalconContainer`) and want a **single CRD‑driven model** across clusters.  
- You enable `autoUpdate` and want CrowdStrike to own the sensor rollout logic.  
- You start mirroring images into your own registry (ACR, private registry) and benefit from Operator‑driven mirroring.  
- You run **both AKS and OpenShift** and want a unified CRD‑based control plane for Falcon.  
- You build a **security platform layer** that watches `FalconNodeSensor`/`FalconAdmission`/`FalconImageAnalyzer` CRs and acts on them.  
- CrowdStrike adds `tolerations` support for `FalconAdmission` and `FalconImageAnalyzer`, removing a current AKS‑node‑pool limitation.  

Until then, the Operator introduces **additional operational overhead and abstraction depth** without enough upside for AKS‑only deployments managed by FluxCD.

---

## **7. Recommendation for AKS‑only deployments**

Given:

- **Platform**: AKS‑only.  
- **Orchestration**: FluxCD‑based GitOps.  
- **Features not required**:  
  - `autoUpdate`,  
  - image mirroring,  
  - frequent Falcon CID/API credential changes.  
- **Current needs**: `FalconNodeSensor` and `FalconAdmission`.  
- **Possible future need**: `FalconImageAnalyzer`.  
- **AKS design goals**: node‑pool isolation, tolerations, affinity, and full Kubernetes‑level customization.  

We **recommend deploying Falcon via Helm charts**:

- `falcon-sensor` for `FalconNodeSensor`.  
- `falcon-kac` for `FalconAdmission`.  
- `falcon-image-analyzer` for `FalconImageAnalyzer` (if added).  

managed by **FluxCD** using `HelmRelease` CRs.  

Helm provides:
- Lower operational overhead.  
- Full Kubernetes‑level customization (including `tolerations` and node‑pool targeting).  
- Alignment with your existing GitOps model.  

The Falcon Operator is best reserved for when you:
- Run multiple Falcon components at scale.  
- Enable advanced lifecycle features (`autoUpdate`, mirroring).  
- Or standardize on a CRD‑driven platform across AKS + OpenShift.  
