"""Loom's reference extractor: source bytes into the extraction artifact, served
over HTTP by `service` and run one extraction per child process by `child`.
`source` detects and decodes, one module per format parses. Nothing here
imports the Loom server, and the service holds no Loom or database credential.
"""

from loom_extractor.source import (
    KNOWN_FORMATS,
    PARSED_FORMATS,
    ExtractionError,
    Options,
    decode,
    detect_format,
    extract,
)

__all__ = [
    "KNOWN_FORMATS",
    "PARSED_FORMATS",
    "ExtractionError",
    "Options",
    "decode",
    "detect_format",
    "extract",
]
