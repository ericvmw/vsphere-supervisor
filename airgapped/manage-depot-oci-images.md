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

Unmanaged (1) -- known component, not seen by vcf-download-tool for --vcf-version=9.1 (may be available under a different VCF version):
  [!!] vcf-service-argocd/ga/1.1.0/argocd-service:v1.1.0_vmware.1  (component: SUPERVISOR_SERVICE_ARGOCD)

Action needed: see the unmanaged/unmapped sections above.
```

## Environment
* VMware Cloud Foundation (VCF) / VMware vSphere Foundation (VVF) 9.1.0, air-gapped deployment
* VCF Software Depot
* vcf-download-tool (VCFDT)

This issue does not apply to:
* VCF / VVF 9.1.1 and later, where `vcf-download-tool`'s `artifacts download` / `depot artifacts upload` commands natively support OCI image components (Supervisor Services, VKS Standard Packages); see the [VKS Deployment Guide for VCF 9.1.1+ air-gapped environments](/airgapped/air-gapped-vcf911.md) _(to be replaced by techdoc link)_.
* Deployments using an external Enterprise OCI registry instead of the Software Depot ([`air-gapped.md`](/airgapped/air-gapped.md), [`air-gapped-vcf90.md`](/airgapped/air-gapped-vcf90.md), [`air-gapped-harbor.md`](/airgapped/air-gapped-harbor.md)), which don't involve `vcf-download-tool` for OCI images at all.
* Software Depots that have direct internet access and used `vcf-download-tool` itself (rather than `oci_image_depot_migrator.py`) to download and upload OCI images.

## Cause
`vcf-download-tool` prior to VCF 9.1.1 does not support downloading and uploading OCI-image components (Supervisor Services, VKS Standard Packages) to the Software Depot. To make these images available in a disconnected or offline Software Depot on VCF 9.1.0, the [VKS Deployment Guide for VCF 9.1.0 air-gapped environments](/airgapped/air-gapped-vcf91.md) instead uses the `imgpkg`-based [`oci_image_depot_migrator.py`](scripts/oci_image_depot_migrator.py) script to copy the images directly into the Software Depot's OCI registry.

`vcf-download-tool`'s own `artifacts download` (to a local depot-store) and `depot artifacts upload` (from the depot-store to Software Depot) commands are the source of truth it uses to know which artifacts are present in the Software Depot. Images pushed by `oci_image_depot_migrator.py` land in the same OCI registry but bypass that bookkeeping entirely, so they remain invisible to `vcf-download-tool depot artifacts list` even though they are physically present and fully usable by the Supervisor. An image in this state is referred to below as **unmanaged**; an image that `vcf-download-tool depot artifacts list` does report is **managed**.

## Resolution
There are two ways to resolve this. **Making the image manageable (Option 1) is the recommended fix**; deleting the image (Option 2) is a workaround for cases where the image is no longer needed or you cannot run `vcf-download-tool` for that component.

**Prerequisites for both options:**
* `vcf-download-tool`, installed on a host with network access to both your VCF Operations (`--ops-fqdn`) endpoint and the Software Depot (`--depot-fqdn`). Download it from the Broadcom Support Portal under **My Downloads → VMware Cloud Foundation → VCF Download Tool**.
* `python3` (already required by `air-gapped-vcf91.md` for `oci_image_depot_migrator.py`).
* The Software Depot FQDN (same value used in `air-gapped-vcf91.md` steps 6a/6b). VCF version (`--vcf-version`) is optional — see the note below.
* VCF Operations credentials: `--ops-fqdn`, `--ops-user`, and a file containing the user's password (`--ops-user-password-file`).
* The companion script [`manage_depot_manual_oci_images.py`](scripts/manage_depot_manual_oci_images.py), which automates the scan/diff/remediation-command steps below. It reuses `oci_image_depot_migrator.py`'s `require_cmd` helper and shells out to `toggle_software_depot_oci_image_upload.sh` for the delete workaround, so keep all three scripts together in `airgapped/scripts/`.

> [!IMPORTANT]
> `vcf-download-tool depot artifacts list` and `depot artifacts upload` are the OCI/Carvel-artifact analog of the publicly documented `depot binaries` family (used for management-appliance ISOs) and take the same `--vcf-version`/`--depot-fqdn`/`--ops-fqdn`/`--ops-user`/`--ops-user-password-file` flags. The plain `artifacts download` command (no `depot` prefix — it downloads to a local directory, not to Software Depot) instead takes `--depot-store` and `--depot-download-activation-code-file`.

> [!IMPORTANT]
> The first time `vcf-download-tool` talks to a given `--depot-fqdn`/`--ops-fqdn` pair, it interactively prompts to confirm the TLS certificate chain and to opt in/out of CEIP before it will proceed. `manage_depot_manual_oci_images.py` runs `vcf-download-tool depot artifacts list` with its stdin inherited from your terminal specifically so you can answer these prompts if they appear; run `check` interactively (not from a non-interactive script, cron job, or with stdin redirected from `/dev/null`) at least once per depot/ops-fqdn pair so you're present to answer them. Once accepted, `vcf-download-tool` does not prompt again for that same depot/ops-fqdn pair.

> [!IMPORTANT]
> `--vcf-version`'s default depends on the action:
> * For `check`, it defaults to `9.1` (the minor version, not a specific patch like `9.1.0` or `9.1.1`), so `vcf-download-tool depot artifacts list` reports every image released under 9.1.x in one pass. Passing a specific patch version narrows what `vcf-download-tool` reports to that patch alone — an image that's genuinely managed but was released under a *different* 9.1.x patch than the one you passed would then be misreported as unmanaged.
> * For `delete`, it defaults to the specific patch `9.1.0` instead, since the untracked images this article covers were all manually uploaded under VCF 9.1.0 — a narrower, exact-patch check here keeps `delete`'s internal re-scan scoped to that release rather than pulling in another 9.1.x patch's managed-version data.
>
> Either default can be overridden with an explicit value if needed.
>
> The remediation commands `check` prints for each unmanaged image use a separate flag, `--remediation-vcf-version`, defaulting to the specific patch `9.1.0` — unlike `--vcf-version`, this one **must** be an exact patch, not the `9.1` wildcard, since `vcf-download-tool artifacts download`/`depot artifacts upload` need a concrete release to fetch. `9.1.0` is correct by default because the manually-uploaded images this article covers were all built for that release; override `--remediation-vcf-version` only if you know a given image was actually released under a different 9.1.x patch.

Before applying either option, run `check` from a host with `imgpkg`-style network access to the Software Depot's registry endpoint and to VCF Operations to identify every unmanaged image:

```bash
## --vcf-version defaults to 9.1 (covers all 9.1.x patch releases); pass it only to narrow to one patch.
./manage_depot_manual_oci_images.py check \
    --depot-fqdn <software-depot-fqdn> \
    --ops-fqdn <vcf-operations-fqdn> --ops-user <ops-username> \
    --ops-user-password-file <path-to-password-file>

