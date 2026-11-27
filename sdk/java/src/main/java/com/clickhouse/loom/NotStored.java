package com.clickhouse.loom;

/** What a chat memory does when {@code conversation.append} answers {@code stored: false}. */
public enum NotStored {
  /**
   * Throw {@link LoomException} (code {@code LOGGING_DISABLED} or {@code DELETED}): a memory that
   * silently keeps nothing loses the conversation on its next read.
   */
  FAIL,
  /** Carry on without the turn: best-effort logging. */
  IGNORE
}
