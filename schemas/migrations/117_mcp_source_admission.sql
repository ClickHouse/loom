-- Code admission barrier: every live replica must understand selected MCP reads
-- and their per-document proof before any connection or source is admitted.
-- This migration changes no stored content or schema object.
SELECT 1;
