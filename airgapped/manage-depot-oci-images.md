# OCI images uploaded manually to VCF Software Depot are not recognized by vcf-download-tool in VCF 9.1.0 air-gapped environments

## Issue
After deploying VKS in a VCF 9.1.0 air-gapped environment by following the [VKS Deployment Guide for VCF 9.1.0 air-gapped environments](/airgapped/air-gapped-vcf91.md), OCI images for Supervisor Services and VKS Standard Packages that were manually copied into the Software Depot's OCI registry using [`oci_image_depot_migrator.py`](scripts/oci_image_depot_migrator.py) (steps 1c, 1d, 6a, and 6b of that guide) do not appear in the output of:

```bash
vcf-download-tool depot artifacts list \
    --vcf-version=<vcf-version> --depot-fqdn=<software-depot-fqdn> \
    --ops-fqdn=<vcf-operations-fqdn> --ops-user=<ops-username> \
    --ops-user-password-file=<path-to-password-file>
```

The images are physically present in the Software Depot's OCI registry and are fully usable by the Supervisor — the affected Supervisor Services and VKS Standard Packages install and run correctly — but `vcf-download-tool` has no record of them. For example, running the companion script [`manage_depot_manual_oci_images.py`](scripts/manage_depot_manual_oci_images.py)'s `check` command against such a depot reports the image as **unmanaged**:

```
Software Depot: fleet-10-144-79-70.vcfd.broadcom.net
Scanned 3 image(s) across the OCI registry catalog.

Managed (2):
  [OK] vks-standard-packages/ga/3.6.0-20260211/vks-standard-packages:3.6.0-20260211  (component: VKS_STANDARD_PACKAGES)
  [OK] supervisor-service-contour/ga/1.33.1/contour:v1.33.1_vmware.1  (component: SUPERVISOR_SERVICE_CONTOUR)

Unmanaged (1) -- known component, not seen by vcf-download-tool:
  [!!] vcf-service-argocd/ga/1.1.0/argocd-service:v1.1.0_vmware.1  (component: SUPERVISOR_SERVICE_ARGOCD)

Action needed: see the unmanaged/unmapped sections above.
```

## Environment
* VMware Cloud Foundation (VCF) / VMware vSphere Foundation (VVF) 9.1.0, air-gapped deployment
* VCF Software Depot
* vcf-download-tool (VCFDT)

This issue does not apply to:
* VCF / VVF 9.1.1 and later, where `vcf-download-tool depot artifacts download`/`upload` natively supports OCI image components (Supervisor Services, VKS Standard Packages); see the [VKS Deployment Guide for VCF 9.1.1+ air-gapped environments](/airgapped/air-gapped-vcf911.md) _(to be replaced by techdoc link)_.
* Deployments using an external Enterprise OCI registry instead of the Software Depot ([`air-gapped.md`](/airgapped/air-gapped.md), [`air-gapped-vcf90.md`](/airgapped/air-gapped-vcf90.md), [`air-gapped-harbor.md`](/airgapped/air-gapped-harbor.md)), which don't involve `vcf-download-tool` for OCI images at all.
* Software Depots that have direct internet access and used `vcf-download-tool` itself (rather than `oci_image_depot_migrator.py`) to download and upload OCI images.

## Cause
`vcf-download-tool` prior to VCF 9.1.1 does not support downloading and uploading OCI-image components (Supervisor Services, VKS Standard Packages) to the Software Depot. To make these images available in a disconnected or offline Software Depot on VCF 9.1.0, the [VKS Deployment Guide for VCF 9.1.0 air-gapped environments](/airgapped/air-gapped-vcf91.md) instead uses the `imgpkg`-based [`oci_image_depot_migrator.py`](scripts/oci_image_depot_migrator.py) script to copy the images directly into the Software Depot's OCI registry.

`vcf-download-tool`'s own `depot artifacts download`/`upload` commands are the source of truth it uses to know which artifacts are present in the Software Depot. Images pushed by `oci_image_depot_migrator.py` land in the same OCI registry but bypass that bookkeeping entirely, so they remain invisible to `vcf-download-tool depot artifacts list` even though they are physically present and fully usable by the Supervisor. An image in this state is referred to below as **unmanaged**; an image that `vcf-download-tool depot artifacts list` does report is **managed**.

## Resolution
There are two ways to resolve this. **Making the image manageable (Option 1) is the recommended fix**; deleting the image (Option 2) is a workaround for cases where the image is no longer needed or you cannot run `vcf-download-tool` for that component.

