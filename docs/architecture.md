# Architecture

The CLI owns scanning, inference, review records and verified copies. A loopback web server edits review labels in the same SQLite database. No background queue or external service is required.

An alternative server-owned job queue would keep long work attached to a browser process and add job lifecycle state. The CLI-owned design makes interruption and resumption independent of the browser.

## Data contract

`images` stores an integer id, source relative path, SHA-256, size, mtime, decoding status and optional perceptual hash. `predictions` stores image id, engine fingerprint, label, score, evidence and error. `reviews` stores independently entered labels, evidence and a human-verified flag. `copies` stores a source id, effective label, target relative path and verified SHA-256.

The effective label is the review label when present, otherwise a selected engine prediction. A review entered by an assistant must not count as human ground truth. Accuracy uses only human-verified reviews, and reports sample count and coverage.

## Safety

Source files are opened for reading only. Scans reject symbolic links and never suppress directory permission failures. State and output directories must be outside the source tree. Copy destinations use exclusive creation and content checks. Existing files are never overwritten. A changed classification produces a new copy; older output copies are reported as stale and are not silently deleted.

Inference sees decoded pixels only. File names and EXIF text are excluded. Orientation metadata may rotate pixels for display. No identity or gender inference is needed.

Model confidence is not an accuracy estimate. Generic scene labels are only candidate evidence. Weak or competing candidates remain unclassified. A generic image classifier cannot reliably establish active competition versus a ceremony or a portrait; review remains necessary.
