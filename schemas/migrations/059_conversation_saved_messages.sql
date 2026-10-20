-- A chat message a user explicitly saved for search (conversation.save_message).
--
-- The flag lives on both the transcript row and its embedding row: the saved
-- search ranks the embedding rows and its substring fallback reads the
-- transcript rows. Saving a turn the transcript already holds sets the flag in
-- place, so the turn keeps its original run_id and the namespace's transcript
-- logging switch is unaffected.
ALTER TABLE loom.conversations
    ADD COLUMN IF NOT EXISTS `is_saved` Bool DEFAULT false;
ALTER TABLE loom.conversation_embeddings
    ADD COLUMN IF NOT EXISTS `is_saved` Bool DEFAULT false;
