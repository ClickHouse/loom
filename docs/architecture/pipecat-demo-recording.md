# Pipecat demonstration recording

The optional `pipecat-demo.yml` workflow renders a timestamped execution
visualization of an authenticated native Pipecat text-memory check. The video
labels itself as a visualization rather than a screen recording. It shows
measured process events and synthetic observations from a validated post-run
assertion receipt. It does not replay API responses or type a simulated session.

## Source entry points

- [dev/pipecat_demo/record_pipecat_video.py](../../dev/pipecat_demo/record_pipecat_video.py):
  `run_native`, `validated_receipt`, `frame_bytes`, `encode_video` and `main`
  collect events, validate observations, render frames and clean owned processes.
- [dev/pipecat_demo/_pipecat_video_ci.py](../../dev/pipecat_demo/_pipecat_video_ci.py):
  `test_pipecat_text_example_recalls_and_stores_the_real_turn` writes the synthetic
  receipt after native assertions pass.
- [tests/integration/_pipecat_demo_ci.py](../../tests/integration/_pipecat_demo_ci.py):
  `test_pipecat_text_example_recalls_and_stores_the_real_turn` qualifies the native
  context and persisted messages.
- [sdk/python/examples/pipecat_text_memory.py](../../sdk/python/examples/pipecat_text_memory.py):
  `main`, `open_application_family` and `run_turn` execute the example.

## Invariants

The workload keeps the native test, example and filming wrapper pinned by hash.
It uses a real Agent and Pipecat with fake server LLM and embedding providers.
Only the exact receipt and allowlisted process summaries reach the renderer.
Raw output stays in the job log. Audio, interruption, protected restoration,
a separate CLI process and registry installation are outside this check.

## Startup diagnostics

The owned server excludes the recording client's `PYTHONPATH` target and uses
its fixture's locked dependencies. The native client retains its own target.
Before the public output directory exists, admission failures emit a fixed stage,
allowlisted error code and numeric I/O details. An owned-server exit observation
retains its unreaped PID and birth identity for cleanup.

A diagnostic reads at most the first 64 KiB of the held private server log and
exports only fixed import, validation and bind-error markers. Raw exception text,
log bytes, paths and environment values are not exported. Missing diagnostics do
not change the refusal or skip owned cleanup. Marker absence only describes the
inspected prefix; it does not establish a startup cause.

## Renderer dependencies

The hosted job installs `imageio-ffmpeg==0.6.0` and `Pillow==13.0.0` into an
isolated target using wheel hashes from `dev/pipecat_demo/renderer-requirements.txt`.
These wheels target Linux x86-64 and CPython 3.12. The recorder checks package
versions and verifies the packaged FFmpeg executable's exact hash before use.
It resolves that resource directly: no wrapper probes, executable overrides,
system fallback or runtime download select an encoder.

Pillow is MIT-CMU licensed. The imageio-ffmpeg Python wrapper is BSD-2-Clause;
its bundled Linux FFmpeg executable comes from the GPLv3 static builds described
by [its source](https://github.com/imageio/imageio-ffmpeg/blob/v0.6.0/imageio_ffmpeg/_definitions.py)
and [binary distributor](https://johnvansickle.com/ffmpeg/). The workflow executes
that dependency privately; it uploads the rendered video and synthetic receipts,
not a copied encoder or a package containing that executable.

## Timing and resource limits

Each event retains its measured monotonic timestamp. After receipt validation,
the actual reading hold lasts until at least 35 seconds from process recording
start and eight seconds after completion. A required hold beyond 55 seconds
refuses the video. The rendered timeline preserves these times and labels the
reading hold as making no new memory calls.

The renderer streams 1280 × 720 RGB frames at five frames per second, with at
most 275 frames and 32 events. It retains one frame at a time; converting a
Pillow image to bytes briefly holds both representations.
The encoder has one thread and a separate 30-second encoding deadline. This is
a bound on the renderer, separate from the video's measured playback duration.
Nonblocking pipes interleave frame writes, encoded output and discarded error
output in chunks of at most 64 KiB. Error output is capped at 64 KiB and encoded
output at 8 MiB. The encoder receives the exact frame-count playback duration
so the nonseekable WebM stream carries duration metadata. No raw encoder output
reaches the public status receipt.

The recorder registers each child before permitting cancellation, retains
PID/birth/process-group identity until reaping, and cleans adopted descendants
through its subreaper. The media probe checks a 30–60 second video without audio.
A failed native check, renderer, probe or cleanup cannot qualify a video.
Once invoked, the recorder preserves the independent native result even if its
visualization fails. Dependency installation can fail before invocation. The
recorder removes unqualified video output. A successful recording requires separate visual and privacy
inspection before use as a demonstration.
