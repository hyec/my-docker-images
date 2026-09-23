import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests import Response

from sync_images import (
    Builder,
    ImageNotFound,
    Registry,
    Subscription,
    SyncError,
    context_digest,
    load_subscriptions,
    parse_platforms,
    render_dockerfile,
    select_tag,
    sync_one,
    write_monthly_status,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


class FakeRegistry:
    def __init__(self):
        self.tag_list = ["3.24.1", "3.24.2", "edge"]
        self.upstream_digest = DIGEST_A
        self.available = {"linux/amd64", "linux/arm64"}
        self.output_tags = {}

    def tags(self, _repository):
        return self.tag_list

    def digest(self, ref, *, missing_ok=False):
        if ref.startswith("docker.io/"):
            return self.upstream_digest
        if ref in self.output_tags:
            return self.output_tags[ref]
        if missing_ok:
            return None
        raise ImageNotFound(ref)

    def platforms(self, _ref):
        return self.available


class FakeBuilder(Builder):
    def __init__(self, registry):
        self.registry = registry
        self.calls = []
        self.fail = False

    def build(self, spec, dockerfile, platforms, tags, source):
        self.calls.append((spec.name, dockerfile, platforms, tags, source))
        if self.fail:
            raise SyncError("simulated build failure")
        digest = "sha256:" + f"{len(self.calls):064x}"
        for tag in tags:
            self.registry.output_tags[tag] = digest


class SyncImagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "alpine"
        self.directory.mkdir(parents=True)
        (self.directory / "subscription.yaml").write_text(
            "enabled: true\nupstream: docker.io/library/alpine\n"
            "tag_regex: '^\\d+\\.\\d+\\.\\d+$'\n"
            "platforms: [linux/amd64, linux/arm64]\n", encoding="utf-8"
        )
        (self.directory / "Dockerfile.template").write_text(
            "FROM docker.io/library/alpine:${latest}\nRUN echo ready\n", encoding="utf-8"
        )
        self.spec = load_subscriptions(self.root)[0]
        self.registry = FakeRegistry()
        self.builder = FakeBuilder(self.registry)

    def sync(self, *, check_only=False, force=False):
        return sync_one(
            self.spec, self.registry, self.builder, "Example/my-docker-images",
            "https://github.com/Example/my-docker-images",
            check_only=check_only, force=force,
        )

    def test_selects_latest_matching_semver_or_lexicographic_tag(self):
        self.assertEqual(
            select_tag(["3.9.9", "3.24.2", "edge", "3.24.1"], r"\d+\.\d+\.\d+", "semver"),
            "3.24.2",
        )
        self.assertEqual(
            select_tag(["20260101", "20260923"], r"\d{8}", "lexicographic"),
            "20260923",
        )
        with self.assertRaisesRegex(SyncError, "no upstream tag"):
            select_tag(["edge"], r"\d+\.\d+\.\d+", "semver")
        with self.assertRaisesRegex(SyncError, "not SemVer"):
            select_tag(["release-3"], r"release-\d+", "semver")

    def test_root_directories_are_images_except_hidden_infrastructure(self):
        (self.root / ".github" / "scripts").mkdir(parents=True)
        self.assertEqual([spec.name for spec in load_subscriptions(self.root)], ["alpine"])
        (self.root / "another-image").mkdir()
        with self.assertRaisesRegex(SyncError, "missing subscription.yaml"):
            load_subscriptions(self.root)

    def test_platform_intersection_ignores_attestations(self):
        manifest = {"manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "unknown", "architecture": "unknown"}},
        ]}
        self.assertEqual(parse_platforms(manifest), {"linux/amd64"})
        self.assertEqual(parse_platforms({}, {"os": "linux", "architecture": "arm64"}), {"linux/arm64"})
        self.registry.available = {"linux/amd64"}
        self.assertEqual(self.sync(check_only=True)["platforms"], ["linux/amd64"])
        self.registry.available = {"linux/s390x"}
        with self.assertRaisesRegex(SyncError, "supports none"):
            self.sync(check_only=True)

    def test_render_is_deterministic_and_pins_digest(self):
        expected = (
            "# Generated from Dockerfile.template; edit the template instead.\n"
            f"FROM docker.io/library/alpine:3.24.2@{DIGEST_A}\nRUN echo ready\n"
        )
        self.assertEqual(render_dockerfile(self.spec, "3.24.2", DIGEST_A), expected)
        self.assertEqual(render_dockerfile(self.spec, "3.24.2", DIGEST_A), expected)
        (self.directory / "Dockerfile.template").write_text("RUN echo ${latest}\n", encoding="utf-8")
        with self.assertRaisesRegex(SyncError, "must occur only in FROM"):
            render_dockerfile(self.spec, "3.24.2", DIGEST_A)

    def test_check_only_does_not_write_or_build(self):
        result = self.sync(check_only=True)
        self.assertEqual(result["action"], "would_build")
        self.assertEqual(result["output"], "ghcr.io/example/my-docker-images/alpine")
        self.assertFalse((self.directory / "Dockerfile").exists())
        self.assertFalse((self.directory / ".sync-state.json").exists())
        self.assertEqual(self.builder.calls, [])

    def test_publishes_once_then_skips_unchanged_image(self):
        self.assertEqual(self.sync()["action"], "published")
        state = json.loads((self.directory / ".sync-state.json").read_text())
        self.assertEqual(state["upstream_digest"], DIGEST_A)
        self.assertEqual(state["platforms"], ["linux/amd64", "linux/arm64"])
        self.assertEqual(self.builder.calls[0][3], (
            "ghcr.io/example/my-docker-images/alpine:3.24.2",
            "ghcr.io/example/my-docker-images/alpine:latest"
        ))
        self.assertEqual(self.sync()["action"], "unchanged")
        self.assertEqual(len(self.builder.calls), 1)

    def test_same_tag_digest_change_rebuilds(self):
        self.sync()
        self.registry.upstream_digest = DIGEST_B
        self.assertEqual(self.sync()["action"], "published")
        self.assertIn(DIGEST_B, (self.directory / "Dockerfile").read_text())
        self.assertEqual(len(self.builder.calls), 2)

    def test_context_change_or_missing_output_tag_rebuilds(self):
        self.sync()
        (self.directory / "extra.txt").write_text("new build input", encoding="utf-8")
        self.assertEqual(self.sync()["action"], "published")
        self.assertEqual(self.sync()["action"], "unchanged")
        del self.registry.output_tags["ghcr.io/example/my-docker-images/alpine:latest"]
        self.assertEqual(self.sync()["action"], "published")
        self.assertEqual(len(self.builder.calls), 3)

    def test_failed_build_does_not_change_generated_files(self):
        self.sync()
        before_dockerfile = (self.directory / "Dockerfile").read_bytes()
        before_state = (self.directory / ".sync-state.json").read_bytes()
        self.registry.upstream_digest = DIGEST_B
        self.builder.fail = True
        with self.assertRaisesRegex(SyncError, "simulated build failure"):
            self.sync()
        self.assertEqual((self.directory / "Dockerfile").read_bytes(), before_dockerfile)
        self.assertEqual((self.directory / ".sync-state.json").read_bytes(), before_state)

    def test_generated_files_do_not_change_context_fingerprint(self):
        before = context_digest(self.directory)
        (self.directory / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.directory / ".sync-state.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual(context_digest(self.directory), before)

    def test_monthly_status_changes_only_when_monthly_result_changes(self):
        result = self.sync(check_only=True)
        write_monthly_status(self.root, [result], [])
        path = self.root / ".github" / "last-check.json"
        first = path.read_bytes()
        write_monthly_status(self.root, [result], [])
        self.assertEqual(path.read_bytes(), first)
        write_monthly_status(self.root, [], [{"name": "alpine", "error": "upstream unavailable"}])
        self.assertNotEqual(path.read_bytes(), first)

    def test_missing_ghcr_package_can_be_recreated(self):
        response = Response()
        response.status_code = 404
        client = SimpleNamespace(
            prefix="https",
            do_request=Mock(return_value=response),
        )
        container = SimpleNamespace(manifest_url=lambda: "ghcr.io/v2/example/alpine/manifests/latest")
        with patch.object(Registry, "_client", return_value=(client, container)):
            self.assertIsNone(Registry().digest("ghcr.io/example/alpine:latest", missing_ok=True))
            with self.assertRaisesRegex(SyncError, "HTTP 404"):
                Registry().digest("ghcr.io/example/alpine:latest")
            client.do_request.side_effect = ValueError("Cannot respond to request for authentication.")
            self.assertIsNone(Registry().digest("ghcr.io/example/alpine:latest", missing_ok=True))
            with self.assertRaisesRegex(SyncError, "authentication"):
                Registry().digest("ghcr.io/example/alpine:latest")

    def test_oras_uses_docker_hub_registry_and_manifest_digest(self):
        manifest = {"manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "linux", "architecture": "arm64"}},
        ]}
        response = Response()
        response.status_code = 200
        response._content = json.dumps(manifest).encode()
        response.headers["Docker-Content-Digest"] = DIGEST_A
        with patch("sync_images.oras.client.OrasClient") as client_type:
            client = client_type.return_value
            client.prefix = "https"
            client.get_container.side_effect = lambda ref: SimpleNamespace(
                manifest_url=lambda: f"registry-1.docker.io/v2/library/alpine/manifests/{ref.rsplit(':', 1)[-1]}"
            )
            client.get_tags.return_value = ["3.24.2"]
            client.do_request.return_value = response
            registry = Registry()
            self.assertEqual(registry.tags("docker.io/library/alpine"), ["3.24.2"])
            self.assertEqual(registry.digest("docker.io/library/alpine:3.24.2"), DIGEST_A)
            self.assertEqual(registry.platforms(f"docker.io/library/alpine@{DIGEST_A}"),
                             {"linux/amd64", "linux/arm64"})
            client.get_container.assert_any_call("registry-1.docker.io/library/alpine")
            self.assertEqual(client_type.call_count, 1)

    def test_oras_reads_single_platform_image_config(self):
        manifest = Response()
        manifest.status_code = 200
        manifest._content = json.dumps({"config": {"digest": DIGEST_B}}).encode()
        config = Response()
        config.status_code = 200
        config._content = b'{"os":"linux","architecture":"amd64"}'
        client = SimpleNamespace(
            prefix="https",
            do_request=Mock(return_value=manifest),
            get_blob=Mock(return_value=config),
        )
        container = SimpleNamespace(manifest_url=lambda: "ghcr.io/v2/example/image/manifests/latest")
        with patch.object(Registry, "_client", return_value=(client, container)):
            self.assertEqual(Registry().platforms("ghcr.io/example/image:latest"), {"linux/amd64"})
            client.get_blob.assert_called_once_with(container, DIGEST_B)


if __name__ == "__main__":
    unittest.main()
