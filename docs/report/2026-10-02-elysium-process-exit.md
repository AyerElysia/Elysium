# 2026-10-02 Elysium process exit diagnosis

## Evidence

- The latest Elysium log ended at 2026-10-02 18:46:37.653.
- Successful NapCat sends were still present immediately before that timestamp.
- At the diagnostic time, no Elysium main.py or tmux process was present.
- Ports 18000 and 8087 were not listening; only New API port 3000 remained.
- systemd reported ActiveState=inactive, SubState=dead, MainPID=0.

## Conclusion

The direct cause of missing replies is that the Elysium main process has stopped. This is not evidence of a 6.1-sol model failure. The non-stream model configuration and empty-tool-call fix on disk will load only after the user manually starts Elysium again.

## Boundary

This was a read-only diagnosis. No restart was performed; after manual startup, verify the process, port 8087, and an actual QQ reply.
