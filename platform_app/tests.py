import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from .models import (
    User, PasswordResetCode, Transaction, CopyTrader, CopyTraderPlan,
    CopyTradingSubscription, PlatformSettings, SupportChat, SupportMessage,
)

STRONG_PASSWORD = 'Sturdy-Pass-9431'


def make_user(username='alice', email='alice@example.com', **extra):
    return User.objects.create_user(
        username=username, email=email, password=STRONG_PASSWORD,
        legal_first_name='Alice', legal_last_name='Smith', **extra,
    )


@patch('platform_app.views.get_country_from_ip', return_value='Testland')
@patch('platform_app.views.send_admin_new_registration_email')
@patch('platform_app.views.send_deposit_reminder')
@patch('platform_app.views.send_welcome_email')
class RegistrationSecurityTests(TestCase):
    def post_register(self, **overrides):
        data = {
            'username': 'newuser', 'email': 'new@example.com', 'password': STRONG_PASSWORD,
            'legal_first_name': 'New', 'legal_last_name': 'User', 'preferred_currency': 'USD',
        }
        data.update(overrides)
        return self.client.post(reverse('register'), data, REMOTE_ADDR='10.0.0.1')

    def test_valid_registration_succeeds(self, *mocks):
        self.post_register()
        self.assertTrue(User.objects.filter(username='newuser').exists())

    def test_html_in_username_is_rejected(self, *mocks):
        self.post_register(username='<img src=x onerror=alert(1)>')
        self.assertFalse(User.objects.exclude(username='newuser').exists())

    def test_html_in_name_is_rejected(self, *mocks):
        self.post_register(legal_first_name='<b>x</b>')
        self.assertFalse(User.objects.exists())

    def test_weak_password_is_rejected(self, *mocks):
        self.post_register(password='123')
        self.assertFalse(User.objects.exists())

    def test_mass_registration_is_rate_limited(self, *mocks):
        for i in range(7):
            self.post_register(username=f'bulk{i}', email=f'bulk{i}@example.com')
        self.assertEqual(User.objects.count(), 5)


class LoginSecurityTests(TestCase):
    def setUp(self):
        make_user()

    def test_unknown_email_and_wrong_password_look_the_same(self):
        r1 = self.client.post(reverse('login'), {'email': 'nobody@example.com', 'password': 'x'}, follow=True)
        r2 = self.client.post(reverse('login'), {'email': 'alice@example.com', 'password': 'x'}, follow=True)
        self.assertContains(r1, 'Invalid email or password')
        self.assertContains(r2, 'Invalid email or password')
        self.assertNotContains(r1, 'User not found')

    def test_brute_force_locks_out_even_correct_password(self):
        for _ in range(8):
            self.client.post(reverse('login'), {'email': 'alice@example.com', 'password': 'wrong'})
        self.client.post(reverse('login'), {'email': 'alice@example.com', 'password': STRONG_PASSWORD})
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_correct_password_logs_in(self):
        self.client.post(reverse('login'), {'email': 'alice@example.com', 'password': STRONG_PASSWORD})
        self.assertIn('_auth_user_id', self.client.session)


@patch('platform_app.views.send_password_reset_code', return_value={'success': True})
class PasswordResetSecurityTests(TestCase):
    def test_code_guessing_is_capped(self, _send):
        user = make_user()
        self.client.post(reverse('forgot_password'), {'email': user.email})
        real_code = PasswordResetCode.objects.get(user=user, is_used=False).code
        wrong = '000000' if real_code != '000000' else '111111'
        for _ in range(6):
            self.client.post(reverse('verify_reset_code'), {'code': wrong})
        # The real code is burned once guessing is cut off
        self.assertFalse(PasswordResetCode.objects.filter(user=user, is_used=False).exists())
        self.client.post(reverse('verify_reset_code'), {'code': real_code})
        self.assertNotIn('reset_code_id', self.client.session)


@patch('platform_app.views.send_admin_support_message_email')
class SupportChatSecurityTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user())

    def send(self, text):
        return self.client.post(reverse('send_support_message'), json.dumps({'message': text}),
                                content_type='application/json')

    def test_overlong_message_rejected(self, _email):
        self.assertEqual(self.send('a' * 2001).status_code, 400)

    def test_message_flood_is_rate_limited(self, _email):
        codes = [self.send(f'msg {i}').status_code for i in range(16)]
        self.assertEqual(codes[:15], [200] * 15)
        self.assertEqual(codes[15], 429)

    def test_admin_email_is_throttled_per_chat(self, email):
        for i in range(5):
            self.send(f'msg {i}')
        self.assertEqual(email.call_count, 1)


