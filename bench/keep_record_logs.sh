#!/usr/bin/env bash
# Derived from paper-io-core bench/keep_record_logs.sh (itself from paper-one-port bench/keep_record_logs.sh
# at commit 1f27ea8), same author (Apache-2.0).
# Changed: the pack is made in a staging folder and moved into place whole, so a pack that fails leaves nothing
# behind that would block the record's rerun; the output line is "ARCHIVE SHA256" and the path may hold a space.
#
# keep_record_logs.sh check NAME
# keep_record_logs.sh pack NAME [SRC...]
#
# The logs of the sanitizer record NAME, kept as the Papers repo's rule D5 asks: in
# $RECORDS_LOGS/NAME/ (default ~/lab/records-logs, never /tmp), packed into
# $RECORDS_LOGS/NAME.tar.gz, whose sha256 goes to NAME.tar.gz.sha256 and into the record.
#
#   check  refuses (exit 2) if NAME's directory or archive exists: record logs are never
#          overwritten, so a record is never made again in silence over an earlier one.
#   pack   copies each SRC file or directory into a staging folder .partial-<pid>/NAME/ (none: an empty one),
#          writes NAME/SHA256SUMS (every file in it), packs NAME/ into NAME.tar.gz.partial, then moves the
#          folder to NAME/ and the archive to NAME.tar.gz, writes NAME.tar.gz.sha256, and prints the archive's
#          path and sha256 on one line, the sha256 last. Any failure removes the staging folder and the partial
#          archive and exits non-zero with NAME/ and NAME.tar.gz as they were.
#
# Used by bench/sanitize_wakeloop.sh.
set -euo pipefail

mode=${1:?usage: keep_record_logs.sh check|pack NAME [SRC...]}
name=${2:?usage: keep_record_logs.sh check|pack NAME [SRC...]}
shift 2
root=${RECORDS_LOGS:-$HOME/lab/records-logs}
case "$root" in /tmp/*) echo "keep_record_logs: record logs never go to /tmp" >&2; exit 2 ;; esac
dir=$root/$name
tar=$root/$name.tar.gz

case "$mode" in
    check)
        if [ -e "$dir" ] || [ -e "$tar" ]; then
            echo "keep_record_logs: $dir or $tar exists; record logs are never overwritten" >&2
            exit 2
        fi ;;
    pack)
        [ ! -e "$tar" ] || { echo "keep_record_logs: $tar exists" >&2; exit 2; }
        [ ! -e "$dir" ] || { echo "keep_record_logs: $dir exists" >&2; exit 2; }
        mkdir -p "$root"
        stage=$root/.partial-$$
        trap 'rm -rf "$stage" "$tar.partial"' EXIT
        mkdir -p "$stage/$name"
        for src in "$@"; do
            [ -e "$src" ] && cp -a "$src" "$stage/$name/"
        done
        (cd "$stage/$name" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 -r sha256sum > SHA256SUMS)
        tar --force-local -czf "$tar.partial" -C "$stage" "$name"
        sha=$(sha256sum "$tar.partial" | cut -d' ' -f1)
        mv "$stage/$name" "$dir"
        mv "$tar.partial" "$tar"
        printf '%s  %s\n' "$sha" "$name.tar.gz" > "$tar.sha256"
        echo "$tar $sha" ;;
    *) echo "keep_record_logs: unknown mode '$mode'" >&2; exit 2 ;;
esac
