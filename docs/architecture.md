# Architecture

The CLI owns scanning, inference, review records and verified copies. A loopback web server edits review labels in the same SQLite database. No background queue or external service is required.

An alternative server-owned job queue would keep long work attached to a browser process and add job lifecycle state. The CLI-owned design makes interruption and resumption independent of the browser.

## Data contract

`images` stores an integer id, source relative path, SHA-256, size, mtime, decoding status and optional perceptual hash. `predictions` stores image id, engine fingerprint, label, score, evidence and error. `reviews` stores independently entered labels, evidence and a human-verified flag. `copies` records the current canonical copy and any tracked copies moved to an archive. Output inventory, rather than the copy ledger, determines which files exist. `OutputPlan` groups source rows by SHA-256 and retains every source id. Conflicting labels within a group produce one unclassified output and an explicit conflict record. JSON export includes this plan.

The effective label is the review label when present, otherwise a selected engine prediction. A review entered by an assistant must not count as human ground truth. Accuracy uses only human-verified reviews, and reports sample count and coverage.

## Safety

Source files are opened for reading only. Scans reject symbolic links and never suppress directory permission failures. State, output, lock and archive paths must not overlap the source. Every source hash, size and modification time is checked before output changes.

`copy` and `verify` share one SHA-256 output plan and inspect actual output files. One matching file per content and label is reused even when a different database created it. Same-content source aliases share that copy. A persistent sibling lock serializes writers across databases. A dry run creates neither the lock nor output files.

New copies use exclusive temporary files, byte checks and a hard-link publication that refuses existing destinations. Only the temporary link created by that operation is removed after publication. A leftover temporary file is never canonical, even if its hash matches. Existing output bytes are never overwritten or deleted. All desired copies must pass checks before any previous copies are archived. The effective plan is checked again before archival so changed reviews stop the operation.

Extra, damaged and wrongly placed regular files move by same-filesystem rename into a unique sibling archive. Their relative paths and bytes remain available. Filesystem reconciliation resumes after interruption without trusting stale ledger states. Verification rejects extra files, duplicate content, mixed labels and missing content, including files absent from the ledger. Only regular files named exactly `.DS_Store` are ignored. Symbolic links and special files stop reconciliation.

Inference sees decoded pixels only. File names and EXIF text are excluded. Orientation metadata may rotate pixels for display. No identity or gender inference is needed.

Model confidence is not an accuracy estimate. Generic scene labels are only candidate evidence. Weak or competing candidates remain unclassified. A generic image classifier cannot reliably establish active competition versus a ceremony or a portrait; review remains necessary.
