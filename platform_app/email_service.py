# platform_app/email_service.py

import requests
from html import escape
from django.conf import settings
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

# Fallback used only if a caller doesn't pass an absolute logo URL built from
# the current request (register() does; anything calling this off-request
# falls back to the known production host).
DEFAULT_LOGO_URL = "https://mirrormarkettrading.com/static/platform_app/img/logo-512.png"

BRAND = {
    'bg': '#0a0a0a',
    'card': '#141414',
    'card_alt': '#1b1b1b',
    'border': '#2a2a2a',
    'accent': '#ffffff',
    'primary': '#ffffff',
    'text_main': '#fafafa',
    'text_muted': '#a3a3a0',
    'text_faint': '#737370',
    'danger': '#f87171',
    'success': '#4ade80',
}


def _email_shell(preheader, body_html, logo_url=None):
    """
    Shared HTML wrapper for every transactional email.

    Declares color-scheme as dark-only so Gmail/Outlook/Apple Mail's
    automatic dark-mode re-coloring doesn't fight our own dark palette
    and mangle it - without this meta tag, clients that auto-darken mail
    can invert or wash out custom dark backgrounds unpredictably.
    """
    logo_url = logo_url or DEFAULT_LOGO_URL
    b = BRAND

    return f"""
    <!DOCTYPE html>
    <html lang="en" style="background-color:{b['bg']};">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta name="color-scheme" content="dark">
        <meta name="supported-color-schemes" content="dark">
        <title>Mirrormarkettrading</title>
        <!--[if mso]>
        <style>table, td {{ font-family: Arial, sans-serif; }}</style>
        <![endif]-->
    </head>
    <body style="margin:0; padding:0; background-color:{b['bg']}; -webkit-text-size-adjust:100%;">
        <div style="display:none; max-height:0; overflow:hidden; opacity:0; font-size:1px; line-height:1px; color:{b['bg']};">
            {preheader}
        </div>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{b['bg']};">
            <tr>
                <td align="center" style="padding: 32px 16px;">
                    <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%; background-color:{b['card']}; border-radius:16px; overflow:hidden; border:1px solid {b['border']};">

                        <!-- Header -->
                        <tr>
                            <td align="center" style="padding: 32px 24px 24px 24px; border-bottom:1px solid {b['border']};">
                                <img src="{logo_url}" width="44" height="44" alt="Mirrormarkettrading"
                                     style="display:block; border-radius:10px; margin:0 auto 12px auto;">
                                <span style="font-family: Arial, Helvetica, sans-serif; font-size:18px; font-weight:bold; color:{b['text_main']};">
                                    Mirrormarkettrading
                                </span>
                            </td>
                        </tr>

                        <!-- Body -->
                        <tr>
                            <td style="padding: 32px 28px; font-family: Arial, Helvetica, sans-serif;">
                                {body_html}
                            </td>
                        </tr>

                        <!-- Footer -->
                        <tr>
                            <td style="padding: 22px 28px; border-top:1px solid {b['border']}; font-family: Arial, Helvetica, sans-serif;" align="center">
                                <p style="color:{b['text_faint']}; font-size:12px; margin:0 0 6px 0;">
                                    Need help? Our support team is available 24/7 via live chat on the platform.
                                </p>
                                <p style="color:{b['text_faint']}; font-size:11px; margin:0;">
                                    &copy; {timezone.now().year} Mirrormarkettrading. All rights reserved.
                                </p>
                            </td>
                        </tr>

                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """


