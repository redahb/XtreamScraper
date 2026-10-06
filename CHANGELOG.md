## Fixed in version 2026.10.06.1

### Library and sync
- Fixed an issue where certain provider/API errors could be mistaken for an empty library. This could incorrectly mark valid movies or series as missing and eventually quarantine them.
- Fixed recovery of previously quarantined movies and episodes. Restored items now correctly return to an active state, preventing later cleanup from leaving orphaned `.strm` files behind.
- Fixed a sync failure caused by unusual numeric-looking provider IDs, such as superscript or circled digits. A single malformed ID can no longer stop the entire sync.
- Fixed filename sanitization that could leave unwanted double spaces after removing invalid control characters.
- Fixed sidecar detection for certain Unicode filenames, preventing files belonging to one title from accidentally being associated with another.
- Fixed failed case-only file renames potentially leaving files hidden under a temporary `.renaming` filename.
- Fixed file permissions for newly generated `.strm`, `.nfo`, and artwork files on Linux, macOS, and Docker. Files now respect the system umask instead of always being created as owner-only, improving compatibility when the media server runs as a different user.

### NFO and media information
- Fixed possible data loss when updating non-UTF-8 NFO files. Declared text encodings are now respected; files that cannot safely be decoded are left untouched instead of being rewritten with replacement characters.
- NFO comments and other content before the main XML element are now preserved when the file is updated.
- Manual metadata confirmation now preserves the NFO's default unique ID marker.
- Scraped text containing invalid XML control characters can no longer make an NFO unreadable.
- Fixed URL-only/self-closing NFO files becoming unreadable after an update.
- Improved handling of malformed or extreme media information, including invalid bitrates, aspect ratios, numeric values and unexpected ffprobe data. These values are now ignored where appropriate instead of causing the item to fail.

### Metadata scraping
- Fixed unnecessary repeated metadata scraping. Items, seasons and episodes that do not need updating are now skipped correctly, reducing redundant API requests and repeated searches for previously unmatched titles.
- Fixed dead/stale metadata IDs being retried simply because their letter casing differed.
- Malformed responses from a metadata plugin's connection test now produce a normal test failure instead of an internal server error.
- Improved artwork language detection for oddly formatted language-region values.
- Concurrent scraper status updates no longer overwrite each other, preventing status information such as quota exhaustion from being lost.

### Artwork
- Fixed failed artwork downloads leaving an unowned/corrupt image behind that could prevent a later retry.
- Artwork reconciliation continues reporting errors correctly even when more than 500 errors occur.

### Jobs and application reliability
- Shutdown now applies one overall timeout to all running jobs instead of potentially waiting the full timeout once for every job.
- Job error totals are no longer capped at 500 errors per provider.
- Jobs that fail unexpectedly outside the normal provider-processing loop are now marked as failed or cancelled instead of remaining stuck as "running".
- Improved database transaction recovery so a failed database operation does not hide the original error or leave the connection unusable for subsequent work.
- Invalid provider URLs now return a normal validation error instead of an HTTP 500 error when creating, editing or testing a provider.
- A failed WebUI shutdown attempt no longer leaves the application permanently stuck in a "shutting down" state; shutdown can be retried.
- Fixed invalid WebUI addresses when binding to IPv6 hosts. `--open` now also opens a valid IPv6 URL.

### TMDB plugin
- Temporary failures while loading TMDB configuration are no longer interpreted as "not found". Existing and manually configured metadata bindings are therefore preserved during these failures.
- Improved handling of unexpected or incorrectly typed fields returned by TMDB so malformed metadata does not cause the entire item to fail.
- Changed or malformed TMDB search responses are now reported as provider-format errors rather than incorrectly appearing as "no results".

### OMDb plugin
- A plain HTTP 404 from OMDb is no longer automatically interpreted as "title not found", preventing existing metadata bindings from being incorrectly discarded because of proxy, server or configuration errors.
- OMDb error messages containing generic phrases such as "API key not found" are no longer mistaken for "movie not found".
- The Poster API connection test now reports quota, authentication, network and other failures more accurately instead of blaming every failure on an invalid API key.
- Improved handling of changed search responses, unusual numeric values and incorrectly typed ratings/episode data so malformed responses do not unnecessarily fail an item.