**Prerequisites for both options:**
* `vcf-download-tool`, installed on a host with network access to both your VCF Operations (`--ops-fqdn`) endpoint and the Software Depot (`--depot-fqdn`). Download it from the Broadcom Support Portal under **My Downloads → VMware Cloud Foundation → VCF Download Tool**.
* `python3` (already required by `air-gapped-vcf91.md` for `oci_image_depot_migrator.py`).
* The Software Depot FQDN and VCF version (same values used in `air-gapped-vcf91.md` steps 6a/6b).
* VCF Operations credentials: `--ops-fqdn`, `--ops-user`, and a file containing the user's password (`--ops-user-password-file`).
* The companion script [`manage_depot_manual_oci_images.py`](scripts/manage_depot_manual_oci_images.py), which automates the scan/diff/remediation-command steps below. It reuses `oci_image_depot_migrator.py`'s `require_cmd` helper and shells out to `toggle_software_depot_oci_image_upload.sh` for the delete workaround, so keep all three scripts together in `airgapped/scripts/`.

> [!IMPORTANT]
> The `depot artifacts` subcommand family used below is the OCI/Carvel-artifact analog of the publicly documented `depot binaries` family (used for management-appliance ISOs) and takes the same `--vcf-version`/`--depot-fqdn`/`--ops-fqdn`/`--ops-user`/`--ops-user-password-file` flags.

Before applying either option, run `check` from a host with `imgpkg`-style network access to the Software Depot's registry endpoint and to VCF Operations to identify every unmanaged image:

```bash
./manage_depot_manual_oci_images.py check \
    --depot-fqdn <software-depot-fqdn> --vcf-version <vcf-version> \
    --ops-fqdn <vcf-operations-fqdn> --ops-user <ops-username> \
    --ops-user-password-file <path-to-password-file>

## Sample Command
./manage_depot_manual_oci_images.py check \
    --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net --vcf-version 9.1.0 \
    --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
    --ops-user-password-file /root/.ops-pw
```

`check` matches each repo path discovered in the Software Depot's OCI registry catalog to a `vcf-download-tool` `--component` value using the table below (kept in sync with the `REVERSE_MAPPINGS` table in [`oci_image_depot_migrator.py`](scripts/oci_image_depot_migrator.py)). A repo path that matches none of these prefixes is reported as **unmapped**, not silently ignored — this usually means either a foreign/unrelated image was pushed to the registry, or Broadcom has introduced a new component this table doesn't yet know about; investigate before assuming it's safe to delete, and update this table (and `COMPONENT_REPO_PREFIXES` in the script) if needed.

|`--component`|Software Depot repo-path prefix|
|---|---|
|SUPERVISOR_SERVICE_ARGOCD|/vcf-service-argocd/ga|
|SUPERVISOR_SERVICE_HARBOR|/supervisor-service-harbor/ga|
|SUPERVISOR_SERVICE_LCI|/supervisor-service-lci/ga|
|VCF_CONSUMPTION_CLI_PLUGINS|/vcf-cli-plugins/ga|
|VCF_SERVICE_CONFIGURATION|/vcf-service-configuration/ga|
|SUPERVISOR_SERVICE_CONTOUR|/supervisor-service-contour/ga|
|VCF_SERVICE_SECRET_STORE|/vcf-service-secret-store/ga|
|VKS_STANDARD_PACKAGES|/vks-standard-packages/ga|
|SUPERVISOR_SERVICE_METRICS_AGGREGATOR|/supervisor-service-metrics-aggregator/ga|
|SUPERVISOR_SERVICE_EXTDNS|/supervisor-service-extdns/ga|
|SUPERVISOR_SERVICE_VKS|/supervisor-service-vks/ga|
|SUPERVISOR_SERVICE_SUPERVISOR_MANAGEMENT_PROXY|/supervisor-service-supervisor-management-proxy/ga|
|SUPERVISOR_SERVICE_CA_CLUSTERISSUER|/supervisor-service-ca-clusterissuer/ga|
|VKSM_EXTENSIONS|/vksm-extensions/ga|
|VCF_SERVICE_PROTECTION_AND_RECOVERY|/vcf-service-protection-and-recovery/ga|

`check` exits `0` when everything is managed and `1` when action is needed, so it can be used as a gate in a script or pipeline.

### Option 1 (Recommended): Make the image manageable by vcf-download-tool
For each unmanaged image, `check` automatically prints a ready-to-run, two-step remediation command pair:

```
Remediation commands (run on a host with vcf-download-tool and network access to VCF Operations):

# Unmanaged image: fleet-10-144-79-70.vcfd.broadcom.net/vcf-service-argocd/ga/1.1.0/argocd-service:v1.1.0_vmware.1
# (matched component: SUPERVISOR_SERVICE_ARGOCD; depot tag found: v1.1.0_vmware.1)
# NOTE: vcf-download-tool's depot-artifacts commands take --vcf-version as the
# VCF release identifier, not a per-image version; the tag above is shown so
# you can visually confirm it matches what --vcf-version=9.1.0 will fetch.
vcf-download-tool depot artifacts download --component=SUPERVISOR_SERVICE_ARGOCD --vcf-version=9.1.0 \
    --ops-fqdn=ops.env1.lab.test --ops-user=admin@vsp.local --ops-user-password-file=/root/.ops-pw
vcf-download-tool depot artifacts upload --component=SUPERVISOR_SERVICE_ARGOCD --vcf-version=9.1.0 \
    --depot-fqdn=fleet-10-144-79-70.vcfd.broadcom.net \
    --ops-fqdn=ops.env1.lab.test --ops-user=admin@vsp.local --ops-user-password-file=/root/.ops-pw
```

