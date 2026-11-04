# Thumbnail cache eviction

The thumbnail service keeps rendered thumbnails in an in-process cache in front of the
renderer. This note records how the cache evicts and why.

## Segmented LRU

We evict with a segmented LRU: a probation segment holds entries seen once, a protected
segment holds entries hit twice or more, and the protected segment is capped at 80% of the
cache. A gallery scan touches thousands of thumbnails exactly once; under a single LRU list
such a scan pushes out every hot thumbnail, while under the segmented LRU it only churns the
probation segment.

## Rejected: plain LRU

A plain LRU list is the simplest option and was rejected because one gallery scan evicts the
whole working set: the hit rate fell from 91% to 40% during scans in the replay test.

## Rejected: LFU

An LFU cache keeps a counter per entry. It was rejected because thumbnails that were popular
last month keep high counts and never leave, and the counters cost 16 bytes per entry.

## Cache size

The cache size setting `thumb_cache_bytes` defaults to 512 MiB. Raise it only together with
the container memory limit: the renderer needs 1 GiB of headroom, and a larger cache under the
same limit gets the process killed by the memory limit.
