# Live4s Prague/DualPI2 fallback preparation

Goal: stage a reproducible fallback kernel for native experiments on
root@live4s.arman-babaei.ir while nested KVM support is being arranged.

Architecture: use the maintained upstream L4S 6.12 kernel build, pinned by
release commit and asset digest. Validate VMware
storage/network, LVM/ext4 and experiment feature support against the remote host.
Stage verified artifacts and a target-specific install/initramfs/boot/rollback
procedure. Do not reboot, install a kernel, or change GRUB during preparation.

User explicitly approved a newer kernel. Select upstream L4S 6.12; record
changed kernel semantics as a new profile, not pooled with old 5.15 results.
The initial read-only 5.15 guest inventory was completed before this steering.

- [x] Inventory running remote OS, boot chain, storage/NIC modules and free space.
- [x] Download and verify upstream 6.12 build artifacts, config, modules, tc and
      provenance. Inspect module vermagic, feature/config coverage.
- [x] Stage checksummed artifacts remotely; validate archive layout and required
      VMware/root-filesystem modules; generate an initramfs staged outside /boot
      if possible. Prepare explicit install/one-time-boot/rollback instructions.
- [x] Record what was verified and any boot blockers. Keep 6.8 default untouched.

Artifacts: results/live4s-kernel-prep-20261119 locally; /opt/live4s/kernel-prep/
remotely. The ready artifact is not evidence of a successful target boot.

Completed: official 6.12.54-0b264c55b-l4steam-117 image/headers staged under
/opt/live4s/kernel-prep/l4s-6.12. Upstream asset digest matched. Target-specific
initramfs built in a private mount namespace with VMware/LVM drivers. Module
vermagic/config checked; private patched tc+libbpf validated. Installation helper
dry-run passed, with a pinned 6.8 default and one-time test instructions. All
original boot hashes verified unchanged. No kernel installed/rebooted, no native
experiment runtime validated. Local evidence: results/live4s-kernel-prep-20261119.