`--vcf-version` here is always the **VCF release identifier** (e.g. `9.1.0`), not a per-image version — `vcf-download-tool` has no separate per-artifact version flag for this command family. The depot tag `check` found for the unmanaged image is printed as an informational comment above the commands so you can confirm it's the version you expect before running `download`/`upload`.

After running both commands, re-run `check`. When every image is managed, it prints:

```
✅ All 3 image(s) in Software Depot are managed by vcf-download-tool.
```

### Option 2 (Workaround): Delete the unmanaged image

> [!IMPORTANT]
> Deleting an image manifest is **destructive, production-impacting, and effectively irreversible**. Any Supervisor or VKS deployment that still pulls this image by tag will fail after deletion. Use this only if you cannot or do not want to run `vcf-download-tool` for the affected component — Option 1 is always the preferred path.

**Warning:** Deleting a manifest only unlinks it from the registry's tag list; the underlying image blobs are reclaimed only by a separate registry garbage-collection pass, which this script does not perform.

Deletion is gated behind the same [`toggle_software_depot_oci_image_upload.sh`](scripts/toggle_software_depot_oci_image_upload.sh) script used in `air-gapped-vcf91.md` steps 5c/6c: `delete` calls it with `enable` before deleting anything, and with `disable` afterward **no matter what** (success, failure, or interruption), so the depot's OCI registry is never left open longer than necessary. This requires the VSP host and admin credentials already used with that script.

```bash
./manage_depot_manual_oci_images.py delete \
    --depot-fqdn <software-depot-fqdn> --vcf-version <vcf-version> \
    --ops-fqdn <vcf-operations-fqdn> --ops-user <ops-username> \
    --ops-user-password-file <path-to-password-file> \
    --vsp-host <vsp-host-fqdn> --admin-username <admin-username> --admin-password '<admin-password>' \
    [--repo <repo-path>] [--tag <tag>] [--dry-run]

## Sample Command (dry run first, strongly recommended)
./manage_depot_manual_oci_images.py delete \
    --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net --vcf-version 9.1.0 \
    --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
    --ops-user-password-file /root/.ops-pw \
    --vsp-host vcf-stls-wcp-pod13-136.lvn.broadcom.net --admin-username admin@vsp.local --admin-password 'Test!23Test!23' \
    --repo vcf-service-argocd/ga/1.1.0/argocd-service --dry-run

## Sample output
1 unmanaged image(s) selected for deletion:
  - vcf-service-argocd/ga/1.1.0/argocd-service:v1.1.0_vmware.1  (component: SUPERVISOR_SERVICE_ARGOCD)

--dry-run: no confirmation prompt, no toggle call, and no DELETE requests were made.
```

Once you've confirmed the plan with `--dry-run`, re-run without it. You must type the literal word `DELETE` to proceed:

```
*** WARNING: DESTRUCTIVE, PRODUCTION-IMPACTING, IRREVERSIBLE OPERATION ***
This will permanently delete the following image manifest(s) from the
Software Depot OCI registry. ...

  - vcf-service-argocd/ga/1.1.0/argocd-service : v1.1.0_vmware.1

Type DELETE (all caps) to proceed, anything else aborts: DELETE
+ toggle_software_depot_oci_image_upload.sh enable --vsp-host ... --admin-username ... --admin-password ****
...
Deleted vcf-service-argocd/ga/1.1.0/argocd-service:v1.1.0_vmware.1 (digest sha256:...).
+ toggle_software_depot_oci_image_upload.sh disable --vsp-host ... --admin-username ... --admin-password ****
...

Summary: 1 succeeded, 0 failed.
```

Anything other than the exact word `DELETE` (including pressing Enter with no input) aborts before the registry is ever toggled open. For scripted use, `--yes-i-am-sure DELETE` skips the interactive prompt but still requires that exact value.

After the deletion completes, run `./manage_depot_manual_oci_images.py check` again to see which unmanaged images (if any) still remain, and iterate — remediating each one via Option 1 or deleting it via this option — until `check` reports everything as managed or intentionally unmapped.

