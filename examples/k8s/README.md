# Chronicle reference manifests

These are a reference, not the lab's deployment. lab-deployment owns the
private instance and its own values; nothing here is applied by this repo's
CI or by any automation in this repository.

They follow spec section 14: one Deployment carrying the `api`, `builder`,
and `preview` containers in a single pod (they share the data and preview
volumes), a PVC for `data` (persistent), a PVC shared by `preview` and
`site` (may be persistent for speed but is disposable), a Secret for the
instance key, and an internal Ingress that routes the root path to `api`
and the preview path to `preview`.

Files:

- `deployment.yaml`: the pod, three containers, volume mounts.
- `pvc-data.yaml`: the `data` PVC (submissions, drafts, versions, images,
  state).
- `pvc-preview-site.yaml`: the `preview` and `site` PVC, shared, disposable.
- `secret-instance-key.yaml`: placeholder Secret for the instance key that
  encrypts credentials at rest. Never commit a real value here.
- `service.yaml`: fronts the pod's api and preview ports for the Ingress.
- `ingress.yaml`: internal-only Ingress, root path to `api`, `/preview` to
  `preview`.

Storage class, ingress class, registry, and resource sizing are cluster
specifics the spec deliberately leaves undecided (section 18). Set them for
your own cluster before applying anything here.
