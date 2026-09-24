# Concurrent FETCH experiment status

The run under `results/l4s/qemu-fetch-concurrency-20260910T071046Z` is a
method failure and must not be used as scientific evidence. It is retained as
diagnostic scratch data only; no QEMU rerun was performed while finalizing the
harness.

With 100 MB offered over a symmetric 20 Mbit/s bottleneck and an 80 second
cutoff, none of the six cases completed. The `N=1`, `10`, `100`, and `1000`
clients received roughly 83.6--86.4 MB. At `N=10000`, 8,392 FETCHes completed
and 83.92 MB arrived. At `N=100000`, the server received and accepted all
100,000 requests, but the client received no payload.

The 100,000-request case is not evidence of a network capacity limit. Its last
transport samples show 4,194,397 queued stream bytes while picoquic's
cumulative sent-data counter remained at 28 bytes; the server-to-client link
counter was only 32,874 bytes. Inspection identified the unbounded
`GAsyncQueue` drain in `src/loop.c` as the likely cause: dispatch continues
processing queued application events and does not return to QUIC network and
timer work while producers keep the queue non-empty.

That event-loop starvation is intentionally unresolved on this branch. A new
experiment should be run only after queue dispatch is bounded and yields back
to the QUIC loop. Until then, the current concurrency series should remain
classified as a harness/method failure rather than a transport result.
