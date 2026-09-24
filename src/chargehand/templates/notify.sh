#!/usr/bin/env bash
# chargehand notification hook.
#
# Called once per state change with these variables set:
#
#   CHARGEHAND_ISSUE   the issue identifier
#   CHARGEHAND_STATE   running | blocked | done | failed | cancelled | long-running | ...
#   CHARGEHAND_URL     the issue URL, when the tracker provides one
#   CHARGEHAND_ROUTE   the route name
#   CHARGEHAND_DETAIL  a short chargehand-generated reason
#
# There is no question text and no agent output here on purpose: this payload usually
# crosses a third-party relay. To read the question, attach to the session.

set -euo pipefail

title="chargehand: ${CHARGEHAND_ISSUE} ${CHARGEHAND_STATE}"
body="${CHARGEHAND_DETAIL:-}${CHARGEHAND_URL:+ ${CHARGEHAND_URL}}"

# As shipped this only prints, and the runner captures what the hook prints, so nothing
# reaches you until you uncomment one of these. That is deliberate: a hook that reached a
# third-party relay by default would be a surprise, not a convenience.
#
# Example: a push service.
#   curl -fsS -H "Title: ${title}" -d "${body}" https://ntfy.sh/your-private-topic
#
# Example: a local notification while you are at the machine.
#   osascript -e "display notification \"${body}\" with title \"${title}\""

printf '%s - %s\n' "${title}" "${body}"
