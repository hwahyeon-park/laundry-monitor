import unittest
from unittest.mock import patch
from notifications import (ENCOURAGEMENTS, SMS_VARIANTS, COMPLETION_HEADER,
                           completion_message, NotificationService)
import test_textbee


GSM_BASIC = set('@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà')
GSM_EXTENSION = set('\f^{}\\[~]|€')


def sms_length(message):
    if all(c in GSM_BASIC or c in GSM_EXTENSION for c in message):
        return sum(2 if c in GSM_EXTENSION else 1 for c in message), 160
    return len(message.encode('utf-16-be')) // 2, 70


class EncouragementTests(unittest.TestCase):
    setUp = test_textbee.TextBeeTests.setUp

    def test_original_four_encouragements_are_preserved(self):
        self.assertEqual(ENCOURAGEMENTS, (
            'The washing machine believes in you.',
            'Ahja ahja hwaiting!',
            'Sending you a tiny bit of good luck!',
            '세상이 거지같을땐 하이랜드 소를 생각해',
        ))
        self.assertEqual({v['encouragement'] for v in SMS_VARIANTS if v['encouragement']}, set(ENCOURAGEMENTS))

    def test_each_body_is_persisted_before_send_and_shared_by_recipients(self):
        for i, variant in enumerate(SMS_VARIANTS):
            with self.subTest(variant=variant['id']):
                self.service.begin_cycle('load-' + str(i))
                c = self.service.current()
                self.service.select(c['cycle_id'], ['wally', 'other'], c['revision'])
                self.post.reset_mock()
                message = completion_message(variant)
                def check_persisted(*args, **kwargs):
                    saved = self.service.current()
                    self.assertEqual(saved['sms_message'], message)
                    self.assertEqual(saved['sms_variant'], variant['id'])
                    self.assertEqual(saved['status'], 'claimed')
                    return self.post.return_value
                self.post.side_effect = check_persisted
                with patch('notifications.secrets.choice', return_value=variant) as choose:
                    self.assertEqual(self.service.on_state_transition(c['cycle_id'], 'RUNNING', 'FINISHED'), 'accepted')
                    choose.assert_called_once_with(SMS_VARIANTS)
                self.assertEqual(self.post.call_count, 2)
                self.assertTrue(all(call.kwargs['json']['message'] == message
                                    for call in self.post.call_args_list))
                restarted = NotificationService(self.service.store, self.service.recipients, self.env, self.post)
                self.assertEqual(restarted.on_state_transition(c['cycle_id'], 'RUNNING', 'FINISHED'), 'skipped')
                self.assertEqual(self.post.call_count, 2)
                self.assertEqual(restarted.current()['sms_message'], message)

    def test_all_complete_messages_fit_one_sms_segment(self):
        self.assertEqual(len({v['id'] for v in SMS_VARIANTS}), len(SMS_VARIANTS))
        for variant in SMS_VARIANTS:
            with self.subTest(variant=variant['id']):
                message = completion_message(variant)
                self.assertTrue(message.startswith(COMPLETION_HEADER + '\n'))
                units, limit = sms_length(message)
                self.assertLessEqual(units, limit)
        self.assertEqual(sms_length('^' * 80), (160, 160))
        self.assertEqual(sms_length('😀' * 35), (70, 70))

    def test_legacy_saved_encouragement_is_preserved(self):
        self.service.begin_cycle('legacy')
        c = self.service.current()
        self.service.select(c['cycle_id'], ['wally'], c['revision'])
        line = ENCOURAGEMENTS[3]
        self.service.store.transact(lambda state: state['cycles'][c['cycle_id']].update(encouragement=line))
        self.service.on_state_transition(c['cycle_id'], 'RUNNING', 'FINISHED')
        self.assertEqual(self.post.call_args.kwargs['json']['message'], COMPLETION_HEADER + '\n' + line)
        self.assertEqual(self.service.current()['sms_variant'], 'legacy')

    def test_connection_test_does_not_choose_art(self):
        with patch('notifications.secrets.choice') as choose:
            self.service.send_sms('wally', test=True)
            choose.assert_not_called()
        self.assertEqual(self.post.call_args.kwargs['json']['message'], '[TEST] Laundry Monitor SMS connection test.')
