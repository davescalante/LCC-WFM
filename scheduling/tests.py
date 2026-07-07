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

    def test_official_admin_coding_creates_admin_coding(self):
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
        coding = Coding.objects.get(agent=admin)
        self.assertTrue(coding.is_admin_coding)

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
