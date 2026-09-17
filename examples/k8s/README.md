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
