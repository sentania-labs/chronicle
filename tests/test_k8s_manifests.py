"""examples/k8s/ YAML validity: parses, and every object carries the
required keys for its kind. kubectl --dry-run=client needs a live API
server to download its openapi schema even in client mode, which CI and
this sandbox do not have, so this is the schema check the task's own
fallback describes instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

K8S_DIR = Path(__file__).resolve().parent.parent / "examples" / "k8s"

REQUIRED_TOP_LEVEL = ("apiVersion", "kind", "metadata")


def _documents() -> list[dict]:
    docs: list[dict] = []
    for path in sorted(K8S_DIR.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc is not None:
                docs.append(doc)
    return docs


def test_every_manifest_file_parses_as_yaml() -> None:
    files = sorted(K8S_DIR.glob("*.yaml"))
    assert len(files) >= 6
    for path in files:
        list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


def _doc_id(doc: dict) -> str:
    return f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"


@pytest.mark.parametrize("doc", _documents(), ids=_doc_id)
def test_every_object_has_required_top_level_keys(doc: dict) -> None:
    for key in REQUIRED_TOP_LEVEL:
        assert key in doc, f"{doc.get('kind')} is missing {key!r}"
    assert "name" in doc["metadata"]


def test_deployment_has_three_containers_with_security_context_and_probes() -> None:
    docs = [d for d in _documents() if d["kind"] == "Deployment"]
    assert len(docs) == 1
    deployment = docs[0]
    pod_spec = deployment["spec"]["template"]["spec"]

    assert pod_spec["securityContext"]["fsGroup"] == 1000
    assert pod_spec["securityContext"]["runAsUser"] == 1000
    assert pod_spec["securityContext"]["runAsNonRoot"] is True

    containers = {c["name"]: c for c in pod_spec["containers"]}
    assert set(containers) == {"api", "builder", "preview"}
    for name, container in containers.items():
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert "resources" in container, f"{name} has no resource requests"
        assert "requests" in container["resources"]
        assert "livenessProbe" in container, f"{name} has no liveness probe"

    # api and preview probe over HTTP; builder has no port, so its liveness
    # is the heartbeat-file healthcheck subcommand, not an inline script.
    assert "readinessProbe" in containers["api"]
    assert "readinessProbe" in containers["preview"]
    assert containers["builder"]["livenessProbe"]["exec"]["command"] == [
        "chronicle-builder",
        "--healthcheck",
    ]


def test_pvcs_declare_storage_requests_with_no_hardcoded_storage_class() -> None:
    pvcs = [d for d in _documents() if d["kind"] == "PersistentVolumeClaim"]
    assert len(pvcs) == 2
    for pvc in pvcs:
        assert pvc["spec"]["resources"]["requests"]["storage"]
        assert "storageClassName" not in pvc["spec"]


def test_secret_never_carries_a_real_looking_key() -> None:
    secrets = [d for d in _documents() if d["kind"] == "Secret"]
    assert len(secrets) == 1
    value = secrets[0]["stringData"]["CHRONICLE_INSTANCE_KEY"]
    assert "REPLACE_ME" in value


def test_ingress_routes_root_to_api_and_preview_path_to_preview() -> None:
    ingresses = [d for d in _documents() if d["kind"] == "Ingress"]
    assert len(ingresses) == 2
    by_service = {}
    for ingress in ingresses:
        for rule in ingress["spec"]["rules"]:
            for path in rule["http"]["paths"]:
                by_service[path["path"]] = path["backend"]["service"]["port"]["number"]
    assert by_service["/"] == 8080
    assert by_service["/preview"] == 8090
