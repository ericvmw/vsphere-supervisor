#!/usr/bin/env python3

"""
Detect Software Depot OCI images that were uploaded via oci_image_depot_migrator.py
(bypassing vcf-download-tool) and are therefore not managed by vcf-download-tool.

Two positional actions are supported: check, delete.
See --help for details and examples.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oci_image_depot_migrator as migrator  # noqa: E402  (sibling script, same dir)

# Static map: vcf-download-tool --component identifier -> Software Depot OCI
# repo-path PREFIX (leading slash, no trailing slash, no tag). Derived from
# REVERSE_MAPPINGS in oci_image_depot_migrator.py by stripping each entry's
# "/$1" (and "/$1/...:$2") capture-group suffix down to the common literal
# prefix. Keep this in sync with REVERSE_MAPPINGS if that list changes.
#
# Deliberately excluded (not managed by this script): "Harbor VCF service"
# (/vcf-service-harbor/ga), "Metrics aggregator VCF service"
# (/vcf-service-metrics-aggregator/ga), VKR baker image, data consumption,
# encryption management, VCD migration, and "VKSM auto attach VCF service"
# (/vcf-service-vksm-auto-attach/ga -- distinct from VKSM_EXTENSIONS below).
# Repos that match none of these prefixes are reported as "unmapped", never
# silently dropped.
COMPONENT_REPO_PREFIXES: dict[str, str] = {
    "SUPERVISOR_SERVICE_ARGOCD": "/vcf-service-argocd/ga",
    "SUPERVISOR_SERVICE_HARBOR": "/supervisor-service-harbor/ga",
    "SUPERVISOR_SERVICE_LCI": "/supervisor-service-lci/ga",
    "VCF_CONSUMPTION_CLI_PLUGINS": "/vcf-cli-plugins/ga",
    "VCF_SERVICE_CONFIGURATION": "/vcf-service-configuration/ga",
    "SUPERVISOR_SERVICE_CONTOUR": "/supervisor-service-contour/ga",
    "VCF_SERVICE_SECRET_STORE": "/vcf-service-secret-store/ga",
    "VKS_STANDARD_PACKAGES": "/vks-standard-packages/ga",
    "SUPERVISOR_SERVICE_METRICS_AGGREGATOR": "/supervisor-service-metrics-aggregator/ga",
    "SUPERVISOR_SERVICE_EXTDNS": "/supervisor-service-extdns/ga",
    "SUPERVISOR_SERVICE_VKS": "/supervisor-service-vks/ga",
    "SUPERVISOR_SERVICE_SUPERVISOR_MANAGEMENT_PROXY": "/supervisor-service-supervisor-management-proxy/ga",
    "SUPERVISOR_SERVICE_CA_CLUSTERISSUER": "/supervisor-service-ca-clusterissuer/ga",
    "VKSM_EXTENSIONS": "/vksm-extensions/ga",
    "VCF_SERVICE_PROTECTION_AND_RECOVERY": "/vcf-service-protection-and-recovery/ga",
}

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]
)

_SSL_CONTEXT: Optional[ssl.SSLContext] = None
_TLS_WARNING_PRINTED = False


# --- reused from oci_image_depot_migrator.py ---
require_cmd = migrator.require_cmd


def run_capture(args: list[str], *, step: str = "") -> str:
    """Like migrator.run_cmd, but captures and returns stdout instead of discarding it."""
    print(f"+ {' '.join(args)}", file=sys.stderr)
    r = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if r.returncode != 0:
        prefix = f"{step}: " if step else ""
        print(f"Error: {prefix}command failed with exit code {r.returncode}.", file=sys.stderr)
        if r.stderr:
            print(r.stderr, file=sys.stderr)
        sys.exit(r.returncode)
    if r.stderr:
        print(r.stderr, file=sys.stderr, end="" if r.stderr.endswith("\n") else "\n")
    return r.stdout


# --- TLS / HTTP (stdlib only; -k-equivalent to match the repo's curl -k convention) ---
def _ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT, _TLS_WARNING_PRINTED
    if _SSL_CONTEXT is None:
        if not _TLS_WARNING_PRINTED:
            print(
                "Warning: TLS certificate verification is disabled for Software Depot "
                "registry calls (matches the 'curl -k' convention used by the other "
                "airgapped/scripts/*.sh scripts).",
                file=sys.stderr,
            )
            _TLS_WARNING_PRINTED = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _SSL_CONTEXT = ctx
    return _SSL_CONTEXT


def http_request(
    url: str, method: str = "GET", headers: Optional[dict[str, str]] = None
) -> tuple[int, Any, bytes]:
    """One request; never raises on non-2xx -- returns (status, headers, body)."""
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()
    except urllib.error.URLError as exc:
        print(f"Error: request to {url} failed: {exc}", file=sys.stderr)
        sys.exit(1)


def _parse_next_link(link_header: Optional[str], base_url: str) -> Optional[str]:
    if not link_header:
        return None
    m = _LINK_NEXT_RE.search(link_header)
    if not m:
        return None
    return urllib.parse.urljoin(base_url, m.group(1))


# --- registry catalog / tags (Distribution API v2, RFC5988 Link pagination) ---
def list_catalog_repos(depot_fqdn: str) -> list[str]:
    repos: list[str] = []
    url = f"https://{depot_fqdn}/v2/_catalog?n=100"
    seen_urls: set[str] = set()
    for _ in range(1000):  # defensive cap against a pagination loop
        if url in seen_urls:
            print(f"Warning: pagination loop detected at {url}; stopping.", file=sys.stderr)
            break
        seen_urls.add(url)
        status, headers, body = http_request(url)
        if status != 200:
            print(f"Error: GET {url} returned HTTP {status}.", file=sys.stderr)
            sys.exit(1)
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            print(f"Error: could not parse catalog JSON from {url}: {exc}", file=sys.stderr)
            sys.exit(1)
        repos.extend(data.get("repositories") or [])
        next_url = _parse_next_link(headers.get("Link"), url)
        if not next_url:
            break
        url = next_url
    return repos


def list_repo_tags(depot_fqdn: str, repo: str) -> list[str]:
    tags: list[str] = []
    url = f"https://{depot_fqdn}/v2/{repo}/tags/list?n=100"
    seen_urls: set[str] = set()
    for _ in range(1000):
        if url in seen_urls:
            print(f"Warning: pagination loop detected at {url}; stopping.", file=sys.stderr)
            break
        seen_urls.add(url)
        status, headers, body = http_request(url)
        if status == 404:
            print(f"Warning: repo '{repo}' returned 404 for tags/list; treating as no tags.", file=sys.stderr)
            break
        if status != 200:
            print(f"Warning: GET {url} returned HTTP {status}; treating repo as having no tags.", file=sys.stderr)
            break
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            print(f"Warning: could not parse tags JSON from {url}: {exc}", file=sys.stderr)
            break
        tags.extend(data.get("tags") or [])
        next_url = _parse_next_link(headers.get("Link"), url)
        if not next_url:
            break
        url = next_url
    return tags


def match_component_for_repo(repo: str) -> Optional[str]:
    """repo has no leading slash (as returned by _catalog); longest-prefix match."""
    repo_path = "/" + repo.lstrip("/")
    best_component: Optional[str] = None
    best_len = -1
    for component, prefix in COMPONENT_REPO_PREFIXES.items():
        if repo_path == prefix or repo_path.startswith(prefix + "/"):
            if len(prefix) > best_len:
                best_component = component
                best_len = len(prefix)
    return best_component


@dataclass
class RepoImage:
    repo: str
    tag: str
    component: Optional[str]


def scan_depot(depot_fqdn: str, only_components: Optional[set[str]] = None) -> list[RepoImage]:
    images: list[RepoImage] = []
    for repo in list_catalog_repos(depot_fqdn):
        component = match_component_for_repo(repo)
        if only_components and component not in only_components:
            continue
        tags = list_repo_tags(depot_fqdn, repo)
        if not tags:
            images.append(RepoImage(repo=repo, tag="<none>", component=component))
            continue
        for tag in tags:
            images.append(RepoImage(repo=repo, tag=tag, component=component))
    return images


# --- vcf-download-tool invocation + parsing ---
def run_vcf_download_tool_list(
    tool: str,
    vcf_version: str,
    depot_fqdn: str,
    ops_fqdn: str,
    ops_user: str,
    ops_user_password_file: Path,
) -> str:
    require_cmd(tool)
    return run_capture(
        [
            tool,
            "depot",
            "artifacts",
            "list",
            f"--vcf-version={vcf_version}",
            f"--depot-fqdn={depot_fqdn}",
            f"--ops-fqdn={ops_fqdn}",
            f"--ops-user={ops_user}",
            f"--ops-user-password-file={ops_user_password_file}",
        ],
        step="vcf-download-tool depot artifacts list",
    )


def parse_managed_components(list_output: str, known_components: Iterable[str]) -> set[str]:
    """
    Defensive two-pass parse of `vcf-download-tool depot artifacts list` output.

    A false "unmanaged" is the dangerous failure mode (it feeds the delete
    workflow), so this function is deliberately biased toward over-matching
    "managed": it unions (never intersects) a strict tabular parse with a
    whole-token substring fallback.
    """
    known = set(known_components)
    managed: set[str] = set()

    lines = [ln for ln in list_output.splitlines() if ln.strip()]

    # Strict path: assume a tabwriter-style table with a COMPONENT/ARTIFACT/NAME
    # header column (the shape documented for the sibling `depot binaries list`
    # command), columns separated by 2+ spaces.
    header_idx = next(
        (i for i, ln in enumerate(lines) if re.search(r"\b(COMPONENT|ARTIFACT|NAME)\b", ln, re.IGNORECASE)),
        None,
    )
    if header_idx is not None:
        header_cols = re.split(r"\s{2,}", lines[header_idx].strip())
        col_idx = next(
            (i for i, c in enumerate(header_cols) if c.strip().upper() in ("COMPONENT", "ARTIFACT", "NAME")),
            None,
        )
        if col_idx is not None:
            for ln in lines[header_idx + 1 :]:
                if re.fullmatch(r"[-=\s]+", ln):  # separator row
                    continue
                cols = re.split(r"\s{2,}", ln.strip())
                if col_idx < len(cols):
                    candidate = cols[col_idx].strip().upper()
                    if candidate in known:
                        managed.add(candidate)

    # Fallback: the real tabular shape of `depot artifacts list` is not
    # publicly documented, so also scan every line for each known component
    # identifier as a whole-token, case-insensitive substring match,
    # regardless of column position.
    for ln in lines:
        upper = ln.upper()
        for comp in known:
            if comp in managed:
                continue
            if re.search(rf"\b{re.escape(comp)}\b", upper):
                managed.add(comp)

    return managed


@dataclass
class Report:
    managed: list[RepoImage]
    unmanaged: list[RepoImage]
    unmapped: list[RepoImage]


def classify(images: list[RepoImage], managed_components: set[str]) -> Report:
    managed: list[RepoImage] = []
    unmanaged: list[RepoImage] = []
    unmapped: list[RepoImage] = []
    for image in images:
        if image.component is None:
            unmapped.append(image)
        elif image.component in managed_components:
            managed.append(image)
        else:
            unmanaged.append(image)
    return Report(managed=managed, unmanaged=unmanaged, unmapped=unmapped)


def print_remediation(image: RepoImage, args: argparse.Namespace) -> None:
    print(f"\n# Unmanaged image: {args.depot_fqdn}/{image.repo}:{image.tag}")
    print(f"# (matched component: {image.component}; depot tag found: {image.tag})")
    print(
        "# NOTE: vcf-download-tool's depot-artifacts commands take --vcf-version as the\n"
        "# VCF release identifier, not a per-image version; the tag above is shown so\n"
        f"# you can visually confirm it matches what --vcf-version={args.vcf_version} will fetch."
    )
    print(
        f"{args.vcf_download_tool} depot artifacts download --component={image.component} "
        f"--vcf-version={args.vcf_version} \\\n"
        f"    --ops-fqdn={args.ops_fqdn} --ops-user={args.ops_user} "
        f"--ops-user-password-file={args.ops_user_password_file}"
    )
    print(
        f"{args.vcf_download_tool} depot artifacts upload --component={image.component} "
        f"--vcf-version={args.vcf_version} \\\n"
        f"    --depot-fqdn={args.depot_fqdn} \\\n"
        f"    --ops-fqdn={args.ops_fqdn} --ops-user={args.ops_user} "
        f"--ops-user-password-file={args.ops_user_password_file}"
    )


def print_check_report(report: Report, args: argparse.Namespace) -> int:
    total = len(report.managed) + len(report.unmanaged) + len(report.unmapped)

    if args.json:
        payload = {
            "depot_fqdn": args.depot_fqdn,
            "managed": [vars(i) for i in report.managed],
            "unmanaged": [vars(i) for i in report.unmanaged],
            "unmapped": [vars(i) for i in report.unmapped],
        }
        print(json.dumps(payload, indent=2))
        return 0 if not report.unmanaged and not report.unmapped else 1

    print(f"Software Depot: {args.depot_fqdn}")
    print(f"Scanned {total} image(s) across the OCI registry catalog.\n")

    if report.managed:
        print(f"Managed ({len(report.managed)}):")
        for image in report.managed:
            print(f"  [OK] {image.repo}:{image.tag}  (component: {image.component})")
        print()

    if report.unmanaged:
        print(f"Unmanaged ({len(report.unmanaged)}) -- known component, not seen by vcf-download-tool:")
        for image in report.unmanaged:
            print(f"  [!!] {image.repo}:{image.tag}  (component: {image.component})")
        print("\nRemediation commands (run on a host with vcf-download-tool and network access to VCF Operations):")
        for image in report.unmanaged:
            print_remediation(image, args)
        print()

    if report.unmapped:
        print(
            f"Unmapped ({len(report.unmapped)}) -- no known component mapping; "
            "update COMPONENT_REPO_PREFIXES if these are expected:"
        )
        for image in report.unmapped:
            print(f"  [??] {image.repo}:{image.tag}")
        print()

    if not report.unmanaged and not report.unmapped:
        print(f"✅ All {total} image(s) in Software Depot are managed by vcf-download-tool.")
        return 0

    print("Action needed: see the unmanaged/unmapped sections above.")
    return 1


def cmd_check(args: argparse.Namespace) -> int:
    only_components = set(args.component) if args.component else None
    images = scan_depot(args.depot_fqdn, only_components=only_components)
    list_output = run_vcf_download_tool_list(
        args.vcf_download_tool,
        args.vcf_version,
        args.depot_fqdn,
        args.ops_fqdn,
        args.ops_user,
        args.ops_user_password_file,
    )
    managed_components = parse_managed_components(list_output, COMPONENT_REPO_PREFIXES.keys())
    report = classify(images, managed_components)
    return print_check_report(report, args)


# --- delete workflow ---
def confirm_delete(targets: list[RepoImage], word: Optional[str]) -> None:
    print("\n*** WARNING: DESTRUCTIVE, PRODUCTION-IMPACTING, IRREVERSIBLE OPERATION ***", file=sys.stderr)
    print(
        "This will permanently delete the following image manifest(s) from the\n"
        "Software Depot OCI registry. Deleting a manifest only unlinks it from the\n"
        "tag list; underlying blobs are reclaimed only by a separate registry\n"
        "garbage-collection pass, which this script does NOT perform.\n"
        "Any Supervisor / VKS deployment that still references these images by tag\n"
        "will fail to pull them after this runs.\n",
        file=sys.stderr,
    )
    for image in targets:
        print(f"  - {image.repo} : {image.tag}", file=sys.stderr)
    print(file=sys.stderr)

    if word == "DELETE":
        print("--yes-i-am-sure DELETE supplied; skipping interactive confirmation.", file=sys.stderr)
        return

    try:
        answer = input("Type DELETE (all caps) to proceed, anything else aborts: ")
    except EOFError:
        answer = ""
    if answer != "DELETE":
        print("Aborted.", file=sys.stderr)
        sys.exit(1)


def toggle_depot(mode: str, toggle_script: Path, vsp_host: str, admin_username: str, admin_password: str) -> None:
    if not toggle_script.is_file():
        print(f"Error: toggle script not found at {toggle_script}", file=sys.stderr)
        sys.exit(1)
    print(
        f"+ {toggle_script.name} {mode} --vsp-host {vsp_host} "
        f"--admin-username {admin_username} --admin-password ****",
        file=sys.stderr,
    )
    r = subprocess.run(
        [str(toggle_script), mode, "--vsp-host", vsp_host, "--admin-username", admin_username, "--admin-password", admin_password],
        stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        if mode == "enable":
            print(
                f"Error: failed to enable Software Depot OCI writes (exit {r.returncode}); "
                "aborting before any deletion.",
                file=sys.stderr,
            )
            sys.exit(r.returncode)
        else:
            print(
                f"WARNING: failed to disable Software Depot OCI writes (exit {r.returncode}). "
                "The depot's OCI registry may still be left open for unauthenticated writes/deletes -- "
                "re-run 'toggle_software_depot_oci_image_upload.sh disable' manually as soon as possible.",
                file=sys.stderr,
            )


@contextlib.contextmanager
def depot_write_enabled(toggle_script: Path, vsp_host: str, admin_username: str, admin_password: str):
    """Python equivalent of bash's `trap ... EXIT`: disable always runs, even on
    exceptions or KeyboardInterrupt (Ctrl-C is delivered as an exception into
    the `with` body). Cannot survive SIGKILL/power loss -- same class of gap
    as the bash scripts' own `trap ... EXIT`."""
    toggle_depot("enable", toggle_script, vsp_host, admin_username, admin_password)
    try:
        yield
    finally:
        toggle_depot("disable", toggle_script, vsp_host, admin_username, admin_password)


