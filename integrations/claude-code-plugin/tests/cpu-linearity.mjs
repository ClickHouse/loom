/** Equal-byte CPU samples; the caller applies the ratio verdict. */
export function sampleCpuPair(cpuMs, smallText, largeText) {
  let n = 1;
  for (;;) {
    while (cpuMs(smallText, 8 * n) < 100) n *= 2;
    // The best of three each, interleaved, for what noise is left.
    let small = Infinity;
    let large = Infinity;
    for (let round = 0; round < 3; round++) {
      small = Math.min(small, cpuMs(smallText, 8 * n));
      large = Math.min(large, cpuMs(largeText, n));
    }
    if (small >= 100) return { n, small, large };
    // A calibration burst must not leave the sampled minimum below the clock floor.
    n *= 2;
  }
}
