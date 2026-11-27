# Security

**Loopback by default.** The server binds `127.0.0.1:7662`. A bind beyond loopback is refused at
boot unless a password is configured — the config file, `SIW_BIND`, and `--bind` all land in the
same check — and `set_settings` refuses to save the combination later. With a password set the
HTTP surface requires `Authorization: Basic`; the engine is started on loopback (`siw serve`
passes `-i`), and `siw doctor` warns when the engine it finds answers on every interface.

**Credentials are stored in plain text** in the config file, and there is no TLS. They are
share-the-machine-grade, not secrets-grade: protect the config file the way the machine needs
protecting.

**Cross-origin writes are rejected.** `/action` answers `403 cross_origin` to a POST whose
`Origin` or `Sec-Fetch-Site` names another site, and no response sends
`Access-Control-Allow-Origin`. The guard is against browsers, not against anything that can
reach the port directly — a non-browser client sends neither header and is unconstrained.

**`preview` and `download_url` fetch arbitrary URLs.** Pasting a URL makes this server request
it: any host, including private addresses — there is no IP blocklist, because the server runs on
your machine, which already chooses what it can reach. The preview fetch is bounded (10 s
timeout, 1 MiB read) and failures report the failure kind, not the raw upstream text.

**Downloads are verified before they are served.** The checksum of record is the caller-supplied
`sha256` when one was given, else the `.meta4`/`.sha256` sidecar when the host offers one, else
the ZIM header read back from disk; `file.verified` says which. Target file names are validated
against the OS's illegal characters, and extracting the pinned tool archive refuses absolute
paths and traversal members.

**`open_folder`** opens the corpus folder in the OS file manager — loopback-origin requests only,
and it opens nothing else.

**The supply chain is CPython's standard library.** No third-party package ships or runs, in the
product or in its tests.