def get_manifest_digest(depot_fqdn: str, repo: str, tag: str) -> str:
    url = f"https://{depot_fqdn}/v2/{repo}/manifests/{tag}"
    headers = {"Accept": _MANIFEST_ACCEPT}
    status, resp_headers, _ = http_request(url, method="HEAD", headers=headers)
    digest = resp_headers.get("Docker-Content-Digest") if status == 200 else None
    if not digest:
        # Some registries only set the digest header on GET, not HEAD.
        status, resp_headers, _ = http_request(url, method="GET", headers=headers)
        digest = resp_headers.get("Docker-Content-Digest") if status == 200 else None
    if not digest:
        raise RuntimeError(f"could not determine manifest digest for {repo}:{tag} (HTTP {status})")
    return digest


def delete_manifest(depot_fqdn: str, repo: str, digest: str) -> None:
    url = f"https://{depot_fqdn}/v2/{repo}/manifests/{digest}"
    status, _, body = http_request(url, method="DELETE")
    if status not in (200, 202, 204):
        raise RuntimeError(f"DELETE {url} returned HTTP {status}: {body[:500]!r}")


def cmd_delete(args: argparse.Namespace) -> int:
    # Re-run the full check pipeline fresh -- never trust a cached/stale
    # unmanaged list (avoids drift between a prior `check` and this `delete`).
    images = scan_depot(args.depot_fqdn)
    list_output = run_vcf_download_tool_list(
        args.vcf_download_tool,
        args.vcf_version,
        args.depot_fqdn,
        args.ops_fqdn,
        args.ops_user,
        args.ops_user_password_file,
    )
    managed_components = parse_managed_components(list_output, COMPONENT_REPO_PREFIXES.keys())
    report = classify(images, managed_components)

    targets = report.unmanaged
    if args.repo:
        wanted_repos = set(args.repo)
        targets = [i for i in targets if i.repo in wanted_repos]
    if args.tag:
        targets = [i for i in targets if i.tag == args.tag]

    if not targets:
        print("Nothing to delete: no unmanaged images match the given filters.")
        return 0

    print(f"{len(targets)} unmanaged image(s) selected for deletion:")
    for image in targets:
        print(f"  - {image.repo}:{image.tag}  (component: {image.component})")

    if args.dry_run:
        print("\n--dry-run: no confirmation prompt, no toggle call, and no DELETE requests were made.")
        return 0

    confirm_delete(targets, args.yes_i_am_sure)

    failures: list[RepoImage] = []
    with depot_write_enabled(args.toggle_script, args.vsp_host, args.admin_username, args.admin_password):
        for image in targets:
            try:
                digest = get_manifest_digest(args.depot_fqdn, image.repo, image.tag)
                delete_manifest(args.depot_fqdn, image.repo, digest)
                print(f"Deleted {image.repo}:{image.tag} (digest {digest}).")
            except Exception as exc:  # collect and continue -- one bad delete must not skip disable
                print(f"Error: failed to delete {image.repo}:{image.tag}: {exc}", file=sys.stderr)
                failures.append(image)

    print(f"\nSummary: {len(targets) - len(failures)} succeeded, {len(failures)} failed.")
    return 0 if not failures else 1


