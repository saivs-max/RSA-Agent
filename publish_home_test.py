#!/usr/bin/env python3
"""Directly publish Rovi's Home tab to one user, bypassing Socket Mode and the
app_home_opened event. This isolates a *publishing* problem (bad token, scope, or
view) from an *event-delivery* problem (event not subscribed, app not reinstalled,
or an old bot process still holding the Socket Mode connection).

Usage:
    python3 publish_home_test.py U0123456789

Get your member ID in Slack: your profile → ⋮ (More) → Copy member ID (starts "U").
"""
import sys

# Importing the app builds the Bolt client but does NOT start Socket Mode
# (that only runs under `if __name__ == "__main__"` in rsa_agent.py).
import rsa_agent as A


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print(__doc__)
        sys.exit(1)
    uid = sys.argv[1].strip()

    view = A.build_home_view()
    print(f"Built home view: {len(view['blocks'])} blocks. Publishing to {uid} …")

    resp = A.slack.client.views_publish(user_id=uid, view=view)
    if resp.get("ok"):
        print("✅ ok=True — published. Open Rovi's Home tab in Slack; the dashboard "
              "should now replace the 'work in progress' placeholder.")
        print("   → If this works but opening the tab normally does NOT, the issue is "
              "event delivery (see notes), not the code or the view.")
    else:
        print(f"❌ ok=False — error: {resp.get('error')}")
        print("   Common causes: 'not_authed'/'invalid_auth' (bad SLACK_BOT_TOKEN), "
              "'missing_scope' (reinstall the app), or a view/block problem.")


if __name__ == "__main__":
    main()
