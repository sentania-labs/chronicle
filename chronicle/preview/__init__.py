"""The Chronicle preview server: a static file server over the preview
volume, path-prefixed by slug, no directory listing (section 8 and 14 of the
spec). Round C0 ships the flat, non-prefixed version of this server; the
per-slug path prefix arrives with the preview builder in a later round.
"""