def main() -> None:
    epilog = """\
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
  %(prog)s check \\
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net --vcf-version 9.1.0 \\
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \\
      --ops-user-password-file /root/.ops-pw

  %(prog)s delete \\
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net --vcf-version 9.1.0 \\
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \\
      --ops-user-password-file /root/.ops-pw \\
      --vsp-host vsp.env1.lab.test --admin-username admin@vsp.local \\
      --admin-password '...' --dry-run
"""
    description = (
        "Detect Software Depot OCI images uploaded via oci_image_depot_migrator.py that\n"
        "are not managed by vcf-download-tool, print remediation commands, and optionally\n"
        "delete unmanaged images as a last-resort workaround.\n"
        "\n"
        "Action is a required positional argument:\n"
        "  check | delete"
    )

    parser = argparse.ArgumentParser(
        prog="verify_depot_oci_management.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=description,
        epilog=epilog,
    )
    parser.add_argument("action", choices=["check", "delete"], metavar="ACTION", help="check | delete (see examples below).")

    parser.add_argument("--depot-fqdn", required=True, metavar="FQDN", help="Software Depot (Fleet Depot Server) FQDN.")
    parser.add_argument(
        "--vcf-version", required=True, metavar="VER",
        help="VCF release identifier, e.g. 9.1.0. Passed through to vcf-download-tool.",
    )
    parser.add_argument("--ops-fqdn", required=True, metavar="FQDN", help="VCF Operations FQDN. Passed through to vcf-download-tool.")
    parser.add_argument("--ops-user", required=True, metavar="USER", help="VCF Operations username. Passed through to vcf-download-tool.")
    parser.add_argument(
        "--ops-user-password-file", required=True, type=Path, metavar="FILE",
        help="Path to a file containing the VCF Operations user's password. Passed through to "
        "vcf-download-tool; never read or printed by this script.",
    )
    parser.add_argument(
        "--vcf-download-tool", default="vcf-download-tool", metavar="PATH",
        help="vcf-download-tool binary name or path. Default: vcf-download-tool (resolved via PATH).",
    )
    parser.add_argument(
        "--component", action="append", metavar="COMPONENT", choices=sorted(COMPONENT_REPO_PREFIXES),
        help="Restrict 'check' to one component (repeatable). Default: scan all known components.",
    )
    parser.add_argument("--json", action="store_true", help="'check' only: emit a machine-readable JSON report instead of text.")

    parser.add_argument("--vsp-host", metavar="HOST", help="'delete' only: VSP host, forwarded to toggle_software_depot_oci_image_upload.sh.")
    parser.add_argument(
        "--admin-username", metavar="USER",
        help="'delete' only: VSP admin username, forwarded to toggle_software_depot_oci_image_upload.sh.",
    )
    parser.add_argument(
        "--admin-password", metavar="PASS",
        help="'delete' only: VSP admin password, forwarded to toggle_software_depot_oci_image_upload.sh.",
    )
    parser.add_argument(
        "--toggle-script", type=Path, default=None, metavar="PATH",
        help="Path to toggle_software_depot_oci_image_upload.sh. Default: the copy next to this script.",
    )
    parser.add_argument(
        "--repo", action="append", metavar="REPO",
        help="'delete' only: restrict deletion to this repo path as printed by 'check' (repeatable). "
        "Default: all unmanaged images.",
    )
    parser.add_argument("--tag", metavar="TAG", help="'delete' only: restrict to this tag (combine with --repo to target one image).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="'delete' only: print the deletion plan and exit; no prompt, no toggle, no DELETE calls.",
    )
    parser.add_argument(
        "--yes-i-am-sure", metavar="WORD", default=None,
        help="'delete' only: non-interactive bypass for the confirmation prompt. Must be exactly "
        "'DELETE'. Use with extreme caution.",
    )

    args = parser.parse_args()

    if args.yes_i_am_sure is not None and args.yes_i_am_sure != "DELETE":
        parser.error("--yes-i-am-sure must be exactly 'DELETE' if given.")

    if args.toggle_script is None:
        args.toggle_script = Path(__file__).resolve().parent / "toggle_software_depot_oci_image_upload.sh"

    if args.action == "delete":
        missing = [
            name
            for name, value in (
                ("--vsp-host", args.vsp_host),
                ("--admin-username", args.admin_username),
                ("--admin-password", args.admin_password),
            )
            if not value
        ]
        if missing:
            parser.error(f"action 'delete' requires {', '.join(missing)}.")

    if args.action == "check":
        sys.exit(cmd_check(args))
    else:
        sys.exit(cmd_delete(args))


if __name__ == "__main__":
    main()
