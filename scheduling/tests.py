from datetime import date, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from adherence.models import AdherenceRecord, Coding
from .models import (
    Agent, AgentRequest, OvertimeShift,
    OpenOTShift, OTShiftClaimRequest, OTCancellationRequest,
)


def _make_agent(username, role='admin', role_type='supervisor', supervisor=None,
                official=False):
    user = User.objects.create_user(username=username, password='pw')
    return Agent.objects.create(
        user=user, role=role, role_type=role_type, agent_name=username.title(),
        supervisor=supervisor, is_official_admin=official,
    )


class StaffRequestTests(TestCase):
    def setUp(self):
        self.boss = _make_agent('boss', role_type='supervisor')
        self.other_sup = _make_agent('othersup', role_type='supervisor')
        self.coordinator = _make_agent('coord', role_type='coordinator', supervisor=self.boss)

    def _login(self, agent):
        self.client.login(username=agent.user.username, password='pw')

    def _submit_vacation(self, agent):
        self._login(agent)
        start = date.today() + timedelta(days=7)
        self.client.post(reverse('staff_my_requests'), {
            'request_type': 'vacation',
            'vacation_start': start.isoformat(),
            'vacation_end': (start + timedelta(days=1)).isoformat(),
            'notes': 'trip',
        })
        self.client.logout()
        return AgentRequest.objects.get(agent=agent)

    def test_staff_without_supervisor_cannot_submit(self):
        self._login(self.other_sup)
        resp = self.client.get(reverse('staff_my_requests'))
        self.assertContains(resp, "You need a supervisor assigned to your profile")

        resp = self.client.post(reverse('staff_my_requests'), {
            'request_type': 'vto', 'vto_date': date.today().isoformat(),
        }, follow=True)
        self.assertContains(resp, "You need a supervisor assigned to your profile")
        self.assertEqual(AgentRequest.objects.count(), 0)

    def test_staff_submission_snapshots_assigned_supervisor(self):
        ar = self._submit_vacation(self.coordinator)
        self.assertTrue(ar.is_staff_request)
        self.assertEqual(ar.assigned_supervisor, self.boss)
        self.assertEqual(ar.status, 'pending')
        self.assertFalse(ar.supervisor_read)

    def test_only_assigned_supervisor_sees_actions(self):
        ar = self._submit_vacation(self.coordinator)

        self._login(self.other_sup)
        resp = self.client.get(reverse('request_detail', kwargs={'pk': ar.pk}))
        self.assertIsNotNone(resp.context['action_block'])
        self.assertContains(resp, "can action this request")
        self.client.logout()

        self._login(self.boss)
        resp = self.client.get(reverse('request_detail', kwargs={'pk': ar.pk}))
        self.assertIsNone(resp.context['action_block'])

    def test_non_assigned_supervisor_cannot_approve(self):
        ar = self._submit_vacation(self.coordinator)
        self._login(self.other_sup)
        self.client.post(reverse('request_approve', kwargs={'pk': ar.pk}))
        ar.refresh_from_db()
        self.assertEqual(ar.status, 'pending')

    def test_assigned_supervisor_approval_auto_applies(self):
        ar = self._submit_vacation(self.coordinator)
        self._login(self.boss)
        self.client.post(reverse('request_approve', kwargs={'pk': ar.pk}))
        ar.refresh_from_db()
        self.assertEqual(ar.status, 'approved')
        self.assertFalse(ar.agent_read)  # requester gets a response badge
        self.assertEqual(
            AdherenceRecord.objects.filter(agent=self.coordinator, status='V').count(), 2
        )

    def test_self_approval_blocked(self):
        ar = self._submit_vacation(self.coordinator)
        self._login(self.coordinator)
        self.client.post(reverse('request_approve', kwargs={'pk': ar.pk}))
        ar.refresh_from_db()
        self.assertEqual(ar.status, 'pending')

    def test_self_supervisor_blocked_for_everyone(self):
        loner = _make_agent('loner', role_type='coordinator')
        loner.supervisor = loner
        loner.save()
        ar = self._submit_vacation(loner)

        for viewer in (loner, self.boss):
            self._login(viewer)
            self.client.post(reverse('request_approve', kwargs={'pk': ar.pk}))
            ar.refresh_from_db()
            self.assertEqual(ar.status, 'pending')
            self.client.logout()

    def test_coding_request_approval_creates_no_coding(self):
        """Coding requests are status-only: supervisors enter the exact coded
        time manually, so neither approval nor mark-as-done creates a Coding."""
        admin = _make_agent('offadmin', role_type='qa', supervisor=self.boss, official=True)
        self._login(admin)
        self.client.post(reverse('staff_my_requests'), {
            'request_type': 'coding',
            'coding_date': date.today().isoformat(),
            'coding_start_time': '09:00',
            'coding_end_time': '11:00',
        })
        self.client.logout()
        ar = AgentRequest.objects.get(agent=admin)

        self._login(self.boss)
        self.client.post(reverse('request_approve', kwargs={'pk': ar.pk}))
        ar.refresh_from_db()
        self.assertEqual(ar.status, 'approved')
        self.assertEqual(ar.auto_action_log, '')
        self.assertEqual(Coding.objects.count(), 0)

        self.client.post(reverse('request_mark_done', kwargs={'pk': ar.pk}))
        ar.refresh_from_db()
        self.assertEqual(ar.status, 'done')
        self.assertEqual(Coding.objects.count(), 0)

    def test_list_view_only_clears_badge_for_assigned_supervisor(self):
        ar = self._submit_vacation(self.coordinator)

        self._login(self.other_sup)
        self.client.get(reverse('requests_list'))
        ar.refresh_from_db()
        self.assertFalse(ar.supervisor_read)
        self.client.logout()

        self._login(self.boss)
        resp = self.client.get(reverse('requests_list'))
        self.assertEqual(resp.wsgi_request.supervisor_request_badge, 1)
        ar.refresh_from_db()
        self.assertTrue(ar.supervisor_read)

    def test_agent_request_flow_unchanged(self):
        agent = _make_agent('agent1', role='agent', role_type='regular_agent',
                            supervisor=self.boss)
        self._login(agent)
        self.client.post(reverse('agent_my_requests'), {
            'request_type': 'vto', 'vto_date': date.today().isoformat(),
        })
        self.client.logout()
        ar = AgentRequest.objects.get(agent=agent)
        self.assertFalse(ar.is_staff_request)
        self.assertIsNone(ar.assigned_supervisor)

        # Any staff user (not just the profile supervisor) can approve
        self._login(self.other_sup)
        self.client.post(reverse('request_approve', kwargs={'pk': ar.pk}))
        ar.refresh_from_db()
        self.assertEqual(ar.status, 'approved')
        self.assertTrue(
            AdherenceRecord.objects.filter(agent=agent, status='VTO').exists()
        )


