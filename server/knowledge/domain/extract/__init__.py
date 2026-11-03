"""The extraction contract as the server reads it: an extractor service parses
a source into an `Artifact` (`artifact`), `validate.parse_artifact` reads its
response as untrusted input, `server.core.document_formats` names the format a
source is sent as, and `structural` cuts chunks from the blocks. The server
parses no original itself (docs/architecture/document-extraction.md).
"""

from server.core.document_formats import KNOWN_FORMATS, format_of
from server.knowledge.domain.extract.artifact import SCHEMA_VERSION, Artifact
from server.knowledge.domain.extract.validate import InvalidArtifact, parse_artifact

__all__ = [
    "KNOWN_FORMATS",
    "SCHEMA_VERSION",
    "Artifact",
    "InvalidArtifact",
    "format_of",
    "parse_artifact",
]