## Sample Command
./manage_depot_manual_oci_images.py check \
    --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net \
    --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
    --ops-user-password-file /root/.ops-pw
```

### Sample output (first run against a depot/ops-fqdn pair, showing the TLS-certificate prompts)
`manage_depot_manual_oci_images.py` streams `vcf-download-tool`'s output live and passes your terminal's stdin straight through to it, so when the prompts described above appear, answer them exactly as you would running `vcf-download-tool` directly (the `Y` after each prompt below is the answer *you* type, not something the tool printed). This run passes `--vcf-version 9.1.1` explicitly to narrow the check to that one patch; omit it (as in the Sample Command above) to check against every 9.1.x release at once using the default:

```
$ ./manage_depot_manual_oci_images.py check \
    --depot-fqdn fleet-10-161-10-187.vcfd.broadcom.net --vcf-version 9.1.1 \
    --ops-fqdn 10.161.11.108 --ops-user admin --ops-user-password-file /home/worker/tmp/.ops-pw
Warning: TLS certificate verification is disabled for Software Depot registry calls (matches the 'curl -k' convention used by the other airgapped/scripts/*.sh scripts).
+ vcf-download-tool depot artifacts list --vcf-version=9.1.1 --depot-fqdn=fleet-10-161-10-187.vcfd.broadcom.net --ops-fqdn=10.161.11.108 --ops-user=admin --ops-user-password-file=/home/worker/tmp/.ops-pw
*********Welcome to VCF Download Tool***********

Chain of certificates from 10.161.11.108:
   1: OU=Broadcom\, Inc.,O=Broadcom\, Inc.,CN=VCFOps-slice-1
   2: OU=Broadcom\, Inc.,O=Broadcom\, Inc.,CN=VCFOps-cluster-ca_e02d858f-956f-4843-b2c2-d66b9d9b6c44
Confirm certificates or choose certificate number to review [1-2/Y/N]:Y
Certificate:
    ... (certificate details, elided for brevity) ...
Confirm certificate from fleet-10-161-10-187.vcfd.broadcom.net [Y/N]Y
Version: 9.1.1.0.25665410
Getting artifacts list from the depot.

Waiting for query 3e8a0289-332c-4053-935c-a31f94406a1e to complete (elapsed: 0 seconds)...
Waiting for query 3e8a0289-332c-4053-935c-a31f94406a1e to complete (elapsed: 4 seconds)...
----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
ID                                   | Component                 | Component Full Name          | Version               | Size*      | Release Date | OCI Image Count | Is Partial
----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
8b701b58-ba59-5324-8b30-240e8b421d8a | SUPERVISOR_SERVICE_HARBOR | Harbor Service               | 2.15.2+vmware.1-vks.1 |  119.2 KiB | 08/14/2026   | 1/1             | false
56dbb976-a4c0-5fd2-a09a-83c202668b19 | SUPERVISOR                | VMware vSphere Supervisor    | 9.1.1.0.25667503      |    3.8 GiB | 08/14/2026   |                 | false
b1e5ae53-f878-5b11-8645-5550e00a2539 | DSM                       | VMware Data Services Manager | 9.1.1.0.25662079      |   32.9 GiB | 08/14/2026   | 1/1             | false
----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
3 elements
* Note: Size does not include the sizes of component OCI images.
----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------


Log file: /home/worker/fds/log/vdt.log
Software Depot: fleet-10-161-10-187.vcfd.broadcom.net
Scanned 53 image(s) across the OCI registry catalog.

Managed (17):
  [OK] supervisor-service-harbor/ga/2.15.2/harbor:v2.15.2_vmware.1-vks.1  (component: SUPERVISOR_SERVICE_HARBOR)  (+16 other tag(s) in this image repo)

Unmanaged (29) -- known component, not seen by vcf-download-tool for --vcf-version=9.1.1 (may be available under a different VCF version):
  [!!] supervisor-service-harbor/ga/2.14.2/harbor:v2.14.2_vmware.2-vks.1  (component: SUPERVISOR_SERVICE_HARBOR)  (+16 other tag(s) in this image repo)
  [!!] vcf-service-argocd/ga/1.1.0/argocd-service:v1.1.0_vmware.1  (component: SUPERVISOR_SERVICE_ARGOCD)  (+11 other tag(s) in this image repo)

Remediation commands (run on a host with vcf-download-tool and network access to VCF Operations):

... (one two-step download/upload command pair per unmanaged repo -- see the format under "Option 1" below) ...

Unmapped (7) -- no known component mapping; update COMPONENT_REPO_PREFIXES if these are expected:
  [??] vcf-service-data-services/ga/9.1.1.0/dsm-consumption-operator-supervisor:9.1.1.0.25623779  (+6 other tag(s) in this image repo)

Action needed: see the unmanaged/unmapped sections above.
```

If you're redirecting `check`'s output to a file or `tee` for a record, expect the certificate dump and these prompts to appear inline with the rest of the output, since stdout and stderr are merged and streamed live for exactly this reason -- so that the prompts are visible in time for you to answer them, instead of being buffered until the command exits.

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

# Unmanaged images under: fleet-10-144-79-70.vcfd.broadcom.net/vcf-service-argocd/ga/1.1.0/argocd-service (5 image(s))
# (matched component: SUPERVISOR_SERVICE_ARGOCD; sample tag: v1.1.0_vmware.1)
# To target only this repo with 'delete': --repos vcf-service-argocd/ga/1.1.0/argocd-service  (bare repo path, no depot FQDN)
# NOTE: vcf-download-tool's --vcf-version is the VCF release identifier, not a
# per-image version; the tag above is shown so you can visually confirm it matches
# what --vcf-version=9.1.0 will fetch (override with --remediation-vcf-version
# if these images were built for a different release). <depot-store-dir> and
# <activation-code-file> are placeholders: a local directory to stage the
# downloaded artifact, and your Broadcom Business Services depot download
# activation code file.
vcf-download-tool artifacts download --component=SUPERVISOR_SERVICE_ARGOCD --vcf-version=9.1.0 \
    --depot-store=<depot-store-dir> --depot-download-activation-code-file=<activation-code-file>
vcf-download-tool depot artifacts upload --component=SUPERVISOR_SERVICE_ARGOCD --vcf-version=9.1.0 \
    --depot-store=<depot-store-dir> \
    --depot-fqdn=fleet-10-144-79-70.vcfd.broadcom.net \
    --ops-fqdn=ops.env1.lab.test --ops-user=admin@vsp.local --ops-user-password-file=/root/.ops-pw
```

`--vcf-version` in these two commands is always the **VCF release identifier**, taken from `--remediation-vcf-version` (`9.1.0` by default), not a per-image version — `vcf-download-tool` has no separate per-artifact version flag for this command family. The depot tag `check` found for the unmanaged image is printed as an informational comment above the commands so you can confirm it's the version you expect before running `download`/`upload`.

After running both commands, re-run `check`. When every image is managed, it prints:

```
✅ All 3 image(s) in Software Depot are managed by vcf-download-tool.
```

### Option 2 (Workaround): Delete the unmanaged image

> [!IMPORTANT]
> Deleting an image manifest is **destructive, production-impacting, and effectively irreversible**. Any Supervisor or VKS deployment that still pulls this image by tag will fail after deletion. Use this only if you cannot or do not want to run `vcf-download-tool` for the affected component — Option 1 is always the preferred path.

**Warning:** Deleting a manifest only unlinks it from the registry's tag list; the underlying image blobs — and the repo path's own entry in the registry's `_catalog` listing — are reclaimed only by a separate registry garbage-collection pass, which this script does not perform. It's expected for a repo to still appear in `_catalog` with `{"tags":null}` after a full deletion; `check` already treats a zero-tag repo as gone (it won't show up as managed/unmanaged/unmapped), so this is harmless and requires no action unless you want Software Depot's own storage/catalog fully reclaimed (a registry-administration task, outside what this script's delete can do).

Deletion is gated behind the same [`toggle_software_depot_oci_image_upload.sh`](scripts/toggle_software_depot_oci_image_upload.sh) script used in `air-gapped-vcf91.md` steps 5c/6c: `delete` calls it with `enable` before deleting anything, and with `disable` afterward **no matter what** (success, failure, or interruption), so the depot's OCI registry is never left open longer than necessary. This requires the VSP host and admin credentials already used with that script.

`delete` requires exactly one of `--all` (every unmanaged image) or `--repos` (a comma-separated list of specific repo paths, for when you don't want to delete every unmanaged image) to select scope — there is no implicit "delete everything" default. `--repos` takes the **bare repo path** — the exact value `check` prints on its "To target only this repo with 'delete': --repos ..." hint line — not the `<depot-fqdn>/<repo>` form shown in the "Unmanaged images under:" line above it (the tool tolerates that prefix if pasted by mistake, but the canonical form is the bare path).

```bash
## --vcf-version defaults to 9.1.0 for 'delete' (see the note below); pass it only to override.
./manage_depot_manual_oci_images.py delete \
    --depot-fqdn <software-depot-fqdn> \
    --ops-fqdn <vcf-operations-fqdn> --ops-user <ops-username> \
    --ops-user-password-file <path-to-password-file> \
    --vsp-host <vsp-host-fqdn> --admin-username <admin-username> --admin-password '<admin-password>' \
    (--all | --repos <repo-path>[,<repo-path>...]) [--tag <tag>] [--dry-run]

## Sample Command: delete a specific repo (dry run first, strongly recommended)
./manage_depot_manual_oci_images.py delete \
    --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net \
    --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
    --ops-user-password-file /root/.ops-pw \
    --vsp-host vcf-stls-wcp-pod13-136.lvn.broadcom.net --admin-username admin@vsp.local --admin-password 'Test!23Test!23' \
    --repos vcf-service-argocd/ga/1.1.0/argocd-service --dry-run

## Sample output
1 unmanaged image(s) selected for deletion:
  - vcf-service-argocd/ga/1.1.0/argocd-service  (component: SUPERVISOR_SERVICE_ARGOCD)  (tag: v1.1.0_vmware.1)

--dry-run: no confirmation prompt, no toggle call, and no DELETE requests were made.
```

`--repos` accepts a comma-separated list to target several specific repos in one run (e.g. `--repos vcf-service-argocd/ga/1.1.0/argocd-service,supervisor-service-harbor/ga/2.14.2/harbor`). To delete every unmanaged image found by `check` instead, use `--all` in place of `--repos`.

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

When a repo has multiple tags (e.g. an image plus its cosign `.sig`/`.imgpkg`/`.image-locations.imgpkg` companion tags), some of them commonly share the same underlying manifest digest. Deleting one such tag can make another tag in the same batch stop resolving before the script gets to it — this shows up as `Already gone (no longer resolves, nothing to delete): <repo>:<tag>.` and is counted toward the succeeded total (e.g. `Summary: 8 succeeded (3 of which were already gone), 0 failed.`), not as a failure, since the desired end state — the tag is gone — is already met.

After the deletion completes, run `./manage_depot_manual_oci_images.py check` again to see which unmanaged images (if any) still remain, and iterate — remediating each one via Option 1 or deleting it via this option — until `check` reports everything as managed or intentionally unmapped.

## Additional Information
* [VKS Deployment Guide for VCF 9.1.0 air-gapped environments](/airgapped/air-gapped-vcf91.md) — the guide whose manual upload path causes this issue.
* [VKS Deployment Guide for VCF 9.1.1+ air-gapped environments](/airgapped/air-gapped-vcf911.md) _(to be replaced by techdoc link)_ — the newer guide, unaffected by this issue since `vcf-download-tool` handles OCI images natively from 9.1.1 onward.
* [`oci_image_depot_migrator.py`](scripts/oci_image_depot_migrator.py), [`toggle_software_depot_oci_image_upload.sh`](scripts/toggle_software_depot_oci_image_upload.sh), and [`manage_depot_manual_oci_images.py`](scripts/manage_depot_manual_oci_images.py) — the scripts referenced throughout this article; keep all three together under `airgapped/scripts/`.

<details>
<summary><code>manage_depot_manual_oci_images.py --help</code> reference</summary>

```
$ ./manage_depot_manual_oci_images.py --help
usage: manage_depot_manual_oci_images.py [-h] --depot-fqdn FQDN
                                         [--vcf-version VER]
                                         [--remediation-vcf-version VER]
                                         --ops-fqdn FQDN --ops-user USER
                                         --ops-user-password-file FILE
                                         [--vcf-download-tool PATH]
                                         [--component COMPONENT] [--json]
                                         [--vsp-host HOST]
                                         [--admin-username USER]
                                         [--admin-password PASS]
                                         [--toggle-script PATH] [--all]
                                         [--repos REPO[,REPO...]] [--tag TAG]
                                         [--dry-run] [--yes-i-am-sure WORD]
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
  --vcf-version VER     VCF release identifier, passed to 'vcf-download-tool
                        depot artifacts list'. Defaults to '9.1' for 'check'
                        -- the minor-version form (rather than a specific
                        patch like 9.1.0 or 9.1.1) so the list reports every
                        image released under 9.1.x, avoiding false 'unmanaged'
                        results for images released under a different 9.1.x
                        patch than the one checked. Defaults to the specific
                        patch '9.1.0' for 'delete' instead, since the
                        untracked images this script targets for deletion were
                        manually uploaded only under VCF 9.1.0; a narrower,
                        exact match there avoids treating an image that's
                        genuinely unmanaged under 9.1.0 as managed just
                        because some other 9.1.x patch happens to include a
                        similarly-versioned artifact. Override either default
                        with an explicit value if needed.
  --remediation-vcf-version VER
                        VCF release identifier used in the 'artifacts
                        download'/'depot artifacts upload' remediation
                        commands 'check' prints for each unmanaged image.
                        Unlike --vcf-version, this must be a specific patch
                        (default: 9.1.0, the release these manually-uploaded
                        images were built for) rather than a minor-version
                        wildcard, since vcf-download-tool needs an exact
                        release to actually download/upload an artifact.
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
  --all                 'delete' only: target all unmanaged images. Mutually
                        exclusive with --repos; exactly one of the two is
                        required.
  --repos REPO[,REPO...]
                        'delete' only: comma-separated list of repo paths (as
                        printed by 'check') to restrict deletion to, for when
                        you don't want to delete every unmanaged image.
                        Mutually exclusive with --all; exactly one of the two
                        is required.
  --tag TAG             'delete' only: further restrict to this tag. Requires
                        --repos with exactly one repo.
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
          registry. Requires exactly one of --all (every unmanaged image) or
          --repos (a comma-separated list of specific repo paths) to select
          scope. Re-runs `check` internally (never trusts a stale list),
          requires typing DELETE to confirm (or --yes-i-am-sure DELETE), and
          wraps the deletion in an automatic enable/disable of OCI writes via
          toggle_software_depot_oci_image_upload.sh (disable always runs,
          even on failure or Ctrl-C).

Examples:
  # --vcf-version defaults to 9.1 (all 9.1.x patch releases); omit it unless
  # you need to narrow the check to one specific patch version.
  manage_depot_manual_oci_images.py check \
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net \
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
      --ops-user-password-file /root/.ops-pw

  # --vcf-version defaults to 9.1.0 for 'delete' instead (see --vcf-version
  # above for why); omit it unless these images were uploaded for a different
  # VCF release. Remove --dry-run to delete all untracked images from the
  # Software Depot.
  manage_depot_manual_oci_images.py delete --all \
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net \
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
      --ops-user-password-file /root/.ops-pw \
      --vsp-host vsp.env1.lab.test --admin-username admin@vsp.local \
      --admin-password '...' --dry-run

  # Remove --dry-run to delete the specified images from the Software Depot.
  manage_depot_manual_oci_images.py delete --repos vcf-service-argocd/ga/1.1.0/argocd-service,supervisor-service-harbor/ga/2.14.2/harbor \
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net \
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \
      --ops-user-password-file /root/.ops-pw \
      --vsp-host vsp.env1.lab.test --admin-username admin@vsp.local \
      --admin-password '...' --dry-run
```

</details>
