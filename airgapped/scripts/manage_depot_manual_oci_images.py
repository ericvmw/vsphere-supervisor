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
    """Like migrator.run_cmd, but captures and returns stdout instead of discarding it.

    stdin is inherited from this process (not /dev/null), and stdout/stderr are
    streamed live to our own stderr as they arrive, in addition to being
    captured. vcf-download-tool prompts interactively the first time it talks
    to a given depot/ops-fqdn (TLS certificate chain trust, then CEIP opt-in);
    with stdin redirected to /dev/null those prompts hit immediate EOF and the
    tool aborts with a Java NoSuchElementException instead of a real answer.
    Inheriting stdin lets an operator running this script from a real terminal
    see and answer those prompts; live-streaming is required so the prompt
    text is visible before the operator has to type a response (it would
    otherwise sit buffered until the process exits). Any such prompt/progress
    text is harmless to the caller: parse_managed_versions() only recognizes
    pipe-delimited table rows and silently ignores everything else.
    """
    print(f"+ {' '.join(args)}", file=sys.stderr)
    proc = subprocess.Popen(
        args,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    captured_lines: list[str] = []
    for line in proc.stdout:
        print(line, end="", file=sys.stderr)
        captured_lines.append(line)
    proc.wait()
    if proc.returncode != 0:
        prefix = f"{step}: " if step else ""
        print(f"Error: {prefix}command failed with exit code {proc.returncode}.", file=sys.stderr)
        sys.exit(proc.returncode)
    return "".join(captured_lines)


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
        # A repo path with no tags at all isn't a pullable image -- nothing to
        # manage or report on, so skip it rather than fabricating a placeholder.
        tags = list_repo_tags(depot_fqdn, repo)
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


def parse_managed_versions(list_output: str, known_components: Iterable[str]) -> dict[str, Optional[list[str]]]:
    """
    Two-pass parse of `vcf-download-tool depot artifacts list` output.

    Primary path: the tool prints a pipe-delimited table, e.g.:
        ID | Component | Component Full Name | Version | Size* | Release Date | OCI Image Count | Is Partial
        <uuid> | SUPERVISOR_SERVICE_HARBOR | Harbor Service | 2.15.2+vmware.1-vks.1 | 119.2 KiB | ...
    Columns are split on "|" and stripped; the header row is identified by an
    exact (case-insensitive) "Component" column (or "Artifact"/"Name" as
    fallback header spellings), and the per-row "Version" column is captured
    for each known component. Row iteration stops at the first line with no
    "|" after the header (the closing dashed border / summary line).

    Fallback: if no such table is found (format drift, unexpected output),
    fall back to a whole-token substring match with no version info -- a
    component matched only here accepts any version, since a false
    "unmanaged" is the dangerous failure mode (it feeds the delete workflow)
    when we have no reliable version data to check.

    Returns a dict mapping each managed component to either a list of raw
    version strings tracked for it, or None if only matched via fallback
    (any version accepted). A component absent from the dict is unmanaged.
    """
    known = set(known_components)
    managed: dict[str, Optional[list[str]]] = {}

    lines = [ln for ln in list_output.splitlines() if ln.strip()]

    header_idx: Optional[int] = None
    comp_idx: Optional[int] = None
    ver_idx: Optional[int] = None
    for i, ln in enumerate(lines):
        if "|" not in ln:
            continue
        upper_cols = [c.strip().upper() for c in ln.split("|")]
        candidate_idx = next((j for j, c in enumerate(upper_cols) if c in ("COMPONENT", "ARTIFACT", "NAME")), None)
        if candidate_idx is not None:
            header_idx = i
            comp_idx = candidate_idx
            ver_idx = next((j for j, c in enumerate(upper_cols) if c == "VERSION"), None)
            break

    if header_idx is not None and comp_idx is not None:
        for ln in lines[header_idx + 1 :]:
            # Dashed border rows (both above and below the header, and after
            # the last data row) and any other non-tabular trailer lines
            # (e.g. "N elements", the "* Note:" footnote) have no "|" --
            # skip them rather than treating the first one as end-of-table.
            if "|" not in ln:
                continue
            if re.fullmatch(r"[-=\s]+", ln):  # separator row that happens to contain "|"
                continue
            cols = [c.strip() for c in ln.split("|")]
            if comp_idx >= len(cols):
                continue
            candidate = cols[comp_idx].upper()
            if candidate not in known:
                continue
            version_value = cols[ver_idx] if ver_idx is not None and ver_idx < len(cols) else ""
            if version_value:
                versions = managed.get(candidate)
                if not isinstance(versions, list):
                    versions = []
                versions.append(version_value)
                managed[candidate] = versions
            elif candidate not in managed:
                managed[candidate] = None

    # Fallback: scan every line for each known component identifier as a
    # whole-token, case-insensitive substring match, regardless of column
    # position. No version info is available this way, so a component
    # matched only here accepts any version.
    for ln in lines:
        upper = ln.upper()
        for comp in known:
            if comp in managed:
                continue
            if re.search(rf"\b{re.escape(comp)}\b", upper):
                managed[comp] = None

    return managed


def extract_path_version(repo: str) -> Optional[str]:
    """The version segment of a Software Depot repo path is always the 3rd
    path component, e.g. "supervisor-service-harbor/ga/2.15.2/harbor" -> "2.15.2"."""
    parts = repo.split("/")
    if len(parts) < 3:
        return None
    return parts[2]


@dataclass
class Report:
    managed: list[RepoImage]
    unmanaged: list[RepoImage]
    unmapped: list[RepoImage]


def classify(images: list[RepoImage], managed_versions: dict[str, Optional[list[str]]]) -> Report:
    managed: list[RepoImage] = []
    unmanaged: list[RepoImage] = []
    unmapped: list[RepoImage] = []
    for image in images:
        if image.component is None:
            unmapped.append(image)
            continue
        if image.component not in managed_versions:
            unmanaged.append(image)
            continue
        versions = managed_versions[image.component]
        if versions is None:
            # Component matched, but no VERSION column was found for it --
            # accept any version (see parse_managed_versions docstring).
            managed.append(image)
            continue
        path_version = extract_path_version(image.repo)
        # vcf-download-tool's reported version string is not an exact match
        # of the repo path's version segment (e.g. it may include a build
        # suffix) -- a match is the path version appearing as a substring of
        # a tracked version string, not equality.
        if path_version is not None and any(path_version in v for v in versions):
            managed.append(image)
        else:
            unmanaged.append(image)
    return Report(managed=managed, unmanaged=unmanaged, unmapped=unmapped)


# Digest-derived tags (e.g. cosign "sha256-<hex>.sig"/".att"/".sbom", or a
# bare hex digest used as a tag) aren't meaningful to a human reading a
# report -- prefer a human-named version tag as the representative tag for a
# repo path whenever one is present.
# Matched as a prefix, not a full-string match: cosign/imgpkg attach digest-derived
# tags with varied, multi-segment suffixes (e.g. "sha256-<hex>.sig", ".att", ".sbom",
# ".image-locations.imgpkg"), so anything after the digest is ignored rather than
# required to fit one fixed suffix grammar.
_HASH_LIKE_TAG_PREFIX_RE = re.compile(r"^(?:sha256|sha512)[-:][0-9a-f]{32,}", re.IGNORECASE)
_BARE_HEX_TAG_RE = re.compile(r"^[0-9a-f]{32,}$", re.IGNORECASE)


def is_named_tag(tag: str) -> bool:
    if _HASH_LIKE_TAG_PREFIX_RE.match(tag):
        return False
    if _BARE_HEX_TAG_RE.match(tag):
        return False
    return True


@dataclass
class GroupedImage:
    image: RepoImage  # representative image for the repo path
    total_in_repo: int  # total number of (repo, tag) images collapsed into this one


def collapse_by_repo(images: list[RepoImage]) -> list[GroupedImage]:
    """Group images that share a repo path (differing only by tag) into one
    representative entry, preferring a named tag over a digest-derived one.
    Order of first appearance is preserved."""
    groups: dict[str, list[RepoImage]] = {}
    order: list[str] = []
    for img in images:
        if img.repo not in groups:
            groups[img.repo] = []
            order.append(img.repo)
        groups[img.repo].append(img)

    result: list[GroupedImage] = []
    for repo in order:
        group = groups[repo]
        named = [i for i in group if is_named_tag(i.tag)]
        chosen = named[0] if named else group[0]
        result.append(GroupedImage(image=chosen, total_in_repo=len(group)))
    return result


def print_grouped_images(images: list[RepoImage], marker: str) -> None:
    for grouped in collapse_by_repo(images):
        img = grouped.image
        extra = grouped.total_in_repo - 1
        suffix = f"  (+{extra} other tag(s) in this image repo)" if extra else ""
        component_part = f"  (component: {img.component})" if img.component else ""
        print(f"  {marker} {img.repo}:{img.tag}{component_part}{suffix}")


def print_remediation(grouped: GroupedImage, args: argparse.Namespace) -> None:
    image = grouped.image
    print(f"\n# Unmanaged images under: {args.depot_fqdn}/{image.repo} ({grouped.total_in_repo} image(s))")
    print(f"# (matched component: {image.component}; sample tag: {image.tag})")
    print(f"# To target only this repo with 'delete': --repos {image.repo}  (bare repo path, no depot FQDN)")
    print(
        "# NOTE: vcf-download-tool's --vcf-version is the VCF release identifier, not a\n"
        "# per-image version; the tag above is shown so you can visually confirm it matches\n"
        f"# what --vcf-version={args.remediation_vcf_version} will fetch (override with "
        "--remediation-vcf-version\n"
        "# if these images were built for a different release). <depot-store-dir> and\n"
        "# <activation-code-file> are placeholders: a local directory to stage the\n"
        "# downloaded artifact, and your Broadcom Business Services depot download\n"
        "# activation code file."
    )
    print(
        f"{args.vcf_download_tool} artifacts download --component={image.component} "
        f"--vcf-version={args.remediation_vcf_version} \\\n"
        f"    --depot-store=<depot-store-dir> --depot-download-activation-code-file=<activation-code-file>"
    )
    print(
        f"{args.vcf_download_tool} depot artifacts upload --component={image.component} "
        f"--vcf-version={args.remediation_vcf_version} \\\n"
        f"    --depot-store=<depot-store-dir> \\\n"
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
        print_grouped_images(report.managed, "[OK]")
        print()

    if report.unmanaged:
        print(
            f"Unmanaged ({len(report.unmanaged)}) -- known component, not seen by vcf-download-tool "
            f"for --vcf-version={args.vcf_version} (may be available under a different VCF version):"
        )
        print_grouped_images(report.unmanaged, "[!!]")
        print("\nRemediation commands (run on a host with vcf-download-tool and network access to VCF Operations):")
        for grouped in collapse_by_repo(report.unmanaged):
            print_remediation(grouped, args)
        print()

    if report.unmapped:
        print(
            f"Unmapped ({len(report.unmapped)}) -- no known component mapping; "
            "update COMPONENT_REPO_PREFIXES if these are expected:"
        )
        print_grouped_images(report.unmapped, "[??]")
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
    managed_versions = parse_managed_versions(list_output, COMPONENT_REPO_PREFIXES.keys())
    report = classify(images, managed_versions)
    return print_check_report(report, args)


# --- delete workflow ---
def confirm_delete(targets: list[RepoImage], word: Optional[str]) -> None:
    print("\n*** WARNING: DESTRUCTIVE, PRODUCTION-IMPACTING, IRREVERSIBLE OPERATION ***", file=sys.stderr)
    print(
        "This will permanently delete the following image manifest(s) from the\n"
        "Software Depot OCI registry. Deleting a manifest only unlinks it from the\n"
        "tag list; underlying blobs, and the repo path itself in the registry's\n"
        "_catalog listing, are reclaimed only by a separate registry garbage-\n"
        "collection pass, which this script does NOT perform -- a repo may still\n"
        "appear in _catalog with zero tags after this completes; that is expected.\n"
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


class TagAlreadyGoneError(Exception):
    """The tag no longer resolves to a manifest (HTTP 404 on both HEAD and GET).

    This commonly happens when another tag in the same delete batch shares the
    same underlying manifest digest (e.g. a cosign .sig/.imgpkg/.image-locations
    companion tag, or even the "real" version tag itself): deleting that shared
    digest via one tag name makes every other tag pointing at it stop resolving,
    even though it was never deleted by that name specifically. The desired end
    state (the tag is gone) is already achieved, so this is treated as a
    success, not a failure.
    """


def get_manifest_digest(depot_fqdn: str, repo: str, tag: str) -> str:
    url = f"https://{depot_fqdn}/v2/{repo}/manifests/{tag}"
    headers = {"Accept": _MANIFEST_ACCEPT}
    status, resp_headers, _ = http_request(url, method="HEAD", headers=headers)
    digest = resp_headers.get("Docker-Content-Digest") if status == 200 else None
    last_status = status
    if not digest:
        # Some registries only set the digest header on GET, not HEAD.
        status, resp_headers, _ = http_request(url, method="GET", headers=headers)
        digest = resp_headers.get("Docker-Content-Digest") if status == 200 else None
        last_status = status
    if not digest:
        if last_status == 404:
            raise TagAlreadyGoneError(f"{repo}:{tag} no longer resolves (HTTP 404)")
        raise RuntimeError(f"could not determine manifest digest for {repo}:{tag} (HTTP {last_status})")
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
    managed_versions = parse_managed_versions(list_output, COMPONENT_REPO_PREFIXES.keys())
    report = classify(images, managed_versions)

    targets = report.unmanaged
    if args.repos:
        wanted_repos = set(args.repos)
        targets = [i for i in targets if i.repo in wanted_repos]
    if args.tag:
        targets = [i for i in targets if i.tag == args.tag]

    if not targets:
        print("Nothing to delete: no unmanaged images match the given filters.")
        return 0

    print(f"{len(targets)} unmanaged image(s) selected for deletion:")
    for grouped in collapse_by_repo(targets):
        extra = grouped.total_in_repo - 1
        tag_part = f"tag: {grouped.image.tag}"
        if extra:
            tag_part += f" and +{extra} other tag(s) in this image repo"
        # Repo path shown bare (no ":tag") so it can be copy-pasted directly into --repos.
        print(f"  - {grouped.image.repo}  (component: {grouped.image.component})  ({tag_part})")

    if args.dry_run:
        print("\n--dry-run: no confirmation prompt, no toggle call, and no DELETE requests were made.")
        return 0

    confirm_delete(targets, args.yes_i_am_sure)

    failures: list[RepoImage] = []
    already_gone: list[RepoImage] = []
    with depot_write_enabled(args.toggle_script, args.vsp_host, args.admin_username, args.admin_password):
        for image in targets:
            try:
                digest = get_manifest_digest(args.depot_fqdn, image.repo, image.tag)
                delete_manifest(args.depot_fqdn, image.repo, digest)
                print(f"Deleted {image.repo}:{image.tag} (digest {digest}).")
            except TagAlreadyGoneError:
                # Already removed, most likely as a side effect of deleting another
                # tag in this batch that shared the same underlying manifest digest.
                # The desired end state is met, so this counts as success.
                print(f"Already gone (no longer resolves, nothing to delete): {image.repo}:{image.tag}.")
                already_gone.append(image)
            except Exception as exc:  # collect and continue -- one bad delete must not skip disable
                print(f"Error: failed to delete {image.repo}:{image.tag}: {exc}", file=sys.stderr)
                failures.append(image)

    succeeded = len(targets) - len(failures)
    already_gone_note = f" ({len(already_gone)} of which were already gone)" if already_gone else ""
    print(f"\nSummary: {succeeded} succeeded{already_gone_note}, {len(failures)} failed.")
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
  %(prog)s check \\
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net \\
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \\
      --ops-user-password-file /root/.ops-pw

  # --vcf-version defaults to 9.1.0 for 'delete' instead (see --vcf-version
  # above for why); omit it unless these images were uploaded for a different
  # VCF release. Remove --dry-run to delete the specified images from the
  # Software Depot.
  %(prog)s delete --all \\
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net \\
      --ops-fqdn ops.env1.lab.test --ops-user admin@vsp.local \\
      --ops-user-password-file /root/.ops-pw \\
      --vsp-host vsp.env1.lab.test --admin-username admin@vsp.local \\
      --admin-password '...' --dry-run

  # Remove --dry-run to delete the specified images from the Software Depot.
  %(prog)s delete --repos vcf-service-argocd/ga/1.1.0/argocd-service,supervisor-service-harbor/ga/2.14.2/harbor \\
      --depot-fqdn fleet-10-144-79-70.vcfd.broadcom.net \\
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
        prog="manage_depot_manual_oci_images.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=description,
        epilog=epilog,
    )
    parser.add_argument("action", choices=["check", "delete"], metavar="ACTION", help="check | delete (see examples below).")

    parser.add_argument("--depot-fqdn", required=True, metavar="FQDN", help="Software Depot FQDN.")
    parser.add_argument(
        "--vcf-version", default=None, metavar="VER",
        help="VCF release identifier, passed to 'vcf-download-tool depot artifacts list'. "
        "Defaults to '9.1' for 'check' -- the minor-version form (rather than a specific "
        "patch like 9.1.0 or 9.1.1) so the list reports every image released under 9.1.x, "
        "avoiding false 'unmanaged' results for images released under a different 9.1.x "
        "patch than the one checked. Defaults to the specific patch '9.1.0' for 'delete' "
        "instead, since the untracked images this script targets for deletion were manually "
        "uploaded only under VCF 9.1.0; a narrower, exact match there avoids treating an "
        "image that's genuinely unmanaged under 9.1.0 as managed just because some other "
        "9.1.x patch happens to include a similarly-versioned artifact. Override either "
        "default with an explicit value if needed.",
    )
    parser.add_argument(
        "--remediation-vcf-version", default="9.1.0", metavar="VER",
        help="VCF release identifier used in the 'artifacts download'/'depot artifacts upload' "
        "remediation commands 'check' prints for each unmanaged image. Unlike --vcf-version, "
        "this must be a specific patch (default: 9.1.0, the release these manually-uploaded "
        "images were built for) rather than a minor-version wildcard, since vcf-download-tool "
        "needs an exact release to actually download/upload an artifact.",
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
        "--all", action="store_true",
        help="'delete' only: target all unmanaged images. Mutually exclusive with --repos; "
        "exactly one of the two is required.",
    )
    parser.add_argument(
        "--repos", metavar="REPO[,REPO...]",
        help="'delete' only: comma-separated list of repo paths (as printed by 'check') to restrict "
        "deletion to, for when you don't want to delete every unmanaged image. Mutually exclusive "
        "with --all; exactly one of the two is required.",
    )
    parser.add_argument(
        "--tag", metavar="TAG",
        help="'delete' only: further restrict to this tag. Requires --repos with exactly one repo.",
    )
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

    if args.vcf_version is None:
        args.vcf_version = "9.1.0" if args.action == "delete" else "9.1"

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

        if args.all and args.repos:
            parser.error("--all and --repos are mutually exclusive.")
        if not args.all and not args.repos:
            parser.error("action 'delete' requires exactly one of --all or --repos.")

        repos_list = [r.strip() for r in args.repos.split(",")] if args.repos else []
        repos_list = [r for r in repos_list if r]
        if args.repos and not repos_list:
            parser.error("--repos must contain at least one non-empty repo path.")
        # Tolerate copy-pasting the fully-qualified "<depot-fqdn>/<repo>" form
        # shown in `check`'s remediation output, not just the bare repo path
        # that internally matches RepoImage.repo.
        fqdn_prefix = f"{args.depot_fqdn}/"
        repos_list = [r[len(fqdn_prefix) :] if r.startswith(fqdn_prefix) else r for r in repos_list]
        args.repos = repos_list or None

        if args.tag and (args.all or len(args.repos or []) != 1):
            parser.error("--tag requires --repos with exactly one repo.")

    if args.action == "check":
        sys.exit(cmd_check(args))
    else:
        sys.exit(cmd_delete(args))


if __name__ == "__main__":
    main()
