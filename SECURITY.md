# Security policy and threat model

## Reporting

Report security issues privately to the project maintainers through the
repository's GitHub Security Advisory feature, or by opening an issue that
contains no exploit details and asking for a private channel. Do not attach
confidential customer drawings to public issues.

## Supported versions

Only the latest 0.3.x Windows x64 portable build is supported during the
hardening pilot. Automatic update installation is **disabled by default** in
0.3.0. A user may check for updates manually; the update is installed only
after the signed manifest, archive hash, complete file list, and startup
health check pass.

## Threat model (summary)

VECTOR PDF opens untrusted PDF files and uses native parsers (PDFium, QPDF /
pikepdf, fontTools) in-process. A malicious PDF can therefore consume CPU,
memory, or disk, and a native parser vulnerability would be in-process. The
application does not execute JavaScript, does not fetch remote resources from
PDF content, and never writes to the source PDF.

Current mitigations:

- PDF parsing and rendering happen in dedicated worker threads with the GUI
  kept responsive; PDFium calls are serialized on one worker.
- The editor refuses encrypted PDFs and real digital signatures.
- Every save is written to a sibling temporary file, fsynced, reopened and
  validated (QPDF syntax check, page geometry, edited text extraction, and
  untouched-stream comparison), and only then atomically replaced.
- The user's source file is never a valid destination. A private snapshot is
  used for editing so that OneDrive/network changes cannot destroy work.
- Update archives are constrained to the pinned GitHub repository, HTTPS
  hosts, exact asset names, size limits, safe relative paths, and a complete
  SHA-256 manifest. The manifest is verified with an Ed25519 detached
  signature before any file is trusted.
- Update activation is followed by a startup handshake. A build that cannot
  start is rolled back automatically; `--safe-mode` and
  `--reset-update` are available as recovery paths.
- Temporary PDF copies are stored under the OS temporary directory and
  cleaned on normal close; stale workspaces are removed at startup. They are
  still readable by the same user account, so use full-disk encryption for
  confidential drawings.

## Known limitations

- No sandbox, job object, low-integrity token, or hard timeout isolates the
  native PDF parsers. A hostile PDF can hang or exhaust resources. This is a
  documented residual risk, not a claim of complete mitigation.
- The local user profile can modify files under
  `%LOCALAPPDATA%\VectorPDF`. Update files are hash-checked before launch;
  an attacker who can already write to the user's profile has equivalent
  code-execution capability in that account.
- Code signing of the launcher and release archive depends on a project
  signing certificate and is performed only if the release environment
  provides it. A build without that certificate is explicitly reported as
  unsigned.
- Physical Windows 11/HiDPI acceptance on 125/150/200% scaling and 4K
  hardware is a release checklist item; automated offscreen Qt tests do not
  replace it.