def _button(label, url, brand):
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="margin: 26px auto 4px auto;">
        <tr>
            <td align="center" bgcolor="{brand['primary']}" style="border-radius:8px;">
                <a href="{url}" target="_blank"
                   style="display:inline-block; padding:14px 32px; font-family: Arial, Helvetica, sans-serif;
                          font-size:15px; font-weight:bold; color:#0a0a0a; text-decoration:none;">
                    {label}
                </a>
            </td>
        </tr>
    </table>
    """


class ResendEmailService:
    """Service for sending emails via the Resend API (https://resend.com)."""

    def __init__(self):
        self.api_key = settings.RESEND_API_KEY
        self.api_url = "https://api.resend.com/emails"
        self.sender_email = settings.RESEND_SENDER_EMAIL
        self.sender_name = settings.RESEND_SENDER_NAME
        self.admin_emails = list(getattr(settings, 'ADMIN_NOTIFICATION_EMAILS', []))

    def send_email(self, to_email, to_name, subject, html_content):
        """
        Send an email through Resend.

        Args:
            to_email: Recipient address, or a list of addresses
            to_name: Recipient name (used only for logging; Resend takes bare addresses)
            subject: Email subject
            html_content: HTML body

        Returns:
            dict: {'success': True, ...} or {'success': False, 'error': ...}
        """
        recipients = [to_email] if isinstance(to_email, str) else list(to_email)
        if not self.api_key:
            logger.error("RESEND_API_KEY is not set - cannot send email to %s", recipients)
            return {'success': False, 'error': 'RESEND_API_KEY not configured'}

        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        payload = {
            'from': f'{self.sender_name} <{self.sender_email}>',
            'to': recipients,
            'subject': subject,
            'html': html_content,
        }

        try:
            response = requests.post(self.api_url, json=payload, headers=headers, timeout=10)
            response.raise_for_status()
            logger.info("Email sent successfully to %s", recipients)
            return {'success': True, 'message': 'Email sent successfully', 'id': response.json().get('id')}
        except requests.exceptions.RequestException as e:
            detail = ''
            if getattr(e, 'response', None) is not None:
                detail = f' - {e.response.text[:300]}'
            logger.error("Failed to send email to %s: %s%s", recipients, e, detail)
            return {'success': False, 'error': str(e)}

    def send_password_reset_email(self, user_email, user_name, reset_code, logo_url=None):
        """Send password reset code email"""
        b = BRAND
        subject = "Password Reset Code - Mirrormarkettrading"

        body_html = f"""
            <h2 style="color:{b['text_main']}; margin:0 0 14px 0; font-size:22px; text-align:center;">
                Password Reset Request
            </h2>
            <p style="color:{b['text_muted']}; font-size:15px; line-height:1.6; margin:0 0 24px 0; text-align:center;">
                Hello <strong style="color:{b['text_main']};">{escape(user_name)}</strong>,
                we received a request to reset your password. Use the code below - it expires in
                <strong style="color:{b['accent']};">15 minutes</strong>.
            </p>

            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin: 0 0 24px 0;">
                <tr>
                    <td align="center" bgcolor="{b['card_alt']}" style="border-radius:10px; padding:24px; border:1px solid {b['border']};">
                        <p style="color:{b['text_muted']}; font-size:12px; margin:0 0 8px 0; letter-spacing:.05em; text-transform:uppercase;">
                            Your reset code
                        </p>
                        <p style="color:{b['accent']}; font-size:40px; font-weight:bold; letter-spacing:8px; margin:0;">
                            {reset_code}
                        </p>
                    </td>
                </tr>
            </table>

            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                    <td bgcolor="#2a1416" style="border-left:3px solid {b['danger']}; border-radius:6px; padding:16px 18px;">
                        <p style="color:#fca5a5; margin:0; font-size:13px; line-height:1.5;">
                            <strong>Didn't request this?</strong> Ignore this email - your account is safe
                            and no changes will be made.
                        </p>
                    </td>
                </tr>
            </table>
        """

        return self.send_email(user_email, user_name, subject, _email_shell("Your password reset code", body_html, logo_url))

    def send_welcome_email(self, user_email, user_name, dashboard_url=None, logo_url=None):
        """Send a welcome email right after a new account is created."""
        b = BRAND
        subject = "Welcome to Mirrormarkettrading"
        dashboard_url = dashboard_url or "https://mirrormarkettrading.com/dashboard/"

        steps = [
            ("1", "Complete your profile", "Confirm your details so we can personalize your dashboard."),
            ("2", "Fund your account", "Make your first deposit to unlock live trading and copy trading."),
            ("3", "Explore the markets", "Check live rates and pick an investment plan that fits your goals."),
        ]
        steps_html = "".join(f"""
            <tr>
                <td bgcolor="{b['card_alt']}" style="border-radius:10px; padding:16px 18px; border-left:3px solid {b['accent']};">
                    <p style="color:{b['text_main']}; margin:0; font-size:14px; font-weight:bold;">{num}. {title}</p>
                    <p style="color:{b['text_muted']}; margin:4px 0 0 0; font-size:13px; line-height:1.4;">{desc}</p>
                </td>
            </tr>
            <tr><td style="height:10px; line-height:10px; font-size:0;">&nbsp;</td></tr>
        """ for num, title, desc in steps)

        body_html = f"""
            <h2 style="color:{b['text_main']}; margin:0 0 14px 0; font-size:22px; text-align:center;">
                Welcome aboard, {escape(user_name)}!
            </h2>
            <p style="color:{b['text_muted']}; font-size:15px; line-height:1.6; margin:0 0 26px 0; text-align:center;">
                Your Mirrormarkettrading account is ready. You now have access to live markets,
                copy trading, AI trading bots, and 24/7 global market access.
            </p>

            <p style="color:{b['text_main']}; font-size:14px; font-weight:bold; margin:0 0 12px 0;">
                Get started in 3 steps
            </p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                {steps_html}
            </table>

            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr><td align="center">{_button("Go to My Dashboard", dashboard_url, b)}</td></tr>
            </table>
        """

        return self.send_email(user_email, user_name, subject, _email_shell(f"Welcome to Mirrormarkettrading, {escape(user_name)}", body_html, logo_url))

    def send_deposit_reminder_email(self, user_email, user_name, deposit_url=None, logo_url=None):
        """Friendly nudge to make a first deposit, sent shortly after signup."""
        b = BRAND
        subject = "Fund your account to start trading - Mirrormarkettrading"
        deposit_url = deposit_url or "https://mirrormarkettrading.com/deposit/"

        body_html = f"""
            <h2 style="color:{b['text_main']}; margin:0 0 14px 0; font-size:22px; text-align:center;">
                Hi {escape(user_name)}, ready to make your first move?
            </h2>
            <p style="color:{b['text_muted']}; font-size:15px; line-height:1.6; margin:0 0 8px 0; text-align:center;">
                Your account is set up, but it's still waiting for its first deposit.
                Fund your account now to unlock live trading, copy trading, and our AI bots.
            </p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr><td align="center">{_button("Make a Deposit", deposit_url, b)}</td></tr>
            </table>
        """

        return self.send_email(user_email, user_name, subject, _email_shell("Fund your account to unlock live trading", body_html, logo_url))

    def send_admin_new_registration_email(self, username, email, account_id, country, registered_at, admin_url=None, logo_url=None):
        """Notify the admin inbox whenever a new account is created."""
        b = BRAND
        if not self.admin_emails:
            logger.warning("No ADMIN_NOTIFICATION_EMAIL is set - skipping admin registration notice")
            return {'success': False, 'error': 'ADMIN_NOTIFICATION_EMAIL not configured'}

        subject = f"New registration: {username} - Mirrormarkettrading"

        def row(label, value, bold=False):
            weight = 'bold' if bold else 'normal'
            value = escape(str(value))
            return f"""
            <tr>
                <td style="padding:10px 0; border-bottom:1px solid {b['border']}; color:{b['text_muted']}; font-size:13px; width:130px;">{label}</td>
                <td style="padding:10px 0; border-bottom:1px solid {b['border']}; color:{b['text_main']}; font-size:14px; font-weight:{weight};">{value}</td>
            </tr>
            """

        rows_html = (
            row("Username", username, bold=True)
            + row("Email", email)
            + row("Account ID", f"#{account_id}")
            + row("Country", country or "Unknown")
            + row("Registered at", registered_at)
        )

        cta_html = ""
        if admin_url:
            cta_html = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr><td align="center">{_button("View User in Admin", admin_url, b)}</td></tr>
            </table>"""

        body_html = f"""
            <p style="color:{b['accent']}; font-size:12px; text-transform:uppercase; letter-spacing:.05em; margin:0 0 8px 0; text-align:center;">
                Admin Notification
            </p>
            <h2 style="color:{b['text_main']}; margin:0 0 22px 0; font-size:20px; text-align:center;">
                New user registered
            </h2>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
                {rows_html}
            </table>
            {cta_html}
        """

        return self.send_email(self.admin_emails, 'Admin', subject, _email_shell(f"New registration: {escape(username)}", body_html, logo_url))

    def send_admin_support_message_email(self, username, message_preview, chat_url=None, logo_url=None):
        """Notify the admin inbox when a client sends a support chat message."""
        b = BRAND
        if not self.admin_emails:
            logger.warning("No ADMIN_NOTIFICATION_EMAIL is set - skipping support message notice")
            return {'success': False, 'error': 'ADMIN_NOTIFICATION_EMAIL not configured'}

        subject = f"New support message from {username} - Mirrormarkettrading"

        cta_html = ""
        if chat_url:
            cta_html = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr><td align="center">{_button("Reply in Admin", chat_url, b)}</td></tr>
            </table>"""

        body_html = f"""
            <p style="color:{b['accent']}; font-size:12px; text-transform:uppercase; letter-spacing:.05em; margin:0 0 8px 0; text-align:center;">
                Admin Notification
            </p>
            <h2 style="color:{b['text_main']}; margin:0 0 20px 0; font-size:20px; text-align:center;">
                New support message
            </h2>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
                <tr>
                    <td bgcolor="{b['card_alt']}" style="border-radius:10px; padding:16px 18px; border-left:3px solid {b['accent']};">
                        <p style="color:{b['text_main']}; margin:0 0 6px 0; font-size:14px; font-weight:bold;">{escape(username)}</p>
                        <p style="color:{b['text_muted']}; margin:0; font-size:14px; line-height:1.5;">{escape(message_preview)}</p>
                    </td>
                </tr>
            </table>
            {cta_html}
        """

        return self.send_email(self.admin_emails, 'Admin', subject, _email_shell(f"New support message from {escape(username)}", body_html, logo_url))

    def send_user_transaction_approved_email(self, *, user_email, user_name, transaction_type, amount, new_balance=None,
                                             crypto=None, reference=None, plan_name=None, dashboard_url=None, logo_url=None):
        """Tell the customer that their deposit / withdrawal / bot purchase was approved."""
        b = BRAND
        dashboard_url = dashboard_url or "https://mirrormarkettrading.com/dashboard/"

        if transaction_type == 'DEPOSIT':
            subject = "Your deposit has been approved - Mirrormarkettrading"
            heading = "Deposit approved"
            intro = f"Good news - your payment was verified and <strong style=\"color:{b['text_main']};\">${escape(amount)}</strong> has been added to your account balance."
            cta = "View my balance"
        elif transaction_type == 'WITHDRAWAL':
            subject = "Your withdrawal has been approved - Mirrormarkettrading"
            heading = "Withdrawal approved"
            intro = (f"Your withdrawal of <strong style=\"color:{b['text_main']};\">${escape(amount)}</strong> has been approved. "
                     "The payout is being processed and will reach your wallet shortly.")
            cta = "View my account"
        elif transaction_type == 'BOT_PURCHASE':
            subject = "Your trading bot is active - Mirrormarkettrading"
            heading = "Trading bot activated"
            what = f" ({escape(plan_name)})" if plan_name else ""
            intro = f"Your payment of <strong style=\"color:{b['text_main']};\">${escape(amount)}</strong> was verified and your trading bot plan{what} is now active."
            cta = "Open my bots"
            dashboard_url = dashboard_url.replace('/dashboard/', '/trading-bot/')
        else:
            subject = "Your payment has been approved - Mirrormarkettrading"
            heading = "Payment approved"
            intro = f"Your payment of <strong style=\"color:{b['text_main']};\">${escape(amount)}</strong> has been approved."
            cta = "Go to my dashboard"

        def row(label, value):
            return f"""
            <tr>
                <td style="padding:10px 0; border-bottom:1px solid {b['border']}; color:{b['text_muted']}; font-size:13px; width:130px;">{label}</td>
                <td style="padding:10px 0; border-bottom:1px solid {b['border']}; color:{b['text_main']}; font-size:14px;">{escape(str(value))}</td>
            </tr>
            """

        rows = row('Amount', f'${amount}')
        if crypto:
            rows += row('Paid with', crypto)
        if reference:
            rows += row('Reference', reference)
        if new_balance is not None:
            rows += row('Account balance', f'${new_balance}')

        body_html = f"""
            <h2 style="color:{b['text_main']}; margin:0 0 14px 0; font-size:22px; text-align:center;">{heading}</h2>
            <p style="color:{b['text_muted']}; font-size:15px; line-height:1.6; margin:0 0 22px 0; text-align:center;">
                Hello <strong style="color:{b['text_main']};">{escape(user_name)}</strong>,<br>{intro}
            </p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr><td align="center">{_button(cta, dashboard_url, b)}</td></tr>
            </table>
            <p style="color:{b['text_faint']}; font-size:12px; line-height:1.5; margin:18px 0 0 0; text-align:center;">
                Didn't make this transaction? Contact our support team right away.
            </p>
        """
        return self.send_email(user_email, user_name, subject, _email_shell(heading, body_html, logo_url))

    def send_user_support_reply_email(self, *, user_email, user_name, message_preview, chat_url=None,
                                      is_new_conversation=False, logo_url=None):
        """Tell a customer that support wrote to them (reply or a message the admin started)."""
        b = BRAND
        chat_url = chat_url or "https://mirrormarkettrading.com/dashboard/"
        if is_new_conversation:
            subject = "New message from Mirrormarkettrading support"
            heading = "You have a new message"
            intro = "Our support team has sent you a message."
        else:
            subject = "Support replied to you - Mirrormarkettrading"
            heading = "Support replied to your message"
            intro = "Our support team has replied to your conversation."

        preview = message_preview if len(message_preview) <= 400 else message_preview[:397] + '...'
        body_html = f"""
            <h2 style="color:{b['text_main']}; margin:0 0 14px 0; font-size:22px; text-align:center;">{heading}</h2>
            <p style="color:{b['text_muted']}; font-size:15px; line-height:1.6; margin:0 0 22px 0; text-align:center;">
                Hello <strong style="color:{b['text_main']};">{escape(user_name)}</strong>,<br>{intro}
            </p>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:6px;">
                <tr>
                    <td bgcolor="{b['card_alt']}" style="border-radius:10px; padding:16px 18px; border-left:3px solid {b['accent']};">
                        <p style="color:{b['text_muted']}; margin:0 0 6px 0; font-size:12px; text-transform:uppercase; letter-spacing:.05em;">Support Team</p>
                        <p style="color:{b['text_main']}; margin:0; font-size:15px; line-height:1.55; white-space:pre-line;">{escape(preview)}</p>
                    </td>
                </tr>
            </table>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr><td align="center">{_button("Open the conversation", chat_url, b)}</td></tr>
            </table>
            <p style="color:{b['text_faint']}; font-size:12px; line-height:1.5; margin:18px 0 0 0; text-align:center;">
                Log in and open the Support chat to reply. Please don't reply to this email.
            </p>
        """
        return self.send_email(user_email, user_name, subject, _email_shell(heading, body_html, logo_url))

    # ------------------------------------------------------------------
    # Payment / approval notices
    # ------------------------------------------------------------------
    def _admin_notice(self, recipients, subject, kicker, heading, rows, cta_label=None, cta_url=None, logo_url=None):
        """Shared layout for the admin-facing notices below."""
        b = BRAND
        if not recipients:
            logger.warning("No admin recipient configured - skipping '%s'", subject)
            return {'success': False, 'error': 'No admin recipient configured'}

        def row(label, value, bold=False):
            weight = 'bold' if bold else 'normal'
            return f"""
            <tr>
                <td style="padding:10px 0; border-bottom:1px solid {b['border']}; color:{b['text_muted']}; font-size:13px; width:140px;">{label}</td>
                <td style="padding:10px 0; border-bottom:1px solid {b['border']}; color:{b['text_main']}; font-size:14px; font-weight:{weight};">{escape(str(value))}</td>
            </tr>
            """

        rows_html = ''.join(row(label, value, bold) for label, value, bold in rows)
        cta_html = ''
        if cta_url:
            cta_html = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:22px;">
                <tr><td align="center">{_button(cta_label, cta_url, b)}</td></tr>
            </table>"""

        body_html = f"""
            <p style="color:{b['accent']}; font-size:12px; text-transform:uppercase; letter-spacing:.05em; margin:0 0 8px 0; text-align:center;">
                {escape(kicker)}
            </p>
            <h2 style="color:{b['text_main']}; margin:0 0 22px 0; font-size:20px; text-align:center;">
                {escape(heading)}
            </h2>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                {rows_html}
            </table>
            {cta_html}
        """
        return self.send_email(recipients, 'Admin', subject, _email_shell(heading, body_html, logo_url))

    def send_admin_transaction_approved_email(self, *, kind, username, email, amount, status_label, new_balance=None,
                                              crypto=None, reference=None, approved_by=None, when=None,
                                              admin_url=None, logo_url=None):
        """Tell ADMIN_NOTIFICATION_EMAIL_TWO that something was approved."""
        rows = [
            ('Approved', kind, True),
            ('User', f'{username} ({email})', False),
            ('Amount', f'${amount}', True),
        ]
        if crypto:
            rows.append(('Paid with', crypto, False))
        if reference:
            rows.append(('Reference', reference, False))
        rows.append(('Result', status_label, False))
        if new_balance is not None:
            rows.append(('New balance', f'${new_balance}', False))
        if approved_by:
            rows.append(('Approved by', approved_by, False))
        if when:
            rows.append(('When', when, False))
        return self._admin_notice(
            list(getattr(settings, 'ADMIN_APPROVAL_EMAILS', [])),
            f'Approved: {kind} - ${amount} - {username}',
            'Approval confirmation', f'{kind} approved', rows,
            'Open in Admin' if admin_url else None, admin_url, logo_url,
        )

    def send_admin_payment_pending_email(self, *, kind, username, email, amount, crypto=None, reference=None,
                                         when=None, admin_url=None, logo_url=None):
        """Tell both admin inboxes that a payment / withdrawal is waiting to be verified."""
        rows = [
            ('Needs review', kind, True),
            ('User', f'{username} ({email})', False),
            ('Amount', f'${amount}', True),
        ]
        if crypto:
            rows.append(('Crypto', crypto, False))
        if reference:
            rows.append(('Reference', reference, False))
        if when:
            rows.append(('Submitted', when, False))
        return self._admin_notice(
            self.admin_emails,
            f'Awaiting approval: {kind} - ${amount} - {username}',
            'Action needed', f'{kind} waiting for approval', rows,
            'Review & approve', admin_url, logo_url,
        )

    def send_admin_withdrawal_access_email(self, *, username, email, granted, approved_by=None, when=None, admin_url=None, logo_url=None):
        """Tell ADMIN_NOTIFICATION_EMAIL_TWO that a user's withdrawal access changed."""
        state = 'granted' if granted else 'revoked'
        rows = [('User', f'{username} ({email})', True), ('Withdrawal access', state.title(), True)]
        if approved_by:
            rows.append(('Changed by', approved_by, False))
        if when:
            rows.append(('When', when, False))
        return self._admin_notice(
            list(getattr(settings, 'ADMIN_APPROVAL_EMAILS', [])),
            f'Withdrawal access {state}: {username}',
            'Approval confirmation', f'Withdrawal access {state}', rows,
            'Open user' if admin_url else None, admin_url, logo_url,
        )


# Convenience functions for easy import
def send_password_reset_code(user_email, user_name, reset_code, logo_url=None):
    """
    Convenience function to send password reset email

    Usage:
        from platform_app.email_service import send_password_reset_code
        send_password_reset_code('user@example.com', 'John Doe', '123456')
    """
    email_service = ResendEmailService()
    return email_service.send_password_reset_email(user_email, user_name, reset_code, logo_url)


def send_welcome_email(user_email, user_name, dashboard_url=None, logo_url=None):
    """Convenience function to send the post-registration welcome email."""
    email_service = ResendEmailService()
    return email_service.send_welcome_email(user_email, user_name, dashboard_url, logo_url)


def send_deposit_reminder(user_email, user_name, deposit_url=None, logo_url=None):
    """Convenience function to send the deposit-reminder email."""
    email_service = ResendEmailService()
    return email_service.send_deposit_reminder_email(user_email, user_name, deposit_url, logo_url)


def send_admin_new_registration_email(username, email, account_id, country, registered_at, admin_url=None, logo_url=None):
    """Convenience function to notify the admin inbox of a new registration."""
    email_service = ResendEmailService()
    return email_service.send_admin_new_registration_email(
        username, email, account_id, country, registered_at, admin_url, logo_url
    )


def send_admin_support_message_email(username, message_preview, chat_url=None, logo_url=None):
    """Convenience function to notify the admin inbox of a new support chat message."""
    email_service = ResendEmailService()
    return email_service.send_admin_support_message_email(username, message_preview, chat_url, logo_url)


def send_admin_transaction_approved_email(**kwargs):
    """Convenience function: approval confirmation to ADMIN_NOTIFICATION_EMAIL_TWO."""
    return ResendEmailService().send_admin_transaction_approved_email(**kwargs)


def send_admin_payment_pending_email(**kwargs):
    """Convenience function: a payment/withdrawal is waiting for review (both admin inboxes)."""
    return ResendEmailService().send_admin_payment_pending_email(**kwargs)


def send_admin_withdrawal_access_email(**kwargs):
    """Convenience function: withdrawal access granted/revoked (ADMIN_NOTIFICATION_EMAIL_TWO)."""
    return ResendEmailService().send_admin_withdrawal_access_email(**kwargs)


def send_user_transaction_approved_email(**kwargs):
    """Convenience function: tell the customer their deposit/withdrawal/bot purchase was approved."""
    return ResendEmailService().send_user_transaction_approved_email(**kwargs)


def send_user_support_reply_email(**kwargs):
    """Convenience function: tell a customer that support replied / messaged them."""
    return ResendEmailService().send_user_support_reply_email(**kwargs)