class OpenOTShiftTests(TestCase):
    def setUp(self):
        self.sup = _make_agent('sup', role_type='supervisor')
        self.sup2 = _make_agent('sup2', role_type='supervisor')
        self.coord = _make_agent('coord2', role_type='coordinator')
        self.qa = _make_agent('qa1', role_type='qa')
        self.agent = _make_agent('agent2', role='agent', role_type='regular_agent',
                                 supervisor=self.sup)
        self.tomorrow = date.today() + timedelta(days=1)

    def _login(self, agent):
        self.client.login(username=agent.user.username, password='pw')

    def _post_open(self, poster=None, count=1, incentive='power_hour'):
        self._login(poster or self.sup)
        self.client.post(reverse('open_ot_create'), {
            'date': self.tomorrow.isoformat(),
            'start_time': '13:00',
            'end_time': '14:00',
            'incentive_type': incentive,
            'notes': 'Need coverage for the 5pm spike',
            'count': str(count),
        })
        self.client.logout()
        return list(OpenOTShift.objects.order_by('pk'))

    def _claim(self, posting, requester):
        self._login(requester)
        self.client.post(reverse('open_ot_claim', kwargs={'pk': posting.pk}))
        self.client.logout()
        return OTShiftClaimRequest.objects.filter(open_shift=posting, requester=requester).latest('pk')

    # ── Part 1: posting ──────────────────────────────────────────────

    def test_supervisor_posts_multiple_identical_open_shifts(self):
        postings = self._post_open(count=3)
        self.assertEqual(len(postings), 3)
        for p in postings:
            self.assertEqual(p.status, 'open')
            self.assertEqual(p.incentive_type, 'power_hour')
            self.assertEqual((p.date, p.start_time, p.end_time),
                             (self.tomorrow, time(13, 0), time(14, 0)))
        # Open postings are NOT OvertimeShift rows → never counted as coverage
        self.assertEqual(OvertimeShift.objects.count(), 0)

    def test_super_admin_and_superuser_can_post_and_approve(self):
        boss = _make_agent('boss2', role_type='qa')
        boss.is_super_admin = True
        boss.save()
        postings = self._post_open(poster=boss)
        self.assertEqual(len(postings), 1)
        claim = self._claim(postings[0], self.agent)
        self._login(boss)
        self.client.post(reverse('ot_claim_approve', kwargs={'pk': claim.pk}))
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'approved')
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.wsgi_request.ot_request_badge, 0)  # cleared after actioning
        self.client.logout()

        # Django superuser with no Agent profile can approve too
        User.objects.create_superuser('root', 'root@example.com', 'pw')
        posting2 = self._post_open()[0]
        claim2 = self._claim(posting2, self.agent)
        self.client.login(username='root', password='pw')
        self.client.post(reverse('ot_claim_approve', kwargs={'pk': claim2.pk}))
        claim2.refresh_from_db()
        self.assertEqual(claim2.status, 'approved')

    def test_non_approver_staff_cannot_post(self):
        self._login(self.qa)
        self.client.post(reverse('open_ot_create'), {
            'date': self.tomorrow.isoformat(), 'start_time': '13:00', 'end_time': '14:00',
        })
        self.assertEqual(OpenOTShift.objects.count(), 0)

    def test_open_shift_delete_rejects_pending_claims(self):
        posting = self._post_open()[0]
        claim = self._claim(posting, self.agent)
        self._login(self.sup)
        self.client.post(reverse('open_ot_delete', kwargs={'pk': posting.pk}))
        claim.refresh_from_db()
        posting.refresh_from_db()
        self.assertEqual(claim.status, 'rejected')
        self.assertEqual(claim.rejection_reason, 'The open shift posting was removed.')
        self.assertFalse(claim.requester_read)
        self.assertEqual(posting.status, 'removed')  # soft-deleted, history kept
        # Removed postings vanish from the available list
        self._login(self.agent)
        resp = self.client.get(reverse('agent_available_ot'))
        self.assertNotContains(resp, 'Request This Shift')

    # ── Part 2: claiming and approvals ───────────────────────────────

    def test_agent_sees_and_requests_open_shift(self):
        posting = self._post_open()[0]
        self._login(self.agent)
        resp = self.client.get(reverse('agent_available_ot'))
        self.assertContains(resp, 'Request This Shift')
        self.client.post(reverse('open_ot_claim', kwargs={'pk': posting.pk}))
        claim = OTShiftClaimRequest.objects.get()
        self.assertEqual(claim.status, 'pending')
        self.assertFalse(claim.supervisor_read)
        # Duplicate request blocked
        self.client.post(reverse('open_ot_claim', kwargs={'pk': posting.pk}))
        self.assertEqual(OTShiftClaimRequest.objects.count(), 1)
        # Others' pending requests are shown
        resp = self.client.get(reverse('agent_available_ot'))
        self.assertContains(resp, 'pending approval')

    def test_approval_assigns_real_shift_and_rejects_backups(self):
        posting = self._post_open()[0]
        claim1 = self._claim(posting, self.agent)
        claim2 = self._claim(posting, self.qa)  # backup request from staff

        self._login(self.sup)
        self.client.post(reverse('ot_claim_approve', kwargs={'pk': claim1.pk}))

        shift = OvertimeShift.objects.get()
        self.assertEqual(shift.agent, self.agent)
        self.assertEqual(shift.status, 'pending')
        self.assertEqual(shift.incentive_type, 'power_hour')
        self.assertEqual(shift.base_hourly_rate, self.agent.hourly_rate)
        self.assertEqual(shift.incentivized_hours, shift.total_shift_hours())

        posting.refresh_from_db()
        self.assertEqual(posting.status, 'filled')
        self.assertEqual(posting.filled_by, self.agent)
        self.assertEqual(posting.assigned_shift, shift)

        claim2.refresh_from_db()
        self.assertEqual(claim2.status, 'rejected')
        self.assertFalse(claim2.requester_read)

        # Filled shifts disappear from the available list
        self.client.logout()
        self._login(self.agent)
        resp = self.client.get(reverse('agent_available_ot'))
        self.assertNotContains(resp, 'Request This Shift')

    def test_self_approval_of_claim_blocked(self):
        posting = self._post_open()[0]
        claim = self._claim(posting, self.coord)

        self._login(self.coord)
        self.client.post(reverse('ot_claim_approve', kwargs={'pk': claim.pk}))
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'pending')
        self.client.logout()

        self._login(self.sup2)
        self.client.post(reverse('ot_claim_approve', kwargs={'pk': claim.pk}))
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'approved')
        self.assertEqual(OvertimeShift.objects.get().agent, self.coord)

    def test_non_approver_cannot_action_claims(self):
        posting = self._post_open()[0]
        claim = self._claim(posting, self.agent)
        self._login(self.qa)
        self.client.post(reverse('ot_claim_approve', kwargs={'pk': claim.pk}))
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'pending')
        self.assertEqual(OvertimeShift.objects.count(), 0)

    def test_rejected_claim_keeps_shift_open(self):
        posting = self._post_open()[0]
        claim = self._claim(posting, self.agent)
        self._login(self.sup)
        self.client.post(reverse('ot_claim_reject', kwargs={'pk': claim.pk}),
                         {'rejection_reason': 'Covered already'})
        claim.refresh_from_db()
        posting.refresh_from_db()
        self.assertEqual(claim.status, 'rejected')
        self.assertEqual(claim.rejection_reason, 'Covered already')
        self.assertFalse(claim.requester_read)
        self.assertEqual(posting.status, 'open')

    # ── Part 3: cancellation requests ────────────────────────────────

    def _make_shift(self, owner):
        return OvertimeShift.objects.create(
            agent=owner, date=self.tomorrow, start_time=time(13, 0), end_time=time(14, 0),
        )

    def test_cancellation_request_does_not_change_shift(self):
        shift = self._make_shift(self.agent)
        self._login(self.agent)
        self.client.post(reverse('ot_cancel_request', kwargs={'pk': shift.pk}),
                         {'reason': 'Family emergency'})
        shift.refresh_from_db()
        self.assertEqual(shift.status, 'pending')  # still assigned and counted
        cr = OTCancellationRequest.objects.get()
        self.assertEqual(cr.status, 'pending')
        self.assertFalse(cr.supervisor_read)
        # Requester sees the pending state on their My OT Shifts page
        week_start = self.tomorrow - timedelta(days=self.tomorrow.weekday())
        resp = self.client.get(reverse('agent_my_ot_shifts') + f'?week_start={week_start.isoformat()}')
        self.assertContains(resp, 'Cancellation requested')

    def test_cancellation_requires_reason_and_ownership(self):
        shift = self._make_shift(self.agent)
        self._login(self.agent)
        self.client.post(reverse('ot_cancel_request', kwargs={'pk': shift.pk}), {'reason': '  '})
        self.assertEqual(OTCancellationRequest.objects.count(), 0)
        self.client.logout()
        self._login(self.qa)
        self.client.post(reverse('ot_cancel_request', kwargs={'pk': shift.pk}),
                         {'reason': 'not mine'})
        self.assertEqual(OTCancellationRequest.objects.count(), 0)

    def test_approved_cancellation_cancels_shift_with_reason(self):
        shift = self._make_shift(self.agent)
        self._login(self.agent)
        self.client.post(reverse('ot_cancel_request', kwargs={'pk': shift.pk}),
                         {'reason': 'Family emergency'})
        self.client.logout()
        cr = OTCancellationRequest.objects.get()

        self._login(self.sup)
        self.client.post(reverse('ot_cancel_approve', kwargs={'pk': cr.pk}))
        shift.refresh_from_db()
        cr.refresh_from_db()
        self.assertEqual(shift.status, 'cancelled')
        self.assertEqual(shift.cancellation_reason, 'Family emergency')
        self.assertEqual(cr.status, 'approved')
        self.assertFalse(cr.requester_read)

    def test_rejected_cancellation_keeps_shift_assigned(self):
        shift = self._make_shift(self.agent)
        self._login(self.agent)
        self.client.post(reverse('ot_cancel_request', kwargs={'pk': shift.pk}),
                         {'reason': 'Sick'})
        self.client.logout()
        cr = OTCancellationRequest.objects.get()

        self._login(self.sup)
        self.client.post(reverse('ot_cancel_reject', kwargs={'pk': cr.pk}),
                         {'review_note': 'No coverage available'})
        shift.refresh_from_db()
        cr.refresh_from_db()
        self.assertEqual(shift.status, 'pending')
        self.assertEqual(cr.status, 'rejected')
        self.assertEqual(cr.review_note, 'No coverage available')

    def test_supervisor_cannot_approve_own_cancellation(self):
        shift = self._make_shift(self.sup)
        self._login(self.sup)
        self.client.post(reverse('ot_cancel_request', kwargs={'pk': shift.pk}),
                         {'reason': 'Conflict'})
        cr = OTCancellationRequest.objects.get()
        self.client.post(reverse('ot_cancel_approve', kwargs={'pk': cr.pk}))
        shift.refresh_from_db()
        self.assertEqual(shift.status, 'pending')
        self.client.logout()

        self._login(self.sup2)
        self.client.post(reverse('ot_cancel_approve', kwargs={'pk': cr.pk}))
        shift.refresh_from_db()
        self.assertEqual(shift.status, 'cancelled')

    # ── Part 2/3: badges ─────────────────────────────────────────────

    def test_ot_badges(self):
        posting = self._post_open()[0]
        self._claim(posting, self.agent)

        # Approver sees badge; QA staff (non-approver) does not
        self._login(self.sup)
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.wsgi_request.ot_request_badge, 1)
        self.client.logout()
        self._login(self.qa)
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.wsgi_request.ot_request_badge, 0)
        self.client.logout()

        # Viewing the OT page clears the approver badge
        self._login(self.sup)
        self.client.get(reverse('overtime_list'))
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.wsgi_request.ot_request_badge, 0)

        # Rejection badges the requester until they view Available OT
        claim = OTShiftClaimRequest.objects.get()
        self.client.post(reverse('ot_claim_reject', kwargs={'pk': claim.pk}))
        self.client.logout()
        self._login(self.agent)
        resp = self.client.get(reverse('agent_my_shifts'))
        self.assertEqual(resp.wsgi_request.agent_ot_claim_badge, 1)
        self.client.get(reverse('agent_available_ot'))
        resp = self.client.get(reverse('agent_my_shifts'))
        self.assertEqual(resp.wsgi_request.agent_ot_claim_badge, 0)