class PublicFiguresFollowSettingsTests(TestCase):
    def test_pages_show_admin_configured_limits_and_fee(self):
        PlatformSettings.objects.create(minimum_deposit=0, minimum_withdrawal=Decimal('10'),
                                        withdrawal_fee_percentage=Decimal('10'))
        self.client.force_login(make_user(withdrawal_approved=True, account_balance=Decimal('100')))

        page = self.client.get(reverse('withdrawal')).content.decode()
        self.assertIn('Minimum withdrawal: $10.00', page)
        self.assertIn('min="10.00"', page)
        self.assertIn('Withdrawals require a fee', page)
        self.assertNotIn('10% withdrawal', page)

        terms = self.client.get(reverse('terms_of_service')).content.decode()
        self.assertIn('There is no minimum deposit.', terms)
        self.assertIn('Withdrawals require a fee.', terms)

        for name in ('crypto', 'forex', 'about'):
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn('$50', html, name)


class MoneySafetyTests(TestCase):
    def setUp(self):
        self.user = make_user(account_balance=Decimal('19.91'), total_deposit=Decimal('19.91'))
        self.client.force_login(self.user)
        PlatformSettings.objects.create(copy_trade_auto_invest=True)
        self.trader = CopyTrader.objects.create(
            name='Trader', country='X', country_flag='x', rating=Decimal('4.5'),
            profit_percentage_per_day=Decimal('1'), roi=Decimal('10'), commission_percentage=Decimal('10'),
        )
        CopyTraderPlan.objects.create(trader=self.trader, tier='SILVER', price=Decimal('50'),
                                      commission_percentage=Decimal('10'))

    def test_copy_trade_below_minimum_rejected(self):
        r = self.client.post(reverse('copy_trader_detail', args=[self.trader.id]))
        self.assertFalse(r.json()['success'])
        self.assertFalse(CopyTradingSubscription.objects.exists())
        self.user.refresh_from_db()
        self.assertEqual(self.user.account_balance, Decimal('19.91'))

    @patch('platform_app.views.get_crypto_price', return_value=Decimal('75000'))
    def test_negative_trade_cannot_mint_balance(self, _price):
        # The exact exploit used in production: "buy" BTC with -$1,000,000
        for bad in ('-1000000', '-1', '0', 'NaN'):
            self.client.post(reverse('trading'), {
                'trade_type': 'BUY', 'from_currency': 'USD', 'to_currency': 'BTC', 'from_amount': bad,
            })
        self.user.refresh_from_db()
        self.assertEqual(self.user.account_balance, Decimal('19.91'))

    @patch('platform_app.views.get_crypto_price', return_value=Decimal('75000'))
    def test_normal_trade_still_works(self, _price):
        self.client.post(reverse('trading'), {
            'trade_type': 'BUY', 'from_currency': 'USD', 'to_currency': 'BTC', 'from_amount': '10',
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.account_balance, Decimal('9.91'))

    def test_garbage_deposit_amount_does_not_crash(self):
        for bad in ('NaN', 'Infinity', 'abc', '-5', ''):
            r = self.client.post(reverse('deposit'), {'amount': bad})
            self.assertEqual(r.status_code, 302, bad)

    def test_withdrawal_approval_blocked_when_balance_too_low(self):
        admin = User.objects.create_user(username='boss', email='boss@example.com',
                                         password=STRONG_PASSWORD, is_staff=True)
        tx = Transaction.objects.create(user=self.user, transaction_type='WITHDRAWAL',
                                        amount=Decimal('100'), status='PENDING')
        self.client.force_login(admin)
        self.client.post(reverse('admin_transaction_action', args=[tx.id]), {'action': 'approve'})
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'PENDING')

    def test_deposit_cannot_be_approved_twice(self):
        admin = User.objects.create_user(username='boss', email='boss@example.com',
                                         password=STRONG_PASSWORD, is_staff=True)
        tx = Transaction.objects.create(user=self.user, transaction_type='DEPOSIT',
                                        amount=Decimal('100'), status='PENDING')
        self.client.force_login(admin)
        url = reverse('admin_transaction_action', args=[tx.id])
        self.client.post(url, {'action': 'approve'})
        self.client.post(url, {'action': 'approve'})
        self.user.refresh_from_db()
        self.assertEqual(self.user.account_balance, Decimal('119.91'))


