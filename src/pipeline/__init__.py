"""
Pipeline package — strict separation of processing phases.

Each module is independently callable:

  ingestion   — IMAP fetch, dedup, thread resolution (no AI)
  analysis    — classification via rules + LLM (pure, no IMAP side effects)
  actions     — execute approved IMAP actions only
  learning    — consume decision_events, structured logging + hooks

The orchestrator (``jobs``) wires these together with resumable job tracking.
"""
