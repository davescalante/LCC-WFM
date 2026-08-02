BONUS_QUALIFYING = frozenset({'P', 'OT', 'MUT', 'VTO', 'P+VTO', 'V'})
BONUS_DISQUALIFYING = frozenset({'Absent', 'NCNS', 'T', 'T+VTO', 'T+I', 'I', 'LOA', 'S'})
PORTAL_ADMIN_TYPES = frozenset({'cs', 'tester', 'sms_email'})

# Statuses that zero out a day's scheduled hours (VTO, LOA, and Vacation all
# mean the agent isn't expected to work that day even though a shift exists).
SCHED_HOURS_ZEROING_STATUSES = frozenset({'VTO', 'LOA', 'V'})

# VTO-type statuses (excludes LOA, a distinct leave type) — presence of any of
# these on any day in a week raises that agent's weekly NR allowance to the
# flat cap, skipping the 12.5% ratio check for that agent-week.
VTO_TYPE_STATUSES = frozenset({'VTO', 'P+VTO', 'T+VTO'})
