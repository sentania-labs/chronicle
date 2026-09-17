# Chronicle

Chronicle is a web service for authoring a Hugo blog. You run it yourself,
alongside the blog repository it writes to.

It sits between raw material and a published post. Bring in an idea, a
half-formed note, or a pile of source material as a submission, and turn it
into a draft. The draft previews on the service itself, built with your
blog's own Hugo theme and config, so you see the real page before anyone
else does. Revise it as many times as you want; every save keeps a version,
and the feedback that led to each revision is kept alongside it, so nothing
about why a draft changed gets lost. Local history builds up the whole way,
independent of what happens in the blog repository. When a draft is ready,
approving it opens a pull request against your blog repo, carrying the
converted post and its images as one commit. Your existing deploy pipeline
takes it from there: Chronicle does not touch your site's hosting, and it
does not decide what merges. A watcher notices when the pull request
merges or closes and updates the draft's status either way; if you unpublish
later, that goes through the same pull request path in reverse.

The API is the actual surface; the web UI is one consumer of it, not a
special case. Anything a person can do by clicking through the UI, an agent
can do the same way, with the same token, against the same endpoints: file
a submission from raw material, draft it, read back the current content and
its version history, see what changed between two versions and why, and push
a revision. This is what makes "push in a bundle of ideas and sharpen them
into content" reasonable to automate. An agent working through the API is
not a separate integration bolted on afterward. It is the same lifecycle a
person walks through the UI, exercised by a different caller.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Docker with Compose v2.

Build and start all three containers (api, builder, preview) against fresh
volumes:

```bash
docker compose up -d --build
```

Claim the instance at `http://localhost:8080/admin` (the one-time code is
in `data/state/claim-code` inside the api container:
`docker compose exec api cat /data/state/claim-code`), log in with a
password of at least 12 characters, and set up a GitHub App later; for now,
give the digest a git clone to read from instead. Copy a local clone of
your Hugo blog into the api container and digest it:

```bash
docker compose cp /path/to/a/local/hugo/clone api:/tmp/blog
docker compose exec -e CHRONICLE_DIGEST_REPO_URL=/tmp/blog api chronicle digest
```

Issue yourself a token and pull a post in from the clone to try the preview
loop:

```bash
docker compose exec api chronicle token issue quickstart   # prints the token once
export TOKEN=<the token just printed>
curl -s -X POST localhost:8080/v1/drafts \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"from_post": "<a slug the digest found>"}'
curl -s -X POST localhost:8080/v1/drafts/<draft id>/actions/preview \
  -H "Authorization: Bearer $TOKEN"
```

Poll `curl -s localhost:8080/v1/runs/<run id> -H "Authorization: Bearer $TOKEN"`
until `status` is `succeeded`, then open the `preview_url` the response
carries (it already points at the preview container, `localhost:8090`).

Tear down, including the volumes, when you're done:

```bash
docker compose down -v
```

`docker-compose.yml` is for local development only; it is not how Chronicle
gets deployed. See [examples/k8s/](examples/k8s/) for reference deployment
manifests.

## Further reading

- [docs/spec/00-spec.md](docs/spec/00-spec.md): the full design spec.
- [docs/authoring-flow.md](docs/authoring-flow.md): how preview, publish,
  unpublish, merge watch, and reconciliation actually work, plus the API
  and UI surface.
- [docs/operations.md](docs/operations.md): running an instance, the data
  directory layout, tokens, the three images, and admin bootstrap.
- [docs/backup.md](docs/backup.md): the backup bundle format.
- [CONTRIBUTING.md](CONTRIBUTING.md): the contributor bar and release
  procedure.
- [examples/k8s/](examples/k8s/): reference Kubernetes manifests.

## What it does not do

Chronicle does not write your posts. Drafting and revising are things you
or an agent acting on your behalf do through the API; the service only
stores, previews, and moves what you give it.

It does not host your blog. The published site is whatever your existing
Hugo pipeline builds from the blog repository; Chronicle's own preview
container serves draft previews only, never the live site.

It does not merge its own pull requests. Approving a draft opens one;
merging it, on your blog repo, under your own review, is what actually
publishes.