class AgentListRoleTypeFilterTests(TestCase):
    """Part 1: role_type filter on the Users page, combining with the
    existing supervisor and status filters."""

    def setUp(self):
        self.sup = _make_agent('lsup', role_type='supervisor')
        self.viewer = _make_agent('lviewer', role_type='coordinator')
        self.ra1 = _make_agent('ra1', role='agent', role_type='regular_agent',
                               supervisor=self.sup)
        self.ra2 = _make_agent('ra2', role='agent', role_type='regular_agent')
        self.kt = _make_agent('kt1', role='agent', role_type='kill_team',
                              supervisor=self.sup)
        self.inactive_ra = _make_agent('ra3', role='agent', role_type='regular_agent',
                                       supervisor=self.sup)
        self.inactive_ra.status = 'inactive'
        self.inactive_ra.save()
        self.client.login(username='lviewer', password='pw')

    def _names(self, query=''):
        resp = self.client.get(reverse('agent_list') + query)
        return {a.user.username for a in resp.context['agents']}

    def test_role_type_filter_narrows(self):
        names = self._names('?role_type=kill_team')
        self.assertEqual(names, {'kt1'})

    def test_filters_combine(self):
        # role_type + supervisor + status all narrow together
        names = self._names(f'?role_type=regular_agent&supervisor={self.sup.pk}&status_filter=active')
        self.assertEqual(names, {'ra1'})
        names = self._names(f'?role_type=regular_agent&supervisor={self.sup.pk}&status_filter=inactive')
        self.assertEqual(names, {'ra3'})

    def test_invalid_role_type_ignored(self):
        names = self._names('?role_type=bogus')
        self.assertIn('ra1', names)  # unfiltered active list
        self.assertIn('kt1', names)

    def test_existing_filters_and_pagination_unchanged(self):
        resp = self.client.get(reverse('agent_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('page_obj', resp.context)
        names = self._names('?status_filter=inactive')
        self.assertEqual(names, {'ra3'})


import io

import openpyxl

from .models import EmploymentPeriod, Five9Profile

XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class AgentListExportTests(TestCase):
    """Part 2: filter-respecting Excel export with export-only columns."""

    HEADER = ['Full name', 'Role type', 'Supervisor', 'Status',
              'Primary Five9 username', 'Start date',
              'Complete years with us', 'Cell phone (formatted)']

    def setUp(self):
        self.sup = _make_agent('xsup', role_type='supervisor')
        self.viewer = _make_agent('xviewer', role_type='coordinator')
        self.client.login(username='xviewer', password='pw')
        self.today = date.today()

    def _make_full_agent(self, username, **kwargs):
        a = _make_agent(username, role='agent', role_type='regular_agent', **kwargs)
        a.user.first_name = 'Test'
        a.user.last_name = username.title()
        a.user.save()
        return a

    def _export_ws(self, query=''):
        resp = self.client.get(reverse('agent_list') + '?export=1' + query)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], XLSX_MIME)
        return openpyxl.load_workbook(io.BytesIO(resp.content)).active

    def _export_rows(self, query=''):
        ws = self._export_ws(query)
        return [list(r) for r in ws.iter_rows(values_only=True)]

    def _row_for(self, rows, full_name):
        return next(r for r in rows[1:] if r[0] == full_name)

    def test_header_and_column_order(self):
        rows = self._export_rows()
        self.assertEqual(rows[0], self.HEADER)

    def test_export_respects_all_filters(self):
        a1 = self._make_full_agent('exp1', supervisor=self.sup)
        self._make_full_agent('exp2')  # different supervisor
        kt = _make_agent('exp3', role='agent', role_type='kill_team', supervisor=self.sup)
        rows = self._export_rows(f'&supervisor={self.sup.pk}&role_type=regular_agent&status_filter=active')
        names = [r[0] for r in rows[1:]]
        self.assertEqual(names, ['Test Exp1'])

    def test_full_row_content(self):
        a = self._make_full_agent('exp4', supervisor=self.sup)
        a.phone_number = '662-369-2710'
        a.save()
        Five9Profile.objects.create(agent=a, five9_username='other.acct', is_primary=False)
        Five9Profile.objects.create(agent=a, five9_username='exp4.primary', is_primary=True)
        # Two periods — the LATEST start (rehire) must win
        EmploymentPeriod.objects.create(agent=a, start_date=self.today - timedelta(days=2000),
                                        end_date=self.today - timedelta(days=1500))
        rehire = self.today - timedelta(days=912)  # ~2.5 years ago
        EmploymentPeriod.objects.create(agent=a, start_date=rehire)

        ws = self._export_ws()
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        row = self._row_for(rows, 'Test Exp4')
        self.assertEqual(row[1], 'Regular Agent')
        self.assertEqual(row[2], str(self.sup))
        self.assertEqual(row[3], 'Active')
        self.assertEqual(row[4], 'exp4.primary')
        self.assertEqual(row[5], rehire.isoformat())
        self.assertEqual(row[6], 2)  # 2.5 years → floored to 2
        self.assertEqual(row[7], '+5216623692710')  # clean text — no tab, no sci-notation
        # The phone cell is explicitly Text-formatted so Excel never coerces it
        row_idx = rows.index(row) + 1
        self.assertEqual(ws.cell(row=row_idx, column=8).number_format, '@')

    def test_years_floor_edge_cases(self):
        # 11 months → 0 complete years
        a = self._make_full_agent('exp5')
        EmploymentPeriod.objects.create(agent=a, start_date=self.today - timedelta(days=335))
        # Exactly N years today → N
        b = self._make_full_agent('exp6')
        try:
            exact = self.today.replace(year=self.today.year - 3)
        except ValueError:  # Feb 29 edge
            exact = self.today.replace(year=self.today.year - 3, day=28)
        EmploymentPeriod.objects.create(agent=b, start_date=exact)
        # One day short of a full year → 0
        c = self._make_full_agent('exp7')
        EmploymentPeriod.objects.create(agent=c, start_date=self.today - timedelta(days=364))

        rows = self._export_rows()
        self.assertEqual(self._row_for(rows, 'Test Exp5')[6], 0)
        self.assertEqual(self._row_for(rows, 'Test Exp6')[6], 3)
        self.assertEqual(self._row_for(rows, 'Test Exp7')[6], 0)

    def test_phone_formats_all_normalize(self):
        cases = {
            'exp8': '662-369-2710',
            'exp9': '(662) 369 2710',
            'exp10': '6623692710',
            'exp11': '+52 662 369 2710',
            'exp12': '+521 662 369 2710',
        }
        for username, raw in cases.items():
            a = self._make_full_agent(username)
            a.phone_number = raw
            a.save()
        rows = self._export_rows()
        for username in cases:
            row = self._row_for(rows, f'Test {username.title()}')
            self.assertEqual(row[7], '+5216623692710', f'failed for {username}')

    def test_bare_agent_exports_blanks_without_error(self):
        self._make_full_agent('exp13')  # no phone, no periods, no five9 profiles
        row = self._row_for(self._export_rows(), 'Test Exp13')
        self.assertIsNone(row[4])   # primary five9
        self.assertIsNone(row[5])   # start date
        self.assertIsNone(row[6])   # years
        self.assertIsNone(row[7])   # phone

    def test_on_screen_table_unchanged(self):
        resp = self.client.get(reverse('agent_list'))
        self.assertNotContains(resp, 'Complete years')
        self.assertNotContains(resp, 'Five9 username')
        self.assertContains(resp, 'Export Excel')


