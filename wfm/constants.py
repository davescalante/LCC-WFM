# 'Holiday' = a company holiday the agent did NOT work — not their fault, so it
# qualifies (never disqualifies) the adherence bonus, same spirit as Vacation.
BONUS_QUALIFYING = frozenset({'P', 'OT', 'MUT', 'VTO', 'P+VTO', 'V', 'Holiday'})
BONUS_DISQUALIFYING = frozenset({'Absent', 'NCNS', 'T', 'T+VTO', 'T+I', 'I', 'LOA', 'S', 'Issues'})
PORTAL_ADMIN_TYPES = frozenset({'cs', 'tester', 'sms_email'})

# Statuses that zero out a day's scheduled hours (VTO, LOA, Vacation, and a
# not-worked Holiday all mean the agent isn't expected to work that day even
# though a shift exists). The nómina reads the shift directly for holiday pay,
# so zeroing here (adherence/NR accounting) doesn't affect that.
SCHED_HOURS_ZEROING_STATUSES = frozenset({'VTO', 'LOA', 'V', 'Holiday'})

# VTO-type statuses (excludes LOA, a distinct leave type) — presence of any of
# these on any day in a week raises that agent's weekly NR allowance to the
# flat cap, skipping the 12.5% ratio check for that agent-week.
VTO_TYPE_STATUSES = frozenset({'VTO', 'P+VTO', 'T+VTO'})
