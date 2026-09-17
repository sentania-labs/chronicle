# Chronicle reference manifests

These are a reference, not the lab's deployment. lab-deployment owns the
private instance and its own values; nothing here is applied by this repo's
CI or by any automation in this repository.

They follow spec section 14: one Deployment carrying the `api`, `builder`,
and `preview` containers in a single pod, a PVC for `data` (persistent,
mounted into `api` and `builder`, never into `preview`: tokens and the
instance key have no reason to be reachable from the container that only
serves static files), a PVC shared by `preview` and `site` across all
three containers (may be persistent for speed but is disposable), a Secret
for the instance key, and an internal Ingress that routes the root path to
`api` and the preview path to `preview`.

`site` and `preview` are mounted into all three containers precisely
because they must be the same storage everywhere: the api's digest writes
`site`, the builder reads that `site` and writes `preview`, and the
preview server only ever reads `preview`. Mounting `site` into only two of
the three (an earlier version of this file did, before C3's builder
actually read it) means the api's digest and the builder's clone silently
diverge.

Files:

- `deployment.yaml`: the pod, three containers, volume mounts, and the
  builder's heartbeat-file liveness probe (it has no port to probe).
- `pvc-data.yaml`: the `data` PVC (submissions, drafts, versions, images,
  state).
- `pvc-preview-site.yaml`: the `preview` and `site` PVC, shared, disposable.
- `secret-instance-key.yaml`: placeholder Secret for the instance key that
  encrypts credentials at rest. Never commit a real value here.
- `service.yaml`: fronts the pod's api and preview ports for the Ingress.
- `ingress.yaml`: internal-only Ingress, root path to `api`, `/preview` to
  `preview`, with no path rewrite: the preview server itself matches on the
  literal `/preview/` prefix because that is the prefix Hugo's own
  `--baseURL` bakes into a build's absolute links.

Storage class, ingress class, registry, and resource sizing are cluster
specifics the spec deliberately leaves undecided (section 18). Set them for
your own cluster before applying anything here.

`deployment.yaml`'s pod-level `securityContext.fsGroup: 1000` is required,
not a hardening extra: a fresh PVC is provisioned with root ownership by
most CSI drivers regardless of what the container image sets, unlike a
Docker named volume, which inherits the image's ownership at the mount
path (see the Dockerfile). `fsGroup` is what makes a fresh `chronicle-data`
or `chronicle-preview-site` PVC group-writable by uid 1000 on first mount;
without it, the api and builder containers get the same
`mkdir: permission denied` a fresh Docker volume produces without the
Dockerfile's `chown` (see the C3 fresh-volume fix in this repo's history).

The builder's liveness probe runs `chronicle-builder --healthcheck`
(chronicle/builder/main.py), the same heartbeat-staleness check the
Dockerfile's own `HEALTHCHECK` runs, not an inline script duplicated a
third time in this file.

## Adapting this for a real instance (lab-deployment's job)

This reference is not applied by anything in this repository. Standing up
a real instance from it means, at minimum:

- **Image tags.** `REPLACE_ME/chronicle/<api|builder|preview>:REPLACE_ME`
  becomes `ghcr.io/sentania-labs/chronicle-<api|builder|preview>:vX.Y.Z`,
  the tag a release actually published (CONTRIBUTING.md's release
  procedure), never `main` or a branch build: ADR 016 and the release CI
  only sign and SBOM a tagged build, so an untagged image has neither.
- **Hostname.** `chronicle.REPLACE_ME.internal` in `deployment.yaml`
  (`CHRONICLE_EXTERNAL_URL`) and both `ingress.yaml` rules must agree,
  since a mismatch between what the api stamps into a preview URL and
  what the Ingress actually routes breaks every preview link.
- **Storage class.** Both PVCs leave `storageClassName` unset on purpose
  (spec section 18 leaves this a cluster specific); set it to whatever
  class backs durable and, ideally, fast local storage for `preview-site`.
- **The Secret.** `secret-instance-key.yaml` ships a placeholder value.
  Generate a real 32-byte key per instance (`openssl rand -base64 32` or
  equivalent) and manage it the way the rest of the cluster's secrets are
  managed, never by committing the real value anywhere this repository's
  history can see it.
- **The GitHub App bootstrap.** Nothing here creates the App: that is a
  one-time admin flow (spec section 10) run against the live instance
  after it starts, from `/admin/claim` through the manifest flow to
  choosing the repo. `ingressClassName` needs to resolve before that flow
  can complete, since the manifest flow's redirect comes back through the
  Ingress.