from .models import AgentSeparation


class AgentListExportPayWindowTests(TestCase):
    """Separated agents stay in the export while their pay window is open —
    the same remove_from_adherence_date rule Finance/Adherence use."""

    def setUp(self):
        self.viewer = _make_agent('pwviewer', role_type='coordinator')
        self.client.login(username='pwviewer', password='pw')
        today = date.today()
        self.this_monday = today - timedelta(days=today.weekday())
        self.next_monday = self.this_monday + timedelta(days=7)
        self.last_monday = self.this_monday - timedelta(days=7)

    def _separated_agent(self, username, remove_date, sep_status='finalized',
                         role_type='regular_agent'):
        a = _make_agent(username, role='agent', role_type=role_type)
        a.user.first_name = 'Pay'
        a.user.last_name = username.title()
        a.user.save()
        a.status = 'inactive'
        a.save()
        AgentSeparation.objects.create(
            agent=a, status=sep_status, separation_type='quit',
            last_day_worked=self.this_monday - timedelta(days=1),
            remove_from_adherence_date=remove_date,
        )
        return a

    def _export_names(self, query=''):
        resp = self.client.get(reverse('agent_list') + '?export=1' + query)
        self.assertEqual(resp['Content-Type'], XLSX_MIME)
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        rows = list(ws.iter_rows(values_only=True))
        return [r[0] for r in rows[1:]]

    def test_still_owed_agent_included(self):
        self._separated_agent('owed1', self.next_monday)
        names = self._export_names()
        self.assertIn('Pay Owed1', names)
        # Also caught when a role-type filter is active
        names = self._export_names('&role_type=regular_agent')
        self.assertIn('Pay Owed1', names)
        # But NOT with a non-matching role filter
        names = self._export_names('&role_type=kill_team')
        self.assertNotIn('Pay Owed1', names)
        # On-screen table unchanged: default active view hides them
        resp = self.client.get(reverse('agent_list'))
        screen = {a.user.username for a in resp.context['agents']}
        self.assertNotIn('owed1', screen)

    def test_window_closed_agent_excluded(self):
        self._separated_agent('gone1', self.last_monday)
        self._separated_agent('gone2', self.this_monday)  # boundary: == Monday → out
        names = self._export_names()
        self.assertNotIn('Pay Gone1', names)
        self.assertNotIn('Pay Gone2', names)
        # Explicitly choosing Inactive still shows them (user's explicit pick)
        names = self._export_names('&status_filter=inactive')
        self.assertIn('Pay Gone1', names)

    def test_missing_data_never_errors(self):
        # Inactive with no separation record at all
        bare = _make_agent('bare1', role='agent', role_type='regular_agent')
        bare.status = 'inactive'
        bare.save()
        # Separation without a remove date
        self._separated_agent('nodate1', None)
        names = self._export_names()
        self.assertNotIn('bare1', ''.join(names).lower())
        self.assertNotIn('Pay Nodate1', names)
