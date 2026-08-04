# Making manually uploaded OCI images in Software Depot manageable by vcf-download-tool

## Applicability
This document is a follow-up to the [VKS Deployment Guide for VCF 9.1.0 air-gapped environments](/airgapped/air-gapped-vcf91.md). It is only relevant if you followed that guide's manual upload path — steps 1c, 1d, 6a, and 6b, which use [`oci_image_depot_migrator.py`](scripts/oci_image_depot_migrator.py) to copy OCI images (Supervisor Services, VKS Standard Packages) directly into the Software Depot's OCI registry.

You do **not** need this document if:
* Your Software Depot has internet connectivity and only used `vcf-download-tool` itself to perform the download and upload of OCI images, or
* You followed the classic Enterprise-registry guides ([`air-gapped.md`](/airgapped/air-gapped.md), [`air-gapped-vcf90.md`](/airgapped/air-gapped-vcf90.md), [`air-gapped-harbor.md`](/airgapped/air-gapped-harbor.md)), which do not use the Software Depot's OCI registry at all.

**Goal:** make images that were manually uploaded via `oci_image_depot_migrator.py` visible to `vcf-download-tool`'s own bookkeeping, so VCF download tool treats them as known/managed artifacts.

**Two ways to get there:**
1. **(Recommended) Remediate** — run `vcf-download-tool` yourself to download and upload the same artifact through the official tool (see [Section 3](#3-remediate-make-an-image-manageable-via-vcf-download-tool)).
2. **(Workaround) Delete** — if the upload OCI images are no longer needed or used, delete the unmanaged image from the Software Depot registry instead (see [Section 4](#4-workaround-delete-an-unmanaged-image)).

## Background
The manual `imgpkg`-based upload path in `air-gapped-vcf91.md` is to make the images available in a disconnected or offline software depot: Supervisor Service and VKS Standard Package OCI images must already exist in the Software Depot's registry before those services can be installed from it, and the `vcf-download-tool` prior to VCF 9.1.1 doensn't support VCF components with OCI images.

`vcf-download-tool`'s own `depot artifacts download`/`upload` commands are the source of truth to manage what artifacts are present in the Software Depot. Images pushed by `oci_image_depot_migrator.py` land in the same OCI registry, but bypass that bookkeeping entirely — so they are invisible to `vcf-download-tool depot artifacts list` even though they are physically present in Software Depot and fully usable by the Supervisor.

## Terminology
* **Software Depot** &mdash; the software depot component of VCF Fleet used to host container images for Supervisor Services and VKS Standard Packages in air-gapped environments.
* **vcf-download-tool (VCFDT)** &mdash; the Broadcom-provided CLI used to download and upload VCF artifacts (including `depot artifacts`) between an internet-connected host and the Software Depot.
* **Managed / unmanaged image** &mdash; an image is *managed* if it appears in `vcf-download-tool depot artifacts list`'s output for its component; it is *unmanaged* if it is physically present in the Software Depot's OCI registry but does not appear there.
* **`--component` identifier** &mdash; the artifact identifier `vcf-download-tool` uses for a given Supervisor Service / package family (e.g. `SUPERVISOR_SERVICE_ARGOCD`, `VKS_STANDARD_PACKAGES`). See the mapping table in [Section 1a](#1a-component-to-repo-path-mapping-reference).

## Prerequisites
* `vcf-download-tool`, installed on a host with network access to both your VCF Operations (`--ops-fqdn`) endpoint and the Software Depot (`--depot-fqdn`). Download it from the Broadcom Support Portal under **My Downloads → VMware Cloud Foundation → VCF Download Tool**.
* `python3` (already required by `air-gapped-vcf91.md` for `oci_image_depot_migrator.py`).
* The Software Depot FQDN and VCF version (same values used in `air-gapped-vcf91.md` steps 6a/6b).
* VCF Operations credentials: `--ops-fqdn`, `--ops-user`, and a file containing the user's password (`--ops-user-password-file`).
* For the delete workaround only ([Section 4](#4-workaround-delete-an-unmanaged-image)): the VSP host and admin credentials already used with [`toggle_software_depot_oci_image_upload.sh`](scripts/toggle_software_depot_oci_image_upload.sh) in `air-gapped-vcf91.md` steps 5c/6c.

> [!IMPORTANT]
> The `depot artifacts` subcommand family used below is the OCI/Carvel-artifact analog of the publicly documented `depot binaries` family (used for management-appliance ISOs) and takes the same `--vcf-version`/`--depot-fqdn`/`--ops-fqdn`/`--ops-user`/`--ops-user-password-file` flags.

The companion script [`verify_depot_oci_management.py`](scripts/verify_depot_oci_management.py) automates the scan/diff/remediation-command steps below. It reuses `oci_image_depot_migrator.py`'s `require_cmd` helper and shells out to `toggle_software_depot_oci_image_upload.sh` for the delete workaround, so keep all three scripts together in `airgapped/scripts/`.

## 1. Check which Software Depot OCI images are managed
Run `check` from a host with `imgpkg`-style network access to the Software Depot's registry endpoint and to VCF Operations:

```bash
./verify_depot_oci_management.py check \
    --depot-fqdn <software-depot-fqdn> --vcf-version <vcf-version> \
    --ops-fqdn <vcf-operations-fqdn> --ops-user <ops-username> \
    --ops-user-password-file <path-to-password-file>

## Sample Command
./verify_depot_oci_management.py check \
    --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net --vcf-version 9.1.0 \
    --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
    --ops-user-password-file /root/.ops-pw
```

Sample output when every image in the depot is managed:

```
Software Depot: fleet-10-144-79-70.vcfd.broadcom.net
Scanned 3 image(s) across the OCI registry catalog.

Managed (3):
  [OK] vcf-service-argocd/ga/1.1.0/argocd-service:v1.1.0_vmware.1  (component: SUPERVISOR_SERVICE_ARGOCD)
  [OK] vks-standard-packages/ga/3.6.0-20260211/vks-standard-packages:3.6.0-20260211  (component: VKS_STANDARD_PACKAGES)
  [OK] supervisor-service-contour/ga/1.33.1/contour:v1.33.1_vmware.1  (component: SUPERVISOR_SERVICE_CONTOUR)

✅ All 3 image(s) in Software Depot are managed by vcf-download-tool.
```

Sample output when an image is unmanaged (with remediation commands printed automatically — see [Section 3](#3-remediate-make-an-image-manageable-via-vcf-download-tool)):

```
Software Depot: fleet-10-144-79-70.vcfd.broadcom.net
Scanned 3 image(s) across the OCI registry catalog.

Managed (2):
  [OK] vks-standard-packages/ga/3.6.0-20260211/vks-standard-packages:3.6.0-20260211  (component: VKS_STANDARD_PACKAGES)
  [OK] supervisor-service-contour/ga/1.33.1/contour:v1.33.1_vmware.1  (component: SUPERVISOR_SERVICE_CONTOUR)

Unmanaged (1) -- known component, not seen by vcf-download-tool:
  [!!] vcf-service-argocd/ga/1.1.0/argocd-service:v1.1.0_vmware.1  (component: SUPERVISOR_SERVICE_ARGOCD)

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

Action needed: see the unmanaged/unmapped sections above.
```

`check` exits `0` when everything is managed and `1` when action is needed, so it can be used as a gate in a script or pipeline.

### 1a. Component-to-repo-path mapping reference
`verify_depot_oci_management.py` matches each repo path discovered in the Software Depot's OCI registry catalog to a `vcf-download-tool` `--component` value using this table (kept in sync with the `REVERSE_MAPPINGS` table in [`oci_image_depot_migrator.py`](scripts/oci_image_depot_migrator.py)):

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

A repo path that matches none of these prefixes is reported as **unmapped** (see [Section 2](#2-interpreting-the-results)), not silently ignored — update this table (and `COMPONENT_REPO_PREFIXES` in the script) if Broadcom adds a new component you need to manage.

## 2. Interpreting the results
* **Managed** &mdash; the image's component was found in `vcf-download-tool depot artifacts list`'s output. No action needed.
* **Unmanaged** &mdash; the image's repo path matched a known component (Section 1a), but that component did not appear in `vcf-download-tool`'s managed list. This is the case this document exists to help with — see Sections 3 and 4.
* **Unmapped** &mdash; the image's repo path did not match *any* entry in the component table. This usually means either a foreign/unrelated image was pushed to the registry, or Broadcom has introduced a new component this document's table doesn't yet know about. Investigate before assuming it's safe to delete; do not treat an unmapped repo as a delete candidate.

## 3. Remediate: make an image manageable via vcf-download-tool
For each unmanaged image, `check` prints a ready-to-run, two-step command pair:

```bash
vcf-download-tool depot artifacts download --component=<COMPONENT> --vcf-version=<vcf-version> \
    --ops-fqdn=<vcf-operations-fqdn> --ops-user=<ops-username> --ops-user-password-file=<path-to-password-file>
vcf-download-tool depot artifacts upload --component=<COMPONENT> --vcf-version=<vcf-version> \
    --depot-fqdn=<software-depot-fqdn> \
    --ops-fqdn=<vcf-operations-fqdn> --ops-user=<ops-username> --ops-user-password-file=<path-to-password-file>
```

`--vcf-version` here is always the **VCF release identifier** (e.g. `9.1.0`), not a per-image version — `vcf-download-tool` has no separate per-artifact version flag for this command family. The depot tag `check` found for the unmanaged image is printed as an informational comment above the commands so you can confirm it's the version you expect before running `download`/`upload`.

After running both commands, re-run `check` — the component should now appear under **Managed**.

## 4. Workaround: delete an unmanaged image

> [!IMPORTANT]
> Deleting an image manifest is **destructive, production-impacting, and effectively irreversible**. Any Supervisor or VKS deployment that still pulls this image by tag will fail after deletion. Use this only if you cannot or do not want to run `vcf-download-tool` for the affected component — remediation (Section 3) is always the preferred path.

**Warning:** Deleting a manifest only unlinks it from the registry's tag list; the underlying image blobs are reclaimed only by a separate registry garbage-collection pass, which this script does not perform.

Deletion is gated behind the same [`toggle_software_depot_oci_image_upload.sh`](scripts/toggle_software_depot_oci_image_upload.sh) script used in `air-gapped-vcf91.md` steps 5c/6c: `delete` calls it with `enable` before deleting anything, and with `disable` afterward **no matter what** (success, failure, or interruption), so the depot's OCI registry is never left open longer than necessary.

```bash
./verify_depot_oci_management.py delete \
    --depot-fqdn <software-depot-fqdn> --vcf-version <vcf-version> \
    --ops-fqdn <vcf-operations-fqdn> --ops-user <ops-username> \
    --ops-user-password-file <path-to-password-file> \
    --vsp-host <vsp-host-fqdn> --admin-username <admin-username> --admin-password '<admin-password>' \
    [--repo <repo-path>] [--tag <tag>] [--dry-run]

## Sample Command (dry run first, strongly recommended)
./verify_depot_oci_management.py delete \
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

After the deletion completes, run `./verify_depot_oci_management.py check` again to see which unmanaged images (if any) still remain, and iterate — remediating each one via Section 3 or deleting it via this section — until `check` reports everything as managed or intentionally unmapped.

## Appendix: verify_depot_oci_management.py reference

```
$ ./verify_depot_oci_management.py --help
usage: verify_depot_oci_management.py [-h] --depot-fqdn FQDN --vcf-version VER
                                      --ops-fqdn FQDN --ops-user USER
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
  --depot-fqdn FQDN     Software Depot (Fleet Depot Server) FQDN.
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
  verify_depot_oci_management.py check \
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net --vcf-version 9.1.0 \
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
      --ops-user-password-file /root/.ops-pw

  verify_depot_oci_management.py delete \
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net --vcf-version 9.1.0 \
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
      --ops-user-password-file /root/.ops-pw \
      --vsp-host vsp.env1.lab.test --admin-username admin@vsp.local \
      --admin-password '...' --dry-run
```