## Additional Information
* [VKS Deployment Guide for VCF 9.1.0 air-gapped environments](/airgapped/air-gapped-vcf91.md) — the guide whose manual upload path causes this issue.
* [VKS Deployment Guide for VCF 9.1.1+ air-gapped environments](/airgapped/air-gapped-vcf911.md) _(to be replaced by techdoc link)_ — the newer guide, unaffected by this issue since `vcf-download-tool` handles OCI images natively from 9.1.1 onward.
* [`oci_image_depot_migrator.py`](scripts/oci_image_depot_migrator.py), [`toggle_software_depot_oci_image_upload.sh`](scripts/toggle_software_depot_oci_image_upload.sh), and [`manage_depot_manual_oci_images.py`](scripts/manage_depot_manual_oci_images.py) — the scripts referenced throughout this article; keep all three together under `airgapped/scripts/`.

<details>
<summary><code>manage_depot_manual_oci_images.py --help</code> reference</summary>

```
$ ./manage_depot_manual_oci_images.py --help
usage: manage_depot_manual_oci_images.py [-h] --depot-fqdn FQDN --vcf-version
                                         VER --ops-fqdn FQDN --ops-user USER
                                         --ops-user-password-file FILE
                                         [--vcf-download-tool PATH]
                                         [--component COMPONENT] [--json]
                                         [--vsp-host HOST]
                                         [--admin-username USER]
                                         [--admin-password PASS]
                                         [--toggle-script PATH] [--repo REPO]
                                         [--tag TAG] [--dry-run]
                                         [--yes-i-am-sure WORD]
                                         ACTION

Detect Software Depot OCI images uploaded via oci_image_depot_migrator.py that
are not managed by vcf-download-tool, print remediation commands, and optionally
delete unmanaged images as a last-resort workaround.

Action is a required positional argument:
  check | delete

positional arguments:
  ACTION                check | delete (see examples below).

options:
  -h, --help            show this help message and exit
  --depot-fqdn FQDN     Software Depot FQDN.
  --vcf-version VER     VCF release identifier, e.g. 9.1.0. Passed through to
                        vcf-download-tool.
  --ops-fqdn FQDN       VCF Operations FQDN. Passed through to vcf-download-
                        tool.
  --ops-user USER       VCF Operations username. Passed through to vcf-
                        download-tool.
  --ops-user-password-file FILE
                        Path to a file containing the VCF Operations user's
                        password. Passed through to vcf-download-tool; never
                        read or printed by this script.
  --vcf-download-tool PATH
                        vcf-download-tool binary name or path. Default: vcf-
                        download-tool (resolved via PATH).
  --component COMPONENT
                        Restrict 'check' to one component (repeatable).
                        Default: scan all known components.
  --json                'check' only: emit a machine-readable JSON report
                        instead of text.
  --vsp-host HOST       'delete' only: VSP host, forwarded to
                        toggle_software_depot_oci_image_upload.sh.
  --admin-username USER
                        'delete' only: VSP admin username, forwarded to
                        toggle_software_depot_oci_image_upload.sh.
  --admin-password PASS
                        'delete' only: VSP admin password, forwarded to
                        toggle_software_depot_oci_image_upload.sh.
  --toggle-script PATH  Path to toggle_software_depot_oci_image_upload.sh.
                        Default: the copy next to this script.
  --repo REPO           'delete' only: restrict deletion to this repo path as
                        printed by 'check' (repeatable). Default: all
                        unmanaged images.
  --tag TAG             'delete' only: restrict to this tag (combine with
                        --repo to target one image).
  --dry-run             'delete' only: print the deletion plan and exit; no
                        prompt, no toggle, no DELETE calls.
  --yes-i-am-sure WORD  'delete' only: non-interactive bypass for the
                        confirmation prompt. Must be exactly 'DELETE'. Use
                        with extreme caution.

Actions:
  check   Scan the Software Depot OCI registry catalog and cross-reference it
          against `vcf-download-tool depot artifacts list`. Reports managed,
          unmanaged (known component, not seen by vcf-download-tool), and
          unmapped (no known component) images, and prints remediation
          commands for unmanaged images. Exit code 0 if everything is
          managed, 1 if action is needed.

  delete  Delete unmanaged image manifest(s) from the Software Depot OCI
          registry. Re-runs `check` internally (never trusts a stale list),
          requires typing DELETE to confirm (or --yes-i-am-sure DELETE), and
          wraps the deletion in an automatic enable/disable of OCI writes via
          toggle_software_depot_oci_image_upload.sh (disable always runs,
          even on failure or Ctrl-C).

Examples:
  manage_depot_manual_oci_images.py check \
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net --vcf-version 9.1.0 \
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
      --ops-user-password-file /root/.ops-pw

  manage_depot_manual_oci_images.py delete \
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net --vcf-version 9.1.0 \
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
      --ops-user-password-file /root/.ops-pw \
      --vsp-host vsp.env1.lab.test --admin-username admin@vsp.local \
      --admin-password '...' --dry-run
```

</details>