class ApprovalFlowTests(TestCase):
    """Admin approval credits the right thing and reports it to inbox TWO only."""

    def setUp(self):
        self.admin = make_user('boss', 'boss@example.com', is_staff=True, is_superuser=True)
        self.user = make_user('payer', 'payer@example.com', account_balance=Decimal('10'), withdrawal_approved=True)
        self.client.force_login(self.admin)
        # Never let a test reach the real Resend API (the key in .env is live).
        user_mail = patch('platform_app.admin_views.send_user_transaction_approved_email')
        self.user_mail = user_mail.start()
        self.addCleanup(user_mail.stop)

    def approve(self, tx):
        return self.client.post(reverse('admin_transaction_action', args=[tx.id]), {'action': 'approve'})

    @patch('platform_app.admin_views.send_admin_transaction_approved_email')
    def test_deposit_approval_credits_balance_and_emails_once(self, mail):
        tx = Transaction.objects.create(user=self.user, transaction_type='DEPOSIT', amount=Decimal('250'), crypto_currency='BTC')
        self.approve(tx)
        self.approve(tx)  # double click
        self.user.refresh_from_db()
        self.assertEqual(self.user.account_balance, Decimal('260'))
        self.assertEqual(self.user.total_deposit, Decimal('250'))
        self.assertEqual(mail.call_count, 1)
        self.assertEqual(mail.call_args.kwargs['kind'], 'Deposit')
        self.assertEqual(mail.call_args.kwargs['new_balance'], '260.00')

    @patch('platform_app.admin_views.send_admin_transaction_approved_email')
    def test_rejected_transaction_sends_no_approval_email(self, mail):
        tx = Transaction.objects.create(user=self.user, transaction_type='DEPOSIT', amount=Decimal('250'))
        self.client.post(reverse('admin_transaction_action', args=[tx.id]), {'action': 'reject'})
        mail.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(self.user.account_balance, Decimal('10'))

    @patch('platform_app.admin_views.send_admin_transaction_approved_email', side_effect=RuntimeError('resend down'))
    def test_email_failure_does_not_undo_approval(self, mail):
        tx = Transaction.objects.create(user=self.user, transaction_type='DEPOSIT', amount=Decimal('5'))
        self.approve(tx)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'APPROVED')

    @patch('platform_app.admin_views.send_admin_withdrawal_access_email')
    def test_granting_withdrawal_access_emails(self, mail):
        other = make_user('newbie', 'newbie@example.com')
        self.client.post(reverse('admin_user_detail', args=[other.id]), {'action': 'toggle_withdrawal'})
        self.assertEqual(mail.call_count, 1)
        self.client.post(reverse('admin_user_detail', args=[other.id]), {'action': 'toggle_withdrawal'})  # revoke
        self.assertEqual(mail.call_count, 1)

    @patch('platform_app.email_service.requests.post')
    def test_approval_email_goes_to_inbox_two_only(self, post):
        from django.test import override_settings
        post.return_value.json.return_value = {'id': 'x'}
        with override_settings(RESEND_API_KEY='re_test', ADMIN_APPROVAL_EMAILS=['two@example.com'],
                               ADMIN_NOTIFICATION_EMAILS=['one@example.com', 'two@example.com']):
            from .email_service import send_admin_transaction_approved_email
            send_admin_transaction_approved_email(kind='Deposit', username='u', email='u@x.com', amount='5.00',
                                                  status_label='Credited')
        self.assertEqual(post.call_args.kwargs['json']['to'], ['two@example.com'])

    @patch('platform_app.views.send_admin_payment_pending_email')
    def test_withdrawal_request_alerts_admins(self, mail):
        PlatformSettings.objects.create(minimum_withdrawal=Decimal('1'))
        self.client.force_login(self.user)
        with patch('platform_app.views.get_crypto_price', return_value=Decimal('50000')):
            self.client.post(reverse('withdrawal'), {'amount': '5', 'cryptocurrency': 'BTC', 'wallet_address': 'bc1qxyz'})
        self.assertEqual(mail.call_count, 1)
        self.assertEqual(mail.call_args.kwargs['kind'], 'Withdrawal')

    @patch('platform_app.admin_views.send_admin_transaction_approved_email')
    def test_customer_gets_their_own_approval_email(self, admin_mail):
        tx = Transaction.objects.create(user=self.user, transaction_type='DEPOSIT', amount=Decimal('75'))
        self.approve(tx)
        self.assertEqual(self.user_mail.call_count, 1)
        kw = self.user_mail.call_args.kwargs
        self.assertEqual(kw['user_email'], 'payer@example.com')
        self.assertEqual(kw['transaction_type'], 'DEPOSIT')
        self.assertEqual(kw['new_balance'], '85.00')

    @patch('platform_app.admin_views.send_admin_transaction_approved_email')
    def test_customer_gets_no_email_on_rejection(self, admin_mail):
        tx = Transaction.objects.create(user=self.user, transaction_type='DEPOSIT', amount=Decimal('75'))
        self.client.post(reverse('admin_transaction_action', args=[tx.id]), {'action': 'reject'})
        self.user_mail.assert_not_called()

    @patch('platform_app.email_service.requests.post')
    def test_customer_email_is_addressed_to_the_customer_only(self, post):
        from django.test import override_settings
        post.return_value.json.return_value = {'id': 'x'}
        with override_settings(RESEND_API_KEY='re_test'):
            from .email_service import ResendEmailService
            for kind in ('DEPOSIT', 'WITHDRAWAL', 'BOT_PURCHASE'):
                ResendEmailService().send_user_transaction_approved_email(
                    user_email='payer@example.com', user_name='Alice', transaction_type=kind, amount='75.00', new_balance='85.00')
                self.assertEqual(post.call_args.kwargs['json']['to'], ['payer@example.com'])
        subjects = [c.kwargs['json']['subject'] for c in post.call_args_list]
        self.assertTrue(any('deposit' in s for s in subjects) and any('withdrawal' in s for s in subjects) and any('bot' in s for s in subjects))


