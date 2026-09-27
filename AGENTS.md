# Project data locality

- Keep all files derived from a user's video inside `<video filename>.voxlate` beside that video. This includes experimental audio/video clips, transcripts, prompts, logs, screenshots, reports and temporary files.
- Do not use the repository `temp` directory, application data, model directories or system temporary directory for real project content. Development scripts must accept the input video and derive the project directory from it instead of hard-coding real video paths or dialogue.
- Automated tests outside the video project directory must use synthetic fixtures, never copies of real user content.
- Original source files and the final user-facing export remain in their explicitly selected locations. Shared models, runtime dependencies and application preferences are not project content.
- Never delete or overwrite user projects as part of testing. Follow the user's explicit cleanup scope and verify absolute target boundaries before deleting.

# Public repository and release privacy

- Never publish the user's local paths, account/host identifiers, installed-tool locations, usage history, video-derived files, local settings, logs, or temporary test reports. Public documentation must use generic instructions and synthetic examples.
- Before pushing or publishing, inspect the actual tracked files, changed history, screenshots, and release archive contents; `.gitignore` alone is not sufficient. Check frozen Python archives for embedded local build paths as well.
- Keep public Git attribution on the user's chosen GitHub identity and noreply email. Do not embed credentials in source, documentation, build artifacts, or remote URLs.
- If previously published personal data is found, report it. Removing it from the newest commit does not remove old commits or release attachments. Coordinate historical cleanup explicitly before rewriting published history or replacing frozen release artifacts.
