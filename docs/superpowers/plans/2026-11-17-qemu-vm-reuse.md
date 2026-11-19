# QEMU VM reuse implementation plan

**Goal:** Reuse one VM across suite QEMU cases while reliably isolating cases, including interruption.

**Architecture:** The suite owns one lazy VM session outside per-attempt memory scopes. The existing QEMU launcher prepares and stages that VM once and publishes local connection metadata without credentials. Case launchers use it sequentially, stage case inputs, run fresh guest processes/topology, preserve evidence, and reset before releasing the case lock. Guest process ownership and signal handling support independent cleanup after forced exits. A reset failure stops the suite.

**Scope:** Existing topology and configurable rates/delays/controllers; no resumed experiment batch, unrelated cleanup, or ideal changes. Existing per-invocation VM mode remains available. Work in the current checkout to preserve its in-progress changes.

- [x] Add failing tests for process ownership, termination cleanup and dirty reset rejection.
- [x] Implement guest ownership records, process-group teardown and independent reset verification.
- [x] Add failing tests for session ownership, per-case reset and suite interruption.
- [x] Implement lazy suite VM ownership and per-case connection reuse.
- [x] Run focused checks and make cpu-check, preserving prior unrelated changes.
- [x] Run consecutive live cases and interruption/recovery in one VM; record evidence under results/.

Implementation notes:
- Mininet's `mnexec -d` already creates the child session; use it directly and wait for its group identity before registering ownership. A second detach caused an early live smoke failure and was removed.
- Suite-level reset shares the case lock, since the case launcher may still be unwinding after its host parent exits.
- Case input deletion runs with guest root privileges because the guest Makefile extracts scene data as root.
- Independent review found and regression-tested surviving QEMU descendants after a hard-killed owner; close now reaps the recorded process group even after the owner exits.
- Reset evidence is retrieved on success and failure before the VM is discarded.
