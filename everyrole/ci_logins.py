"""Where a kind of user's test login lives when an app's tests run in the team's own CI: two CI variables, named
from the kind of user, the same on GitLab, GitHub and anywhere else.

  admin        -> EVERYROLE_LOGIN_ADMIN_USERNAME, EVERYROLE_LOGIN_ADMIN_PASSWORD
  team-member  -> EVERYROLE_LOGIN_TEAM_MEMBER_USERNAME, EVERYROLE_LOGIN_TEAM_MEMBER_PASSWORD

EveryRole never has these values: the hosted app shows the names, `everyrole ci` reads the values in the CI job and
types them itself (AI models only ever see the value's name).
"""

import re

PREFIX = "EVERYROLE_LOGIN_"


def variable_stem(kind):
    """A kind of user's name as it appears in its variables: upper case, anything but letters and digits as _."""
    return re.sub(r"[^A-Z0-9]+", "_", str(kind).upper()).strip("_")


def variables(kind):
    """{"username": ..., "password": ...}: the CI variables that hold this kind of user's login."""
    stem = PREFIX + variable_stem(kind)
    return {"username": f"{stem}_USERNAME", "password": f"{stem}_PASSWORD"}
