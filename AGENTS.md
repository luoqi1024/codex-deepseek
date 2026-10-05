# Contributor instructions

Default to doing the work in Codex. Run DeepSeek model tasks only when the user explicitly asks to use DeepSeek or invokes `$deepseek-delegate`. Discussing this project, MCP or quota does not grant execution permission. Read-only connection checks do not run a model.

Keep planning, communication and final verification in Codex. Delegated exchanges must be visible in the current chat. Use compact incremental status and results; do not import entire traces. The installed skill documents fixed acceptance criteria, two delegation rounds, user takeover and recovery rules.

Do not publish task histories, `runs/`, `generated/`, local capability tokens, account credentials, backups or extracted third-party runtime sources. Use public extension interfaces; never patch client binaries or bypass usage limits. Tests must use temporary data/mocks and must not invoke a real model or alter the contributor's profiles.
