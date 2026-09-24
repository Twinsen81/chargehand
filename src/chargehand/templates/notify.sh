#!/usr/bin/env bash
# chargehand notification hook.
#
# Called once per state change with these variables set:
#
#   CHARGEHAND_ISSUE   the issue identifier
#   CHARGEHAND_STATE   blocked | done | failed | cancelled | long-running | watchdog-stopped | ...
#   CHARGEHAND_URL     the issue URL, when the tracker provides one
#   CHARGEHAND_ROUTE   the route name
#   CHARGEHAND_DETAIL  a short chargehand-generated reason
#
# There is no question text and no agent output here on purpose: this payload usually
# crosses a third-party relay. To read the question, attach to the session.
#
# Exit non-zero when a notification did not go out. The runner logs it and sends a
# session's new state again on the next tick.

set -uo pipefail

title="chargehand: ${CHARGEHAND_ISSUE:-?} ${CHARGEHAND_STATE:-?}"
body="${CHARGEHAND_DETAIL:-}${CHARGEHAND_URL:+ ${CHARGEHAND_URL}}"
status=0

# As shipped this only prints, and the runner captures what the hook prints, so nothing
# reaches you until you uncomment one of these. That is deliberate: a hook that reached a
# third-party relay by default would be a surprise, not a convenience. Each example sets
# status instead of exiting, so one method that fails does not skip the next one.
#
# Example: a macOS notification, for when you are at the machine. The values reach
# AppleScript as arguments, never as script text, so a quote in them cannot change it.
# macOS shows it as coming from Script Editor, as a banner that closes after a few
# seconds, so it also plays a sound. To keep it on screen until you close it, set Script
# Editor's notifications to persistent in System Settings.
#
#   osascript -e 'on run argv' \
#     -e 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"' \
#     -e 'end run' "${title}" "${body}" >/dev/null || status=1
#
# Example: an email, for when you are away. macOS has no mail relay configured, so
# `mail` alone sends nothing; this goes through an SMTP server with curl. Use a mailbox
# that exists only to send these alerts: any process that runs as you can read the
# password from the keychain. Store its app password once:
#
#   security add-generic-password -s chargehand-smtp -a alerts@example.com -w
#
# then send (for Gmail, the server is smtps://smtp.gmail.com:465):
#
#   sender=alerts@example.com recipient=you@example.com
#   if password="$(security find-generic-password -s chargehand-smtp -a "${sender}" -w)"; then
#     password="${password//\\/\\\\}"; password="${password//\"/\\\"}"
#     # The password reaches curl on a file descriptor, never as an argument.
#     printf 'From: %s\r\nTo: %s\r\nSubject: %s\r\nDate: %s\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n%s\r\n' \
#         "${sender}" "${recipient}" "${title}" "$(LC_ALL=C date '+%a, %d %b %Y %H:%M:%S %z')" "${body}" \
#       | curl --silent --show-error --ssl-reqd --max-time 20 \
#           --url smtps://smtp.example.com:465 \
#           --mail-from "${sender}" --mail-rcpt "${recipient}" \
#           --config <(printf 'user = "%s:%s"\n' "${sender}" "${password}") \
#           --upload-file - || status=1
#   else
#     status=1
#   fi
#
# Example: a push service.
#
#   curl -fsS --max-time 20 -H "Title: ${title}" --data-raw "${body}" \
#     https://ntfy.sh/your-private-topic >/dev/null || status=1

printf '%s - %s\n' "${title}" "${body}"
exit "${status}"
