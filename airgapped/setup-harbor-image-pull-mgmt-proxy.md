# Configure a Management Proxy on Supervisor to Pull Harbor Images from Software Depot in Air-Gapped Environments

**Article ID:** 442742 _(placeholder — replace with assigned KB ID)_
**Updated On:** 2026-07-06
**Product:** VMware vSphere Kubernetes Service, VMware vSphere Foundation, VMware Cloud Foundation

---

## Issue

In a VVF deployment, or in a VCF deployment without VCF Automation, the Harbor Supervisor Service must be installed manually. When attempting to install Harbor on a Supervisor in an air-gapped environment, Harbor images fail to pull because the Supervisor cannot reach the Software Depot OCI registry directly. The Software Depot resides on the management network, which is not directly accessible from the Supervisor workload network.

Symptoms include:
- Harbor Supervisor Service installation stalls or fails to complete.
- Package reconciliation errors referencing an unreachable image repository.
- CoreDNS on the Supervisor control plane VMs cannot resolve the Software Depot registry hostname.

---

## Environment

- VMware vSphere Foundation (VVF) 9.1.1
- VMware Cloud Foundation (VCF) 9.1.1 without VCF Automation
- Air-gapped or internet-restricted deployments where the VCF Software Depot is configured in **disconnected or offline depot mode**, using the VCF Software Depot OCI registry

> **Note:** Do not use `manage-depot-image-proxy.sh` on a VCF 9.1.1 deployment where the Software Depot is configured in **online mode** (i.e., the Software Depot itself has direct internet connectivity). This management proxy together with the manual Harbor bootstrap workflow it supports, are required only for disconnected or offline depot mode.

---

## Cause

The VCF Software Depot OCI registry is reachable from the Supervisor control plane VMs only when a management proxy service is present. Without this proxy, the Supervisor cannot resolve or reach the Software Depot hostname to pull Harbor Supervisor Service images, because:

- Software Depot lives on the management network, separate from the Supervisor workload network.
- The default CoreDNS configuration on the Supervisor control plane VMs does not include a route to the Software Depot hostname.

