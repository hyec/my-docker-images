#!/usr/bin/env python3
"""Resolve subscribed base images, then check or publish derived images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import oras.client
import oras.defaults
import requests
import yaml
from requests.adapters import HTTPAdapter


ROOT = Path(__file__).resolve().parents[2]
GENERATED = {"Dockerfile", ".sync-state.json"}
METADATA = GENERATED | {"Dockerfile.template", "subscription.yaml"}
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
UPSTREAM_RE = re.compile(r"(?:docker\.io|ghcr\.io)/[a-z0-9_./-]+\Z")
PLATFORM_RE = re.compile(r"linux/[a-z0-9]+(?:/[a-z0-9]+)?\Z")
SEMVER_RE = re.compile(
    r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\Z"
)


class SyncError(Exception):
    pass


class ImageNotFound(SyncError):
    pass


class TimeoutAdapter(HTTPAdapter):
    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = 20
        return super().send(request, **kwargs)


@dataclass(frozen=True)
class Subscription:
    name: str
    directory: Path
    upstream: str
    tag_regex: str
    sort: str
    platforms: tuple[str, ...]
    enabled: bool


def load_subscriptions(root: Path) -> list[Subscription]:
    if not root.is_dir():
        raise SyncError(f"missing repository directory: {root}")
    subscriptions = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        config_path = directory / "subscription.yaml"
        if not config_path.is_file():
            raise SyncError(f"missing subscription.yaml: {directory}")
        if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", directory.name):
            raise SyncError(f"invalid image name: {directory.name}")
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SyncError(f"cannot read subscription {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SyncError(f"subscription must be a mapping: {config_path}")
        allowed = {"enabled", "upstream", "tag_regex", "sort", "platforms"}
        unknown = set(raw) - allowed
        if unknown:
            raise SyncError(f"unknown subscription keys in {config_path}: {sorted(unknown)}")
        upstream = raw.get("upstream")
        if not isinstance(upstream, str) or not UPSTREAM_RE.fullmatch(upstream):
            raise SyncError(f"upstream must be a full docker.io or ghcr.io repository: {config_path}")
        if "//" in upstream or "/../" in upstream or upstream.endswith("/"):
            raise SyncError(f"invalid upstream repository: {upstream}")
        tag_regex = raw.get("tag_regex")
        if not isinstance(tag_regex, str):
            raise SyncError(f"tag_regex must be a string: {config_path}")
        try:
            re.compile(tag_regex)
        except re.error as exc:
            raise SyncError(f"invalid tag_regex in {config_path}: {exc}") from exc
        sort = raw.get("sort", "semver")
        if not isinstance(sort, str) or sort not in {"semver", "lexicographic"}:
            raise SyncError(f"sort must be semver or lexicographic: {config_path}")
        platforms = raw.get("platforms")
        if (
            not isinstance(platforms, list)
            or not platforms
            or any(not isinstance(p, str) or not PLATFORM_RE.fullmatch(p) for p in platforms)
            or len(platforms) != len(set(platforms))
        ):
            raise SyncError(f"platforms must be a nonempty list of unique linux platforms: {config_path}")
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise SyncError(f"enabled must be true or false: {config_path}")
        if not (directory / "Dockerfile.template").is_file():
            raise SyncError(f"missing Dockerfile.template: {directory}")
        subscriptions.append(
            Subscription(directory.name, directory, upstream, tag_regex, sort, tuple(platforms), enabled)
        )
    return subscriptions


def semver_key(tag: str) -> tuple[Any, ...]:
    match = SEMVER_RE.fullmatch(tag)
    if not match:
        raise SyncError(f"tag {tag!r} is not SemVer; change its filter or use lexicographic sorting")
    major, minor, patch = (int(match.group(i)) for i in range(1, 4))
    prerelease = match.group(4)
    if prerelease is None:
        suffix: tuple[Any, ...] = (1,)
    else:
        identifiers = prerelease.split(".")
        if any(not part or (part.isdigit() and len(part) > 1 and part.startswith("0")) for part in identifiers):
            raise SyncError(f"tag {tag!r} has an invalid SemVer prerelease")
        suffix = (0, tuple((0, int(part)) if part.isdigit() else (1, part) for part in identifiers))
    return major, minor, patch, suffix


def select_tag(tags: list[str], pattern: str, sort: str) -> str:
    regex = re.compile(pattern)
    matches = [tag for tag in tags if regex.fullmatch(tag)]
    if not matches:
        raise SyncError(f"no upstream tag matches {pattern!r}")
    if sort == "semver":
        return max(matches, key=lambda tag: (semver_key(tag), tag))
    return max(matches)


def parse_platforms(manifest: dict[str, Any], config: dict[str, Any] | None = None) -> set[str]:
    if "manifests" in manifest:
        entries = manifest["manifests"]
        if not isinstance(entries, list):
            raise SyncError("invalid image index: manifests is not a list")
        platforms = [entry.get("platform", {}) for entry in entries]
    elif config is not None:
        platforms = [config]
    else:
        raise SyncError("single-platform image requires its image config")
    result = set()
    for platform in platforms:
        if not isinstance(platform, dict):
            continue
        os_name = platform.get("os")
        arch = platform.get("architecture")
        if not os_name or not arch or os_name == "unknown" or arch == "unknown":
            continue
        name = f"{os_name}/{arch}"
        variant = platform.get("variant")
        if variant and arch != "arm64":
            name += f"/{variant}"
        result.add(name)
    return result


def render_dockerfile(spec: Subscription, tag: str, digest: str) -> str:
    if not DIGEST_RE.fullmatch(digest):
        raise SyncError(f"invalid upstream digest: {digest}")
    template = (spec.directory / "Dockerfile.template").read_text(encoding="utf-8")
    placeholder = "${latest}"
    expected_from = f"{spec.upstream}:{placeholder}"
    found = False
    for line in template.splitlines():
        if placeholder not in line:
            continue
        match = re.match(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", line, re.IGNORECASE)
        if not match or match.group(1) != expected_from or line.count(placeholder) != 1:
            raise SyncError(f"${{latest}} must occur only in FROM {expected_from}: {spec.directory}")
        found = True
    if not found:
        raise SyncError(f"template has no FROM {expected_from}: {spec.directory}")
    return "# Generated from Dockerfile.template; edit the template instead.\n" + template.replace(
        placeholder, f"{tag}@{digest}"
    )


def context_digest(directory: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if relative.parts[0] in GENERATED:
            continue
        if path.is_symlink():
            raise SyncError(f"symlinks are not supported in image contexts: {path}")
        hasher.update(str(relative).encode() + b"\0")
        if path.is_dir():
            hasher.update(b"d\0")
        elif path.is_file():
            mode = path.stat().st_mode
            hasher.update(b"x\0" if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else b"f\0")
            hasher.update(path.read_bytes())
            hasher.update(b"\0")
        else:
            raise SyncError(f"unsupported file in image context: {path}")
    return hasher.hexdigest()


def read_state(directory: Path) -> dict[str, Any] | None:
    path = directory / ".sync-state.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncError(f"invalid state file {path}: {exc}") from exc
    if not isinstance(state, dict):
        raise SyncError(f"invalid state file {path}: expected an object")
    return state


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp, indent=2, sort_keys=True, ensure_ascii=False)
        tmp.write("\n")
        temp_path = Path(tmp.name)
    os.replace(temp_path, path)


class Registry:
    def __init__(self) -> None:
        self.clients: dict[str, oras.client.OrasClient] = {}

    def _client(self, ref: str) -> tuple[oras.client.OrasClient, Any]:
        # Docker Hub's registry API is served from registry-1.docker.io.
        registry_ref = ref.replace("docker.io/", "registry-1.docker.io/", 1) if ref.startswith("docker.io/") else ref
        hostname = registry_ref.split("/", 1)[0]
        repository = registry_ref.split("@", 1)[0].split(":", 1)[0]
        if repository not in self.clients:
            self.clients[repository] = oras.client.OrasClient(hostname=hostname)
            self.clients[repository].session.mount("https://", TimeoutAdapter())
        client = self.clients[repository]
        container = client.get_container(registry_ref)
        client.auth.load_configs(container)
        return client, container

    @staticmethod
    def _check_response(ref: str, response: requests.Response, *, missing_ok: bool = False) -> bool:
        if response.status_code == 404 and missing_ok:
            return False
        if response.status_code != 200:
            raise SyncError(f"registry request for {ref} failed: HTTP {response.status_code}")
        return True

    def _manifest(self, ref: str, *, missing_ok: bool = False) -> tuple[dict[str, Any], str] | None:
        client, container = self._client(ref)
        url = f"{client.prefix}://{container.manifest_url()}"
        headers = {"Accept": ", ".join(oras.defaults.default_manifest_accepted_media_types)}
        try:
            # ORAS retries failed authentication for several minutes. For an
            # optional destination lookup, one attempt is enough to detect an
            # unpublished GHCR package.
            once = getattr(client.do_request, "__wrapped__", None) if missing_ok else None
            response = once(client, url, "GET", headers=headers) if once else client.do_request(url, "GET", headers=headers)
        except (requests.RequestException, ValueError) as exc:
            if missing_ok and str(exc) == "Cannot respond to request for authentication.":
                return None
            raise SyncError(f"registry request for {ref} failed: {exc}") from exc
        if not self._check_response(ref, response, missing_ok=missing_ok):
            return None
        digest = response.headers.get("Docker-Content-Digest") or f"sha256:{hashlib.sha256(response.content).hexdigest()}"
        if not DIGEST_RE.fullmatch(digest):
            raise SyncError(f"invalid manifest digest for {ref}: {digest}")
        try:
            manifest = response.json()
        except ValueError as exc:
            raise SyncError(f"invalid manifest for {ref}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise SyncError(f"invalid manifest for {ref}: expected an object")
        return manifest, digest

    def tags(self, repository: str) -> list[str]:
        client, container = self._client(repository)
        try:
            return client.get_tags(container)
        except (requests.RequestException, ValueError) as exc:
            raise SyncError(f"cannot list tags for {repository}: {exc}") from exc

    def digest(self, ref: str, *, missing_ok: bool = False) -> str | None:
        result = self._manifest(ref, missing_ok=missing_ok)
        return result[1] if result else None

    def platforms(self, ref: str) -> set[str]:
        result = self._manifest(ref)
        assert result is not None
        manifest = result[0]
        if "manifests" in manifest:
            return parse_platforms(manifest)
        config_descriptor = manifest.get("config")
        if not isinstance(config_descriptor, dict) or not DIGEST_RE.fullmatch(str(config_descriptor.get("digest", ""))):
            raise SyncError(f"invalid image config descriptor for {ref}")
        client, container = self._client(ref)
        try:
            response = client.get_blob(container, config_descriptor["digest"])
        except (requests.RequestException, ValueError) as exc:
            raise SyncError(f"cannot read image config for {ref}: {exc}") from exc
        self._check_response(ref, response)
        try:
            config = response.json()
            if not isinstance(config, dict):
                raise ValueError("expected an object")
            return parse_platforms(manifest, config)
        except ValueError as exc:
            raise SyncError(f"invalid image config for {ref}: {exc}") from exc


class Builder:
    def build(self, spec: Subscription, dockerfile: str, platforms: tuple[str, ...], tags: tuple[str, str], source: str) -> None:
        with tempfile.TemporaryDirectory(prefix=f"sync-{spec.name}-") as temp:
            context = Path(temp) / "context"
            shutil.copytree(spec.directory, context, ignore=lambda _directory, names: METADATA & set(names))
            dockerfile_path = Path(temp) / "Dockerfile"
            dockerfile_path.write_text(dockerfile, encoding="utf-8")
            command = [
                "docker", "buildx", "build", "--push", "--file", str(dockerfile_path),
                "--platform", ",".join(platforms),
                "--tag", tags[0], "--tag", tags[1],
                "--label", f"org.opencontainers.image.source={source}",
                "--label", f"org.opencontainers.image.base.name={spec.upstream}",
                str(context),
            ]
            try:
                subprocess.run(command, check=True)
            except subprocess.CalledProcessError as exc:
                raise SyncError(f"Buildx failed for {spec.name} with exit code {exc.returncode}") from exc


def sync_one(
    spec: Subscription,
    registry: Registry,
    builder: Builder,
    repository: str,
    source: str,
    *,
    check_only: bool,
    force: bool,
) -> dict[str, Any]:
    tag = select_tag(registry.tags(spec.upstream), spec.tag_regex, spec.sort)
    upstream_ref = f"{spec.upstream}:{tag}"
    upstream_digest = registry.digest(upstream_ref)
    if upstream_digest is None:
        raise SyncError(f"selected upstream tag vanished: {upstream_ref}")
    available = registry.platforms(f"{spec.upstream}@{upstream_digest}")
    platforms = tuple(p for p in spec.platforms if p in available)
    if not platforms:
        raise SyncError(f"{upstream_ref} supports none of {spec.platforms}; available: {sorted(available)}")
    dockerfile = render_dockerfile(spec, tag, upstream_digest)
    input_digest = context_digest(spec.directory)
    expected = {
        "version": tag,
        "upstream_digest": upstream_digest,
        "platforms": list(platforms),
        "input_digest": input_digest,
    }
    state = read_state(spec.directory)
    current_file = spec.directory / "Dockerfile"
    file_matches = current_file.is_file() and current_file.read_text(encoding="utf-8") == dockerfile
    local_matches = state is not None and all(state.get(key) == value for key, value in expected.items()) and file_matches
    output = f"ghcr.io/{repository.lower()}/{spec.name}" if repository else None
    result = {
        "name": spec.name,
        "version": tag,
        "upstream_digest": upstream_digest,
        "platforms": list(platforms),
    }
    if output:
        result["output"] = output
    if check_only:
        result["action"] = "would_build" if force or not local_matches else "locally_current"
        return result
    if output is None:
        raise SyncError("publishing requires a GitHub repository")
    tags = (f"{output}:{tag}", f"{output}:latest")
    published_digest = state.get("image_digest") if state else None
    remote_matches = bool(published_digest and all(registry.digest(ref, missing_ok=True) == published_digest for ref in tags))
    if not force and local_matches and remote_matches:
        result["action"] = "unchanged"
        return result
    builder.build(spec, dockerfile, platforms, tags, source)
    built_digest = registry.digest(tags[0])
    latest_digest = registry.digest(tags[1])
    if not built_digest or built_digest != latest_digest:
        raise SyncError(f"published tags disagree for {spec.name}: {built_digest} / {latest_digest}")
    current_file.write_text(dockerfile, encoding="utf-8")
    write_json(spec.directory / ".sync-state.json", {**expected, "image_digest": built_digest})
    result["action"] = "published"
    result["image_digest"] = built_digest
    return result


def write_monthly_status(root: Path, results: list[dict[str, Any]], errors: list[dict[str, str]]) -> None:
    path = root / ".github" / "last-check.json"
    now = datetime.now(timezone.utc)
    snapshot = {
        "month": now.strftime("%Y-%m"),
        "images": [
            {key: result[key] for key in ("name", "version", "upstream_digest", "platforms")}
            for result in results
        ],
        "errors": errors,
    }
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if {key: previous.get(key) for key in snapshot} != snapshot:
        write_json(path, {**snapshot, "checked_at": now.isoformat(timespec="seconds")})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="query upstreams without building or changing files")
    parser.add_argument("--force", action="store_true", help="rebuild enabled images even if unchanged")
    parser.add_argument("--root", type=Path, default=ROOT)
    arguments = parser.parse_args(argv)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not arguments.check and not repository:
        parser.error("publishing requires GITHUB_REPOSITORY")
    source = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}"
    try:
        subscriptions = load_subscriptions(arguments.root)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    registry = Registry()
    builder = Builder()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for spec in subscriptions:
        if not arguments.check and not spec.enabled:
            continue
        try:
            result = sync_one(
                spec, registry, builder, repository, source,
                check_only=arguments.check, force=arguments.force,
            )
            result["enabled"] = spec.enabled
            results.append(result)
            print(json.dumps(result, sort_keys=True))
        except (SyncError, OSError) as exc:
            errors.append({"name": spec.name, "error": str(exc)})
            print(f"error: {spec.name}: {exc}", file=sys.stderr)
    if not arguments.check:
        try:
            write_monthly_status(arguments.root, results, errors)
        except (OSError, ValueError) as exc:
            print(f"error: cannot write monthly status: {exc}", file=sys.stderr)
            return 1
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