class SupportInboxTests(TestCase):
    """Admin inbox: reply to many customers, and each of them is emailed."""

    def setUp(self):
        self.admin = make_user('boss', 'boss@example.com', is_staff=True, is_superuser=True, first_name='Ada')
        self.bob = make_user('bob', 'bob@example.com')
        self.cat = make_user('cat', 'cat@example.com')
        self.bob_chat = SupportChat.objects.create(user=self.bob)
        self.cat_chat = SupportChat.objects.create(user=self.cat)
        SupportMessage.objects.create(chat=self.bob_chat, sender_type='USER', sender_name='bob', message='Where is my deposit?')
        SupportMessage.objects.create(chat=self.cat_chat, sender_type='USER', sender_name='cat', message='Hello')
        SupportMessage.objects.create(chat=self.cat_chat, sender_type='USER', sender_name='cat', message='Anyone there?')
        self.client.force_login(self.admin)
        mail = patch('platform_app.admin_views.send_user_support_reply_email')
        self.mail = mail.start()
        self.addCleanup(mail.stop)
        rl = patch('platform_app.admin_views.rate_limited', return_value=False)
        self.rl = rl.start()
        self.addCleanup(rl.stop)

    def send(self, chat, text='On it!'):
        return self.client.post(reverse('admin_support_send', args=[chat.id]), data=json.dumps({'message': text}),
                                content_type='application/json')

    def test_inbox_lists_every_conversation_with_unread_counts(self):
        rows = {r['username']: r for r in self.client.get(reverse('admin_support_list_json')).json()['chats']}
        self.assertEqual(rows['bob']['unread'], 1)
        self.assertEqual(rows['cat']['unread'], 2)
        self.assertEqual(rows['cat']['last_text'], 'Anyone there?')
        unread = self.client.get(reverse('admin_support_list_json') + '?status=unread').json()['chats']
        self.assertEqual(len(unread), 2)

    def test_inbox_hides_chats_nobody_has_written_in(self):
        lurker = make_user('lurker', 'lurker@example.com')
        SupportChat.objects.create(user=lurker)                       # opened the bubble, said nothing
        greeted = make_user('greeted', 'greeted@example.com')
        g = SupportChat.objects.create(user=greeted)                  # only the automatic greeting
        SupportMessage.objects.create(chat=g, sender_type='SUPPORT', sender_name='Support Team', message='Hello! Thanks for reaching out.')
        botted = make_user('botted', 'botted@example.com')
        b = SupportChat.objects.create(user=botted)
        SupportMessage.objects.create(chat=b, sender_type='SUPPORT', sender_name='Support Bot', message='auto reply')
        names = {r['username'] for r in self.client.get(reverse('admin_support_list_json')).json()['chats']}
        self.assertEqual(names, {'bob', 'cat'})

    def test_chat_appears_once_the_customer_writes_or_an_admin_messages(self):
        quiet = make_user('quiet', 'quiet@example.com')
        q = SupportChat.objects.create(user=quiet)
        listed = lambda: {r['username'] for r in self.client.get(reverse('admin_support_list_json')).json()['chats']}
        self.assertNotIn('quiet', listed())
        self.send(q, 'Hi, anything I can help with?')
        self.assertIn('quiet', listed())

    def test_reply_is_saved_marks_read_and_emails_customer(self):
        res = self.send(self.bob_chat, 'Checking your payment now')
        self.assertEqual(res.status_code, 200)
        msg = SupportMessage.objects.filter(chat=self.bob_chat, sender_type='SUPPORT').get()
        self.assertEqual(msg.message, 'Checking your payment now')
        self.assertFalse(self.bob_chat.messages.filter(sender_type='USER', is_read=False).exists())
        self.assertEqual(self.mail.call_count, 1)
        kw = self.mail.call_args.kwargs
        self.assertEqual(kw['user_email'], 'bob@example.com')
        self.assertFalse(kw['is_new_conversation'])
        self.assertIn('Checking your payment', kw['message_preview'])

    def test_each_customer_gets_their_own_email(self):
        self.send(self.bob_chat, 'hi bob')
        self.send(self.cat_chat, 'hi cat')
        self.assertEqual([c.kwargs['user_email'] for c in self.mail.call_args_list], ['bob@example.com', 'cat@example.com'])

    def test_burst_of_replies_only_emails_once(self):
        self.rl.side_effect = [False, True]
        self.send(self.bob_chat, 'one')
        self.send(self.bob_chat, 'two')
        self.assertEqual(self.mail.call_count, 1)
        self.assertEqual(SupportMessage.objects.filter(chat=self.bob_chat, sender_type='SUPPORT').count(), 2)

    def test_email_failure_does_not_lose_the_reply(self):
        self.mail.side_effect = RuntimeError('resend down')
        res = self.send(self.bob_chat)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(SupportMessage.objects.filter(chat=self.bob_chat, sender_type='SUPPORT').exists())

    def test_reply_to_closed_chat_reaches_the_customers_active_chat(self):
        self.bob_chat.status = 'CLOSED'
        self.bob_chat.save()
        active = SupportChat.objects.create(user=self.bob)
        res = self.send(self.bob_chat, 'late reply').json()
        self.assertEqual(res['chat_id'], active.id)
        self.assertTrue(active.messages.filter(message='late reply').exists())

    def test_reply_to_closed_chat_reopens_it_when_no_other_active_chat(self):
        self.bob_chat.status = 'CLOSED'
        self.bob_chat.save()
        self.send(self.bob_chat, 'back again')
        self.bob_chat.refresh_from_db()
        self.assertEqual(self.bob_chat.status, 'ACTIVE')

    def test_cannot_reopen_a_chat_when_customer_already_has_an_active_one(self):
        self.bob_chat.status = 'CLOSED'
        self.bob_chat.save()
        SupportChat.objects.create(user=self.bob)
        res = self.client.post(reverse('admin_chat_close', args=[self.bob_chat.id]), HTTP_X_REQUESTED_WITH='fetch')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(SupportChat.objects.filter(user=self.bob, status='ACTIVE').count(), 1)

    def test_admin_can_start_a_new_conversation_and_customer_is_emailed(self):
        dan = make_user('dan', 'dan@example.com')
        res = self.client.post(reverse('admin_support_new'), data=json.dumps({'user_id': dan.id, 'message': 'Welcome aboard!'}),
                               content_type='application/json')
        self.assertEqual(res.status_code, 200)
        chat = SupportChat.objects.get(user=dan, status='ACTIVE')
        self.assertTrue(chat.messages.filter(sender_type='SUPPORT', message='Welcome aboard!').exists())
        self.assertTrue(self.mail.call_args.kwargs['is_new_conversation'])
        self.client.post(reverse('admin_support_new'), data=json.dumps({'user_id': dan.id, 'message': 'Another'}),
                         content_type='application/json')
        self.assertEqual(SupportChat.objects.filter(user=dan, status='ACTIVE').count(), 1)

    def test_empty_and_oversized_messages_are_rejected(self):
        self.assertEqual(self.send(self.bob_chat, '   ').status_code, 400)
        self.assertEqual(self.send(self.bob_chat, 'x' * 2001).status_code, 400)
        self.mail.assert_not_called()

    def test_live_polling_returns_only_new_messages(self):
        first = self.client.get(reverse('admin_support_messages_json', args=[self.cat_chat.id])).json()
        self.assertEqual(len(first['messages']), 2)
        last = first['messages'][-1]['id']
        newer = self.client.get(reverse('admin_support_messages_json', args=[self.cat_chat.id]) + f'?after={last}').json()
        self.assertEqual(newer['messages'], [])
        self.assertFalse(self.cat_chat.messages.filter(sender_type='USER', is_read=False).exists())

    def test_customers_and_anonymous_cannot_use_the_admin_inbox(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('admin_support_chats')).status_code, 302)
        self.client.force_login(self.bob)
        for url in (reverse('admin_support_chats'), reverse('admin_support_list_json')):
            self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.send(self.cat_chat).status_code, 302)
        self.assertFalse(SupportMessage.objects.filter(sender_type='SUPPORT').exists())

    def test_user_unread_badge_counts_support_messages(self):
        self.send(self.bob_chat, 'ping')
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(reverse('support_unread_count')).json()['unread'], 1)
        self.client.get(reverse('get_chat_messages'))
        self.assertEqual(self.client.get(reverse('support_unread_count')).json()['unread'], 0)

    def test_inbox_page_and_email_deep_link_render(self):
        self.assertEqual(self.client.get(reverse('admin_support_chats')).status_code, 200)
        res = self.client.get(reverse('admin_chat_detail', args=[self.bob_chat.id]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'id="ibThread"')


class CopyTradingRulesTests(TestCase):
    """One copy per trader per account, and copy trading is locked until the first deposit."""

    def setUp(self):
        PlatformSettings.objects.create(copy_trade_auto_invest=True)
        self.trader = CopyTrader.objects.create(
            name='Tomi', country='X', country_flag='x', rating=Decimal('4.5'),
            profit_percentage_per_day=Decimal('1'), roi=Decimal('10'), commission_percentage=Decimal('10'),
        )
        CopyTraderPlan.objects.create(trader=self.trader, tier='SILVER', price=Decimal('50'),
                                      commission_percentage=Decimal('10'))
        self.url = reverse('copy_trader_detail', args=[self.trader.id])

    def test_locked_until_first_deposit(self):
        user = make_user(account_balance=Decimal('500'), total_deposit=Decimal('0'))  # e.g. bonus only
        self.client.force_login(user)
        r = self.client.post(self.url)
        self.assertFalse(r.json()['success'])
        self.assertIn('locked', r.json()['message'])
        self.assertFalse(CopyTradingSubscription.objects.exists())

        page = self.client.get(reverse('copy_trading')).content.decode()
        self.assertIn('Copy trading is locked', page)
        self.assertIn('Deposit to unlock', self.client.get(self.url).content.decode())

    def test_deposited_user_can_copy_once_only(self):
        user = make_user(account_balance=Decimal('300'), total_deposit=Decimal('300'))
        self.client.force_login(user)
        self.assertTrue(self.client.post(self.url).json()['success'])
        self.assertEqual(CopyTradingSubscription.objects.filter(user=user, trader=self.trader).count(), 1)

        user.refresh_from_db()
        user.account_balance = user.total_deposit = Decimal('300')  # top up and try again
        user.save()
        r = self.client.post(self.url)
        self.assertFalse(r.json()['success'])
        self.assertIn('already copied', r.json()['message'])
        self.assertEqual(CopyTradingSubscription.objects.filter(user=user, trader=self.trader).count(), 1)

        self.assertIn('Already copied', self.client.get(reverse('copy_trading')).content.decode())

    def test_stopped_copy_still_counts(self):
        user = make_user(account_balance=Decimal('300'), total_deposit=Decimal('300'))
        self.client.force_login(user)
        CopyTradingSubscription.objects.create(user=user, trader=self.trader, amount_invested=Decimal('50'),
                                               commission_paid=Decimal('5'), is_active=False)
        self.assertFalse(self.client.post(self.url).json()['success'])
        self.assertEqual(CopyTradingSubscription.objects.filter(user=user).count(), 1)