A Kubernetes `Service` of type `ExternalName` (named `depot-image-proxy`) must be created in the `kube-system` namespace on each Supervisor control plane VM to proxy image pull requests to the Software Depot. This service is created and managed by the [`manage-depot-image-proxy.sh`](#attachment) script, which is attached to this article.

> **Note:** If the Supervisor is configured with an HTTP proxy for outbound traffic, add `depot-image-proxy.kube-system.svc.cluster.local` to the list of hosts excluded from the proxy (via the Supervisor proxy configuration UI or API). Otherwise, image pull requests to the management proxy will be incorrectly routed through the outbound proxy.

---

## Resolution

### Prerequisites

- The Admin host has `ssh`, `sshpass`, and `openssl` installed.
- The Software Depot endpoint is already configured on the vCenter and the Supervisor (visible under **Supervisor → Configure → Supervisor → Network → Management Porxy Configuration → `vcf-depot` management service**).
- The Harbor Supervisor Service OCI images have been uploaded to the Software Depot in disconnected or offline depot mode via VCF download tool (see ["Deploy in AirGapped Environments"](https://docworks.broadcom.net/docworks-review/cms?type=AEM&documentIds=GUID-ab1ea5ea-26f1-4572-abe6-ada6505ade6e-en&aemPubId=2584&reviewId=8903&topicToNavigate=GUID-9afcafd3-7d2c-46fd-9612-24fd9855c0d5-en.dita) _(docworks review link — to be replaced with the published techdocs URL once available)_).
- You have the vCenter FQDN, vCenter root SSH password, vCenter admin credentials, and the Supervisor ID.

### Step 1: Download the `manage-depot-image-proxy.sh` script

Download the `manage-depot-image-proxy.sh` script attached to this article and place it on the Admin host. Grant it execute permissions:

```bash
chmod +x manage-depot-image-proxy.sh
```

### Step 2: Find the Supervisor ID

The script requires the Supervisor ID. Retrieve it from the vCenter REST API or from the vSphere UI under **Workload Management → Supervisors**. Alternatively, retrieve it using the vCenter API:

```bash
## Query the vCenter REST API for the list of Supervisors (requires vCenter admin credentials)
SESSION_ID=$(curl -k -s -u 'administrator@vsphere.local:<password>' -X POST "https://vcenter.env1.lab.test/api/session" | tr -d '"')
curl -k -s -X GET -H "vmware-api-session-id: $SESSION_ID" \
    "https://vcenter.env1.lab.test/api/vcenter/namespace-management/supervisors/summaries" | jq -r '.items.[].supervisor'
```

### Step 3: Add the management proxy to the Supervisor

> [!IMPORTANT]
> This script SSHs into vCenter and runs `bash` non-interactively. By default, SSH sessions on the vCenter Server Appliance land in the restricted **appliancesh** shell, which cannot execute this and will fail with `Unknown command: 'bash'`. Before running the script, on the vCenter Server Appliance:
> 1. Enable **SSH Login** and activate **BASH Shell** access via the vCenter Server Management Interface (VAMI, `https://<vcenter>:5480` → **Access**).
> 2. Change root's login shell to bash so non-interactive SSH commands actually execute as bash: `chsh -s /bin/bash root`.
>
> **After the script finishes, revert both changes** to restore the appliance's default security posture:
> 1. Change root's login shell back to the restricted appliance shell: `chsh -s /bin/appliancesh root`.
> 2. Disable **BASH Shell** access via VAMI (and **SSH Login**, if it was not already enabled for other purposes).

Run the script with the `add` action. The script SSHs into the vCenter, then from vCenter into each Supervisor control plane VM, and deploys the `depot-image-proxy` service in the `kube-system` namespace.

```bash
./manage-depot-image-proxy.sh add <VC_HOST> <VC_ROOT_SSH_PASSWORD> <VC_ADMIN_USER> <VC_ADMIN_PASSWORD> <SUPERVISOR_ID>

## Sample command
./manage-depot-image-proxy.sh add vcenter.env1.lab.test 'MyRootPassword' administrator@vsphere.local 'MyAdminPassword' 284256be-074e-4750-8c9b-f57dfea4fb0a
```

Expected output (abbreviated):

```
VMware vCenter Server
Release: 9.1.1.0
Version: 9.1.1.0

Supervisor topology clusters: domain-c52
Matched cluster_id=domain-c52 floating_ip=10.161.112.94
Control Plane VM management IPs (3): 10.161.119.189 10.161.115.81 10.161.117.145
Generated CA and server cert in /tmp/depot-image-proxy.mA5QH9
Configuring control plane VM 10.161.119.189 ...
service/depot-image-proxy created
deployment.apps/coredns restarted
Done control plane VM 10.161.119.189
Configuring control plane VM 10.161.115.81 ...
service/depot-image-proxy unchanged
deployment.apps/coredns restarted
Done control plane VM 10.161.115.81
Configuring control plane VM 10.161.117.145 ...
service/depot-image-proxy unchanged
deployment.apps/coredns restarted
Done control plane VM 10.161.117.145
Registered depot-registry with supervisor 284256be-074e-4750-8c9b-f57dfea4fb0a (HTTP 201)
All steps finished (add).
```

### Step 4: Update the Harbor package YAML image reference

The Harbor Supervisor Service package YAML (`harbor-svs-v2.15.2-vmware.1-vks.1-25601986.yml`, downloaded in [step 1c of the air-gapped guide](air-gapped-vcf911.md)) contains an image reference that must be updated to route through the management proxy.

Locate the `imgpkgBundle.image` field in the YAML. The original value points directly to the Software Depot internal service:

```yaml
      fetch:
        - imgpkgBundle:
            image: "depot.kube-system.svc/vcf/supervisor-service-harbor/ga/2.15.2/harbor:v2.15.2_vmware.2-vks.1"
```

Replace it with the management-proxy hostname:

```yaml
      fetch:
        - imgpkgBundle:
            image: "depot-image-proxy.kube-system.svc.cluster.local/supervisor-service-harbor/ga/2.15.2/harbor:v2.15.2_vmware.2-vks.1"
```

You can use `sed` to apply this substitution in place:

```bash
sed -i 's|depot.kube-system.svc/vcf/supervisor-service-harbor|depot-image-proxy.kube-system.svc.cluster.local/supervisor-service-harbor|g' \
    harbor-svs-v2.15.2-vmware.1-vks.1-25601986.yml
```

### Step 5: Register and install the Harbor Supervisor Service

Register the updated Harbor package YAML on the vCenter, then follow the [Deploy Harbor Supervisor Service in VVF without VCFA](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-harbor-as-vcf-service/installing-and-configuring-harbor-and-contour/deploy-harbor-supervisor-service-in-vvf-without-vcfa.html) documentation to install Harbor on the Supervisor.

### Step 6 (Optional): Remove the management proxy

If the management proxy is no longer needed (for example, after migrating to a fully connected deployment), remove it using the same script with the `remove` action:

```bash
./manage-depot-image-proxy.sh remove <VC_HOST> <VC_ROOT_SSH_PASSWORD> <VC_ADMIN_USER> <VC_ADMIN_PASSWORD> <SUPERVISOR_ID>
```

---

## Script Reference

The script accepts the following positional arguments:

| Argument | Description |
|----------|-------------|
| `VC_HOST` | vCenter FQDN (must match the server TLS certificate; used for both SSH as root and REST API calls) |
| `VC_ROOT_SSH_PASSWORD` | vCenter root SSH password (used by `sshpass` for the workstation → vCenter hop) |
| `VC_ADMIN_USER` | vCenter administrator user (e.g. `administrator@vsphere.local`) |
| `VC_ADMIN_PASSWORD` | vCenter administrator password |
| `SUPERVISOR_ID` | Supervisor identifier (UUID) for the target Supervisor |

**Script requirements:** `ssh` and `sshpass` must be installed on the Admin host. `sshpass` must also be available on the vCenter appliance for the vCenter → Supervisor control plane VM SSH hops. Passwords may be visible in process listings; quote arguments containing shell metacharacters.

---

## Additional Information

- [VKS Deployment Guide for VCF 9.1.1 air-gapped environments](air-gapped-vcf911.md) — end-to-end air-gapped deployment procedure, including downloading and uploading Harbor images to Software Depot.
- [Deploy Harbor Supervisor Service in VVF without VCFA](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-harbor-as-vcf-service/installing-and-configuring-harbor-and-contour/deploy-harbor-supervisor-service-in-vvf-without-vcfa.html) — official documentation for manually installing the Harbor Supervisor Service.
- [Using Harbor as a VCF service](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-service-administration-and-development/9-1/using-harbor-as-vcf-service/using-harbor-as-a-vcf-service.html) — overview of Harbor as a VCF service, applicable to VCF deployments with VCF Automation.

---

## Attachments

- `manage-depot-image-proxy.sh` — shell script that deploys or removes the `depot-image-proxy` Kubernetes service on Supervisor control plane VMs, and registers the depot registry with the Supervisor.
