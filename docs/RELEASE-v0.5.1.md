# Choir Assistant v0.5.1

Retains all [v0.5.0 manual correction and synchronization capabilities](RELEASE-v0.5.0.md).
Production music, rendering, server and synchronization code are unchanged.

The first Linux CI run found one platform-dependent transfer test: an artificial
one-second socket timeout combined with tiny kernel TCP buffers interrupted the
fixture despite the production chunked writer. The regression now models the
whole-write timeout deterministically: a legacy single write fails, while bounded
writes preserve the complete body and hash beyond that timeout. A separate real
HTTP test checks the complete body with normal socket buffers.

This patch updates package metadata and tests. It requires no website migration,
musical rebuild, publication or service restart. Existing online revisions and
rollback history remain independent of plugin installation.
