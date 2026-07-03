from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from adherence.models import AdherenceRecord, Coding
from .models import Agent, AgentRequest


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
