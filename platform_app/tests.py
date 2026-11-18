import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from .models import (
    User, PasswordResetCode, Transaction, CopyTrader, CopyTraderPlan,
    CopyTradingSubscription, PlatformSettings,
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
