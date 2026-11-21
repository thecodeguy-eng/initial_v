# Create this file: platform_app/admin_views.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import user_passes_test
from django.contrib import messages
from django.http import JsonResponse
from django.db import transaction as db_transaction
from django.db.models import Sum, Count, Q, Avg, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import json
import logging
from django.urls import reverse
from django.templatetags.static import static

from .email_service import (
    send_admin_transaction_approved_email, send_admin_withdrawal_access_email,
    send_user_transaction_approved_email, send_user_support_reply_email,
)
from .security import rate_limited

logger = logging.getLogger(__name__)

from .models import (
    User, WalletAddress, TradingBotPlan, UserBotSubscription,
    CopyTrader, CopyTraderPlan, CopyTradingSubscription, Transaction, Portfolio,
    Trade, TradeHistory, PlatformSettings, SupportChat, SupportMessage,
    UserActivity, AdminUser, SystemLog,
    InvestmentPlan, UserInvestment,
)


def is_admin(user):
    """Check if user is admin"""
    return user.is_authenticated and (user.is_superuser or user.is_staff)


TX_KIND = {
    'DEPOSIT': 'Deposit',
    'WITHDRAWAL': 'Withdrawal',
    'BOT_PURCHASE': 'Trading bot purchase',
    'COPY_TRADE': 'Copy trading payment',
    'BONUS': 'Bonus',
    'PROFIT': 'Profit credit',
}


def _notify_transaction_approved(request, transaction, user):
    """Email ADMIN_NOTIFICATION_EMAIL_TWO that a transaction was approved.

    Never raises: a mail outage must not undo (or hide) an approval that already happened.
    """
    try:
        kind = TX_KIND.get(transaction.transaction_type, transaction.get_transaction_type_display())
        if transaction.transaction_type == 'DEPOSIT':
            result = 'Credited to the user\'s balance'
        elif transaction.transaction_type == 'WITHDRAWAL':
            result = 'Deducted from the user\'s balance - send the payout'
        elif transaction.transaction_type == 'BOT_PURCHASE':
            plan = transaction.bot_plan.get_plan_type_display() if transaction.bot_plan else 'bot'
            result = f'Bot plan activated ({plan})'
        else:
            result = 'Approved'
        crypto = None
        if transaction.crypto_currency:
            crypto = transaction.crypto_currency
            if transaction.crypto_amount:
                crypto = f'{transaction.crypto_amount:.8f} {transaction.crypto_currency}'
        send_admin_transaction_approved_email(
            kind=kind,
            username=user.username,
            email=user.email,
            amount=f'{transaction.amount:,.2f}',
            status_label=result,
            new_balance=f'{user.account_balance:,.2f}',
            crypto=crypto,
            reference=transaction.blockchain_transaction_id or str(transaction.transaction_id)[:8],
            approved_by=request.user.username,
            when=timezone.now().strftime('%d %b %Y, %H:%M UTC'),
            admin_url=request.build_absolute_uri(reverse('admin_transactions_list') + '?status=approved'),
            logo_url=request.build_absolute_uri(static('platform_app/img/logo-512.png')),
        )
    except Exception:
        logger.exception('Approval email failed for transaction %s', transaction.pk)

    # The customer gets their own confirmation (deposits, withdrawals and bot purchases).
    if transaction.transaction_type in ('DEPOSIT', 'WITHDRAWAL', 'BOT_PURCHASE') and user.email:
        try:
            crypto = None
            if transaction.crypto_currency:
                crypto = transaction.crypto_currency
                if transaction.crypto_amount:
                    crypto = f'{transaction.crypto_amount:.8f} {transaction.crypto_currency}'
            send_user_transaction_approved_email(
                user_email=user.email,
                user_name=user.legal_first_name or user.username,
                transaction_type=transaction.transaction_type,
                amount=f'{transaction.amount:,.2f}',
                new_balance=f'{user.account_balance:,.2f}',
                crypto=crypto,
                reference=transaction.blockchain_transaction_id or None,
                plan_name=transaction.bot_plan.get_plan_type_display() if transaction.bot_plan else None,
                dashboard_url=request.build_absolute_uri(reverse('dashboard')),
                logo_url=request.build_absolute_uri(static('platform_app/img/logo-512.png')),
            )
        except Exception:
            logger.exception('Customer approval email failed for transaction %s', transaction.pk)


@user_passes_test(is_admin)
def admin_dashboard(request):
    """Main admin dashboard with statistics"""
    # Get date range for filtering
    today = timezone.now().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    
    # User statistics
    total_users = User.objects.count()
    new_users_today = User.objects.filter(created_at__date=today).count()
    new_users_week = User.objects.filter(created_at__date__gte=week_ago).count()
    active_users = User.objects.filter(last_login__gte=week_ago).count()
    
    # Financial statistics
    total_deposits = Transaction.objects.filter(
        transaction_type='DEPOSIT', 
        status='APPROVED'
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    total_withdrawals = Transaction.objects.filter(
        transaction_type='WITHDRAWAL',
        status='APPROVED'
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    pending_deposits = Transaction.objects.filter(
        transaction_type='DEPOSIT',
        status='PENDING'
    ).count()
    
    pending_withdrawals = Transaction.objects.filter(
        transaction_type='WITHDRAWAL',
        status='PENDING'
    ).count()
    
    # Trading statistics
    total_trades = Trade.objects.count()
    active_bots = UserBotSubscription.objects.filter(is_active=True).count()
    active_copy_trades = CopyTradingSubscription.objects.filter(is_active=True).count()
    
    # Support statistics
    open_chats = SupportChat.objects.filter(status='ACTIVE').count()
    unread_messages = SupportMessage.objects.filter(
        sender_type='USER',
        is_read=False
    ).count()
    
    # Recent activities
    recent_activities = UserActivity.objects.select_related('user')[:20]
    
    # Recent transactions
    recent_transactions = Transaction.objects.select_related('user').order_by('-created_at')[:10]
    
    # Chart data - User registrations over time
    user_chart_data = []
    for i in range(7):
        date = today - timedelta(days=6-i)
        count = User.objects.filter(created_at__date=date).count()
        user_chart_data.append({
            'date': date.strftime('%b %d'),
            'count': count
        })
    
    # Revenue chart data
    revenue_chart_data = []
    for i in range(7):
        date = today - timedelta(days=6-i)
        deposits = Transaction.objects.filter(
            transaction_type='DEPOSIT',
            status='APPROVED',
            created_at__date=date
        ).aggregate(total=Sum('amount'))['total'] or 0
        revenue_chart_data.append({
            'date': date.strftime('%b %d'),
            'amount': float(deposits)
        })
    
    context = {
        'total_users': total_users,
        'new_users_today': new_users_today,
        'new_users_week': new_users_week,
        'active_users': active_users,
        'total_deposits': total_deposits,
        'total_withdrawals': total_withdrawals,
        'pending_deposits': pending_deposits,
        'pending_withdrawals': pending_withdrawals,
        'total_trades': total_trades,
        'active_bots': active_bots,
        'active_copy_trades': active_copy_trades,
        'open_chats': open_chats,
        'unread_messages': unread_messages,
        'recent_activities': recent_activities,
        'recent_transactions': recent_transactions,
        'user_chart_data': json.dumps(user_chart_data),
        'revenue_chart_data': json.dumps(revenue_chart_data),
    }
    
    return render(request, 'custom_admin/dashboard.html', context)


@user_passes_test(is_admin)
def admin_users_list(request):
    """List all users with search and filter"""
    search = request.GET.get('search', '')
    status = request.GET.get('status', 'all')
    
    users = User.objects.all().order_by('-created_at')
    
    if search:
        users = users.filter(
            Q(username__icontains=search) |
            Q(email__icontains=search) |
            Q(legal_first_name__icontains=search) |
            Q(legal_last_name__icontains=search)
        )
    
    if status == 'active':
        users = users.filter(is_active=True)
    elif status == 'inactive':
        users = users.filter(is_active=False)
    elif status == 'withdrawal_approved':
        users = users.filter(withdrawal_approved=True)
    
    context = {
        'users': users,
        'search': search,
        'status': status,
    }
    
    return render(request, 'custom_admin/users_list.html', context)


@user_passes_test(is_admin)
def admin_user_detail(request, user_id):
    """View and edit user details"""
    user = get_object_or_404(User, id=user_id)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'update_balance':
            amount = Decimal(request.POST.get('amount', 0))
            user.account_balance = amount
            user.save()
            
            SystemLog.objects.create(
                level='INFO',
                action='UPDATE_USER_BALANCE',
                description=f'Admin updated balance for {user.username} to ${amount}',
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )
            
            messages.success(request, f'Balance updated to ${amount}')
        
        elif action == 'toggle_withdrawal':
            user.withdrawal_approved = not user.withdrawal_approved
            user.save()
            
            status = 'approved' if user.withdrawal_approved else 'disapproved'
            SystemLog.objects.create(
                level='INFO',
                action='TOGGLE_WITHDRAWAL',
                description=f'Admin {status} withdrawal for {user.username}',
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )
            
            if user.withdrawal_approved:
                try:
                    send_admin_withdrawal_access_email(
                        username=user.username, email=user.email, granted=True,
                        approved_by=request.user.username,
                        when=timezone.now().strftime('%d %b %Y, %H:%M UTC'),
                        admin_url=request.build_absolute_uri(reverse('admin_user_detail', args=[user.id])),
                        logo_url=request.build_absolute_uri(static('platform_app/img/logo-512.png')),
                    )
                except Exception:
                    logger.exception('Withdrawal-access email failed for %s', user.pk)

            messages.success(request, f'Withdrawal {status}')
        
        elif action == 'toggle_active':
            user.is_active = not user.is_active
            user.save()

            status = 'activated' if user.is_active else 'deactivated'
            messages.success(request, f'User {status}')

        elif action == 'update_profile':
            user.legal_first_name = request.POST.get('legal_first_name', user.legal_first_name).strip()
            user.legal_last_name = request.POST.get('legal_last_name', user.legal_last_name).strip()
            user.email = request.POST.get('email', user.email).strip()
            user.preferred_currency = request.POST.get('preferred_currency', user.preferred_currency)
            user.bonus = Decimal(request.POST.get('bonus') or 0)
            user.commission = Decimal(request.POST.get('commission') or 0)
            user.save()

            SystemLog.objects.create(
                level='INFO',
                action='UPDATE_USER_PROFILE',
                description=f'Admin updated profile for {user.username}',
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )

            messages.success(request, 'Profile updated successfully')

        elif action == 'toggle_staff':
            if user.id == request.user.id:
                messages.error(request, "You can't change your own admin status.")
            else:
                user.is_staff = not user.is_staff
                if not user.is_staff:
                    user.is_superuser = False
                user.save()

                status = 'granted' if user.is_staff else 'revoked'
                SystemLog.objects.create(
                    level='WARNING',
                    action='TOGGLE_ADMIN_ACCESS',
                    description=f'Admin {status} admin access for {user.username}',
                    user=request.user,
                    ip_address=request.META.get('REMOTE_ADDR')
                )
                messages.success(request, f'Admin access {status} for {user.username}')

        elif action == 'add_trade':
            instrument = request.POST.get('instrument', '').strip()
            try:
                profit_amount = Decimal(request.POST.get('profit_amount') or 0)
                loss_amount = Decimal(request.POST.get('loss_amount') or 0)
                commission = Decimal(request.POST.get('commission') or 0)
            except Exception:
                messages.error(request, 'Enter valid numbers for profit, loss and commission.')
                return redirect('admin_user_detail', user_id=user_id)

            if not instrument:
                messages.error(request, 'Enter what was traded.')
                return redirect('admin_user_detail', user_id=user_id)

            if profit_amount > 0 and loss_amount > 0:
                messages.error(request, 'Enter either a profit or a loss for this trade, not both.')
                return redirect('admin_user_detail', user_id=user_id)

            profit_loss = profit_amount - loss_amount

            entry = TradeHistory.objects.create(
                user=user,
                instrument=instrument,
                profit_loss=profit_loss,
                commission=commission,
                notes=request.POST.get('notes', '').strip(),
                recorded_by=request.user,
            )

            user.account_balance += entry.net_amount
            user.total_profit += profit_loss
            user.save()

            SystemLog.objects.create(
                level='INFO',
                action='ADD_TRADE_HISTORY',
                description=f'Admin logged a {"profit" if entry.is_profit else "loss"} of ${abs(profit_loss)} on {instrument} for {user.username}',
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )

            messages.success(request, f'Trade entry added for {user.username}')

        elif action == 'delete_trade':
            entry = get_object_or_404(TradeHistory, id=request.POST.get('trade_id'), user=user)

            user.account_balance -= entry.net_amount
            user.total_profit -= entry.profit_loss
            user.save()

            entry.delete()
            messages.success(request, 'Trade entry removed and balance adjusted back')

        elif action == 'delete_user':
            if user.id == request.user.id:
                messages.error(request, "You can't delete your own account.")
                return redirect('admin_user_detail', user_id=user_id)

            username = user.username
            SystemLog.objects.create(
                level='CRITICAL',
                action='DELETE_USER',
                description=f'Admin deleted user account {username} (id={user.id})',
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )
            user.delete()
            messages.success(request, f'User {username} deleted permanently')
            return redirect('admin_users_list')

        return redirect('admin_user_detail', user_id=user_id)
    
    # Get user statistics
    user_transactions = Transaction.objects.filter(user=user).order_by('-created_at')[:20]
    user_trades = Trade.objects.filter(user=user).order_by('-created_at')[:20]
    user_trade_history = TradeHistory.objects.filter(user=user).order_by('-created_at')[:50]
    user_activities = UserActivity.objects.filter(user=user).order_by('-timestamp')[:50]

    context = {
        'user_obj': user,
        'user_transactions': user_transactions,
        'user_trades': user_trades,
        'user_trade_history': user_trade_history,
        'user_activities': user_activities,
    }
    
    return render(request, 'custom_admin/user_detail.html', context)


@user_passes_test(is_admin)
def admin_transactions_list(request):
    """List and manage transactions"""
    status = request.GET.get('status', 'all')
    trans_type = request.GET.get('type', 'all')

    transactions = Transaction.objects.select_related('user').order_by('-created_at')

    if status != 'all':
        transactions = transactions.filter(status=status.upper())

    if trans_type != 'all':
        transactions = transactions.filter(transaction_type=trans_type.upper())

    today = timezone.now().date()
    pending_count = Transaction.objects.filter(status='PENDING').count()
    approved_today = Transaction.objects.filter(status='APPROVED', updated_at__date=today).count()
    total_deposits = Transaction.objects.filter(
        transaction_type='DEPOSIT', status='APPROVED'
    ).aggregate(total=Sum('amount'))['total'] or 0
    total_withdrawals = Transaction.objects.filter(
        transaction_type='WITHDRAWAL', status='APPROVED'
    ).aggregate(total=Sum('amount'))['total'] or 0

    context = {
        'transactions': transactions,
        'status': status,
        'trans_type': trans_type,
        'pending_count': pending_count,
        'approved_today': approved_today,
        'total_deposits': total_deposits,
        'total_withdrawals': total_withdrawals,
    }

    return render(request, 'custom_admin/transactions_list.html', context)


@user_passes_test(is_admin)
def admin_transaction_action(request, transaction_id):
    """Approve or reject transaction"""
    if request.method == 'POST':
        transaction = get_object_or_404(Transaction, id=transaction_id)
        action = request.POST.get('action')
        
        if action == 'approve' and transaction.status == 'PENDING':
            with db_transaction.atomic():
                # Re-read both rows under lock: a double-clicked Approve must
                # not credit a deposit twice, and the balance must be current.
                transaction = Transaction.objects.select_for_update().get(pk=transaction.pk)
                if transaction.status != 'PENDING':
                    messages.error(request, 'This transaction was already processed.')
                    return redirect('admin_transactions_list')
                user = User.objects.select_for_update().get(pk=transaction.user_id)

                if transaction.transaction_type == 'WITHDRAWAL' and user.account_balance < transaction.amount:
                    # Previously this still got marked APPROVED (without deducting
                    # anything), which reads as "go ahead and pay out".
                    messages.error(
                        request,
                        f'Cannot approve: {user.username} only has ${user.account_balance:,.2f}, '
                        f'less than the ${transaction.amount:,.2f} requested. Reject it instead.'
                    )
                    return redirect('admin_transactions_list')

                transaction.status = 'APPROVED'

                if transaction.transaction_type == 'DEPOSIT':
                    user.account_balance += transaction.amount
                    user.total_deposit += transaction.amount
                    user.save()
                elif transaction.transaction_type == 'WITHDRAWAL':
                    user.account_balance -= transaction.amount
                    user.total_withdrawal += transaction.amount
                    user.save()
                elif transaction.transaction_type == 'BOT_PURCHASE' and transaction.bot_plan:
                    UserBotSubscription.objects.get_or_create(
                        user=user, plan=transaction.bot_plan, is_active=True
                    )

                transaction.save()

            SystemLog.objects.create(
                level='INFO',
                action='APPROVE_TRANSACTION',
                description=f'Admin approved {transaction.transaction_type} of ${transaction.amount} for {user.username}',
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )
            
            _notify_transaction_approved(request, transaction, user)
            messages.success(request, 'Transaction approved successfully')
        
        elif action == 'reject':
            transaction.status = 'REJECTED'
            transaction.save()
            
            SystemLog.objects.create(
                level='WARNING',
                action='REJECT_TRANSACTION',
                description=f'Admin rejected {transaction.transaction_type} of ${transaction.amount} for {transaction.user.username}',
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )
            
            messages.success(request, 'Transaction rejected')
        
        return redirect('admin_transactions_list')
    
    return JsonResponse({'error': 'Invalid request'}, status=400)


@user_passes_test(is_admin)
def admin_traders_list(request):
    """Manage copy traders"""
    traders = CopyTrader.objects.all().order_by('-created_at')
    
    context = {'traders': traders}
    return render(request, 'custom_admin/traders_list.html', context)


@user_passes_test(is_admin)
def admin_trader_create(request):
    """Create new copy trader"""
    if request.method == 'POST':
        name = request.POST.get('name')
        country = request.POST.get('country')
        country_flag = request.POST.get('country_flag')
        rating = Decimal(request.POST.get('rating'))
        profit_percentage = Decimal(request.POST.get('profit_percentage'))
        roi = Decimal(request.POST.get('roi'))
        commission = Decimal(request.POST.get('commission'))
        profile_image = request.FILES.get('profile_image')
        
        trader = CopyTrader.objects.create(
            name=name,
            country=country,
            country_flag=country_flag,
            rating=rating,
            profit_percentage_per_day=profit_percentage,
            roi=roi,
            commission_percentage=commission,
            profile_image=profile_image
        )
        
        messages.success(request, f'Trader {name} created successfully')
        return redirect('admin_traders_list')
    
    return render(request, 'custom_admin/trader_form.html', {'action': 'create'})


@user_passes_test(is_admin)
def admin_trader_edit(request, trader_id):
    """Edit copy trader"""
    trader = get_object_or_404(CopyTrader, id=trader_id)
    
    if request.method == 'POST':
        trader.name = request.POST.get('name')
        trader.country = request.POST.get('country')
        trader.country_flag = request.POST.get('country_flag')
        trader.rating = Decimal(request.POST.get('rating'))
        trader.profit_percentage_per_day = Decimal(request.POST.get('profit_percentage'))
        trader.roi = Decimal(request.POST.get('roi'))
        trader.commission_percentage = Decimal(request.POST.get('commission'))
        
        if 'profile_image' in request.FILES:
            trader.profile_image = request.FILES['profile_image']
        
        trader.save()
        
        messages.success(request, 'Trader updated successfully')
        return redirect('admin_traders_list')
    
    context = {'trader': trader, 'action': 'edit'}
    return render(request, 'custom_admin/trader_form.html', context)


@user_passes_test(is_admin)
def admin_trader_delete(request, trader_id):
    """Delete copy trader"""
    if request.method == 'POST':
        trader = get_object_or_404(CopyTrader, id=trader_id)
        trader_name = trader.name
        trader.delete()
        
        messages.success(request, f'Trader {trader_name} deleted successfully')
        return redirect('admin_traders_list')
    
    return JsonResponse({'error': 'Invalid request'}, status=400)


def _save_trader_plan(request, trader, plan):
    """Parse POST data and create/update a CopyTraderPlan.

    Returns an error message string if the save was rejected (e.g. this
    trader already has a plan of that tier), or None on success.
    """
    data = request.POST
    raw_features = data.get('features_text', '')
    features = [line.strip() for line in raw_features.splitlines() if line.strip()]

    tier = data.get('tier', 'SILVER')
    name = data.get('name', '').strip()

    if tier == 'CUSTOM':
        if not name:
            return 'Custom plans need a name - that\'s what tells it apart from the others.'
    else:
        dupe = CopyTraderPlan.objects.filter(trader=trader, tier=tier)
        if plan is not None:
            dupe = dupe.exclude(id=plan.id)
        if dupe.exists():
            return f'{trader.name} already has a "{tier}" plan. Edit that one instead, or choose a different tier.'

    fields = dict(
        trader=trader,
        tier=tier,
        name=name,
        price=Decimal(data.get('price', '0')),
        commission_percentage=Decimal(data.get('commission_percentage', '0')),
        description=data.get('description', '').strip(),
        features=features,
        is_active=bool(data.get('is_active')),
    )

    if plan is None:
        CopyTraderPlan.objects.create(**fields)
    else:
        for key, value in fields.items():
            setattr(plan, key, value)
        plan.save()
    return None


@user_passes_test(is_admin)
def admin_trader_plans_list(request, trader_id):
    """List the tiered plans belonging to one copy trader."""
    trader = get_object_or_404(CopyTrader, id=trader_id)
    plans = trader.plans.all().order_by('price')

    context = {'trader': trader, 'plans': plans}
    return render(request, 'custom_admin/trader_plans_list.html', context)


@user_passes_test(is_admin)
def admin_trader_plan_create(request, trader_id):
    """Create a new plan under a copy trader."""
    trader = get_object_or_404(CopyTrader, id=trader_id)

    if request.method == 'POST':
        error = _save_trader_plan(request, trader, plan=None)
        if error:
            messages.error(request, error)
            context = {'trader': trader, 'plan': None, 'action': 'create', 'tier_choices': CopyTraderPlan.TIER_CHOICES}
            return render(request, 'custom_admin/trader_plan_form.html', context)
        messages.success(request, 'Plan created successfully.')
        return redirect('admin_trader_plans_list', trader_id=trader.id)

    context = {'trader': trader, 'plan': None, 'action': 'create', 'tier_choices': CopyTraderPlan.TIER_CHOICES}
    return render(request, 'custom_admin/trader_plan_form.html', context)


@user_passes_test(is_admin)
def admin_trader_plan_edit(request, trader_id, plan_id):
    """Edit an existing trader plan."""
    trader = get_object_or_404(CopyTrader, id=trader_id)
    plan = get_object_or_404(CopyTraderPlan, id=plan_id, trader=trader)

    if request.method == 'POST':
        error = _save_trader_plan(request, trader, plan=plan)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, f'Plan "{plan.display_name}" updated successfully.')
            return redirect('admin_trader_plans_list', trader_id=trader.id)

    context = {'trader': trader, 'plan': plan, 'action': 'edit', 'tier_choices': CopyTraderPlan.TIER_CHOICES}
    return render(request, 'custom_admin/trader_plan_form.html', context)


@user_passes_test(is_admin)
def admin_trader_plan_delete(request, trader_id, plan_id):
    """Delete a trader plan."""
    if request.method == 'POST':
        plan = get_object_or_404(CopyTraderPlan, id=plan_id, trader_id=trader_id)
        name = plan.display_name
        plan.delete()
        messages.success(request, f'Plan "{name}" deleted successfully.')
        return redirect('admin_trader_plans_list', trader_id=trader_id)
    return JsonResponse({'error': 'Invalid request'}, status=400)


@user_passes_test(is_admin)
def admin_trader_plan_toggle(request, trader_id, plan_id):
    """Toggle active/inactive status of a trader plan."""
    if request.method == 'POST':
        plan = get_object_or_404(CopyTraderPlan, id=plan_id, trader_id=trader_id)
        plan.is_active = not plan.is_active
        plan.save()
        status = 'activated' if plan.is_active else 'deactivated'
        messages.success(request, f'Plan "{plan.display_name}" {status}.')
    return redirect('admin_trader_plans_list', trader_id=trader_id)


@user_passes_test(is_admin)
def admin_bot_plans_list(request):
    """Manage trading bot plans"""
    plans = TradingBotPlan.objects.all().order_by('price')

    context = {'plans': plans}
    return render(request, 'custom_admin/bot_plans_list.html', context)


def _save_bot_plan(request, plan):
    """Parse POST data and create/update a TradingBotPlan.

    Returns an error message string if the save was rejected (e.g. duplicate
    plan_type), or None on success.
    """
    data = request.POST
    raw_features = data.get('features_text', '')
    features = [line.strip() for line in raw_features.splitlines() if line.strip()]

    plan_type = data.get('plan_type', 'BASIC')
    dupe = TradingBotPlan.objects.filter(plan_type=plan_type)
    if plan is not None:
        dupe = dupe.exclude(id=plan.id)
    if dupe.exists():
        return f'A "{plan_type}" plan already exists. Edit that one instead, or choose a different type.'

    fields = dict(
        plan_type=plan_type,
        price=Decimal(data.get('price', '0')),
        daily_profit_percentage=Decimal(data.get('daily_profit_percentage', '0')),
        features=features,
        is_active=bool(data.get('is_active')),
    )

    if plan is None:
        TradingBotPlan.objects.create(**fields)
    else:
        for attr, val in fields.items():
            setattr(plan, attr, val)
        plan.save()
    return None


@user_passes_test(is_admin)
def admin_bot_plan_create(request):
    """Create a new trading bot plan."""
    if request.method == 'POST':
        error = _save_bot_plan(request, plan=None)
        if error:
            messages.error(request, error)
            context = {'plan': None, 'action': 'create', 'plan_choices': TradingBotPlan.PLAN_CHOICES}
            return render(request, 'custom_admin/bot_plan_form.html', context)
        messages.success(request, 'Bot plan created successfully.')
        return redirect('admin_bot_plans_list')

    context = {'plan': None, 'action': 'create', 'plan_choices': TradingBotPlan.PLAN_CHOICES}
    return render(request, 'custom_admin/bot_plan_form.html', context)


@user_passes_test(is_admin)
def admin_bot_plan_edit(request, plan_id):
    """Edit an existing trading bot plan."""
    plan = get_object_or_404(TradingBotPlan, id=plan_id)

    if request.method == 'POST':
        error = _save_bot_plan(request, plan=plan)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, f'Bot plan "{plan.plan_type}" updated successfully.')
            return redirect('admin_bot_plans_list')

    context = {'plan': plan, 'action': 'edit', 'plan_choices': TradingBotPlan.PLAN_CHOICES}
    return render(request, 'custom_admin/bot_plan_form.html', context)


@user_passes_test(is_admin)
def admin_bot_plan_delete(request, plan_id):
    """Delete a trading bot plan."""
    if request.method == 'POST':
        plan = get_object_or_404(TradingBotPlan, id=plan_id)
        plan_type = plan.plan_type
        plan.delete()
        messages.success(request, f'Bot plan "{plan_type}" deleted successfully.')
        return redirect('admin_bot_plans_list')
    return JsonResponse({'error': 'Invalid request'}, status=400)


@user_passes_test(is_admin)
def admin_bot_plan_toggle(request, plan_id):
    """Toggle active/inactive status of a trading bot plan."""
    if request.method == 'POST':
        plan = get_object_or_404(TradingBotPlan, id=plan_id)
        plan.is_active = not plan.is_active
        plan.save()
        status = 'activated' if plan.is_active else 'deactivated'
        messages.success(request, f'Bot plan "{plan.plan_type}" {status}.')
    return redirect('admin_bot_plans_list')


# ── Investment Plans Admin Views ──────────────────────────────────────────────

@user_passes_test(is_admin)
def admin_investment_plans_list(request):
    """List all investment plans with key stats."""
    plans = InvestmentPlan.objects.prefetch_related('subscriptions').order_by('minimum_amount')

    total_investments = UserInvestment.objects.count()
    total_invested    = UserInvestment.objects.aggregate(
        total=Sum('amount_invested')
    )['total'] or Decimal('0')
    active_count      = plans.filter(is_active=True).count()

    context = {
        'plans':             plans,
        'active_count':      active_count,
        'total_investments': total_investments,
        'total_invested':    total_invested,
    }
    return render(request, 'custom_admin/investment_plans_list.html', context)


@user_passes_test(is_admin)
def admin_investment_plan_create(request):
    """Create a new investment plan."""
    if request.method == 'POST':
        error = _save_investment_plan(request, plan=None)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, 'Investment plan created successfully.')
            return redirect('admin_investment_plans_list')

    context = {
        'plan':         None,
        'tier_choices': InvestmentPlan.PLAN_TIER_CHOICES,
    }
    return render(request, 'custom_admin/investment_plan_form.html', context)


@user_passes_test(is_admin)
def admin_investment_plan_edit(request, plan_id):
    """Edit an existing investment plan."""
    plan = get_object_or_404(InvestmentPlan, id=plan_id)

    if request.method == 'POST':
        error = _save_investment_plan(request, plan=plan)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, f'Plan "{plan.name}" updated successfully.')
            return redirect('admin_investment_plans_list')

    context = {
        'plan':         plan,
        'tier_choices': InvestmentPlan.PLAN_TIER_CHOICES,
    }
    return render(request, 'custom_admin/investment_plan_form.html', context)


@user_passes_test(is_admin)
def admin_investment_plan_toggle(request, plan_id):
    """Toggle active/inactive status of an investment plan."""
    if request.method == 'POST':
        plan = get_object_or_404(InvestmentPlan, id=plan_id)
        plan.is_active = not plan.is_active
        plan.save()
        status = 'activated' if plan.is_active else 'deactivated'
        messages.success(request, f'Plan "{plan.name}" {status}.')
    return redirect('admin_investment_plans_list')


@user_passes_test(is_admin)
def admin_investment_plan_delete(request, plan_id):
    """Delete an investment plan."""
    if request.method == 'POST':
        plan = get_object_or_404(InvestmentPlan, id=plan_id)
        if plan.subscriptions.filter(status='ACTIVE').exists():
            messages.error(request, f'Cannot delete "{plan.name}" — it has active subscriptions. Disable it instead.')
        else:
            name = plan.name
            plan.delete()
            messages.success(request, f'Plan "{name}" deleted successfully.')
    return redirect('admin_investment_plans_list')


def _save_investment_plan(request, plan):
    """Parse POST data and create/update an InvestmentPlan.

    Returns an error message string if the save was rejected (e.g. duplicate
    tier), or None on success.
    """
    data = request.POST

    tier = data.get('tier', 'STARTER')
    dupe = InvestmentPlan.objects.filter(tier=tier)
    if plan is not None:
        dupe = dupe.exclude(id=plan.id)
    if dupe.exists():
        return f'A "{tier}" plan already exists. Edit that one instead, or choose a different tier.'

    # Parse features: one per line
    raw_features = data.get('features_text', '')
    features = [line.strip() for line in raw_features.splitlines() if line.strip()]

    # Parse maximum_amount (optional)
    max_raw = data.get('maximum_amount', '').strip()
    maximum_amount = Decimal(max_raw) if max_raw else None

    fields = dict(
        name           = data.get('name', '').strip(),
        tier           = data.get('tier', 'STARTER'),
        description    = data.get('description', '').strip(),
        minimum_amount = Decimal(data.get('minimum_amount', '0')),
        maximum_amount = maximum_amount,
        roi_percentage = Decimal(data.get('roi_percentage', '0')),
        duration_days  = int(data.get('duration_days', 1)),
        risk_level     = data.get('risk_level', 'MEDIUM'),
        features       = features,
        is_active      = bool(data.get('is_active')),
        is_featured    = bool(data.get('is_featured')),
    )

    if plan is None:
        InvestmentPlan.objects.create(**fields)
    else:
        for attr, val in fields.items():
            setattr(plan, attr, val)
        plan.save()
    return None


# ── Support inbox ─────────────────────────────────────────────────────────────

MAX_CHAT_MESSAGE_LENGTH = 2000


def _admin_display_name(user):
    full = f'{user.first_name} {user.last_name}'.strip()
    return full or user.username


def _chat_rows(status='all', limit=200):
    """Conversation list for the inbox (newest activity first)."""
    last = SupportMessage.objects.filter(chat=OuterRef('pk')).order_by('-created_at')
    chats = SupportChat.objects.select_related('user').annotate(
        unread_count=Count('messages', filter=Q(messages__is_read=False, messages__sender_type='USER')),
        last_text=Subquery(last.values('message')[:1]),
        last_sender=Subquery(last.values('sender_type')[:1]),
        last_at=Subquery(last.values('created_at')[:1]),
    ).annotate(sort_at=Coalesce('last_at', 'created_at')).order_by('-sort_at')
    if status == 'active':
        chats = chats.filter(status='ACTIVE')
    elif status == 'closed':
        chats = chats.filter(status='CLOSED')
    elif status == 'unread':
        chats = chats.filter(unread_count__gt=0)
    return [{
        'id': c.id,
        'user_id': c.user_id,
        'username': c.user.username,
        'name': f'{c.user.legal_first_name} {c.user.legal_last_name}'.strip() or c.user.username,
        'email': c.user.email,
        'status': c.status,
        'unread': c.unread_count,
        'last_text': (c.last_text or '')[:140],
        'last_sender': c.last_sender or '',
        'last_at': (c.last_at or c.created_at).isoformat(),
    } for c in chats[:limit]]


def _message_json(m):
    return {
        'id': m.id,
        'sender_type': m.sender_type,
        'sender_name': m.sender_name,
        'message': m.message,
        'created_at': m.created_at.isoformat(),
    }


def _email_customer_about_reply(request, chat, text, is_new_conversation=False):
    """Email the customer that support wrote to them. Never raises.

    Replies in a burst only email once per 5 minutes per chat (the first email already
    brings the customer to the conversation); a brand-new conversation always emails.
    """
    user = chat.user
    if not user.email:
        return
    try:
        if not is_new_conversation and rate_limited(f'chat-user-email:{chat.id}', limit=1, window_seconds=300):
            return
        send_user_support_reply_email(
            user_email=user.email,
            user_name=user.legal_first_name or user.username,
            message_preview=text,
            chat_url=request.build_absolute_uri(reverse('dashboard')),
            is_new_conversation=is_new_conversation,
            logo_url=request.build_absolute_uri(static('platform_app/img/logo-512.png')),
        )
    except Exception:
        logger.exception('Support reply email failed for chat %s', chat.pk)


def _reply_target(chat):
    """The chat a reply must land in so the customer's widget (which only shows their
    ACTIVE chat) actually displays it. Reopens the chat if the customer has no active one."""
    if chat.status == 'ACTIVE':
        return chat
    active = SupportChat.objects.filter(user=chat.user, status='ACTIVE').first()
    if active:
        return active
    chat.status = 'ACTIVE'
    chat.closed_at = None
    chat.save()
    return chat


def _post_support_message(request, chat, text, is_new_conversation=False):
    msg = SupportMessage.objects.create(
        chat=chat, sender_type='SUPPORT', sender_name=_admin_display_name(request.user),
        message=text, is_read=False,
    )
    chat.messages.filter(sender_type='USER', is_read=False).update(is_read=True)
    chat.save()  # bumps updated_at
    _email_customer_about_reply(request, chat, text, is_new_conversation=is_new_conversation)
    return msg


@user_passes_test(is_admin)
def admin_support_chats(request):
    """Inbox: every conversation on the left, the open one on the right."""
    return render(request, 'custom_admin/support_chats.html', {
        'selected_chat_id': None,
        'initial_chats': _chat_rows(),
    })


@user_passes_test(is_admin)
def admin_chat_detail(request, chat_id):
    """Same inbox, opened on one conversation (this is the link inside notification emails)."""
    chat = get_object_or_404(SupportChat, id=chat_id)
    return render(request, 'custom_admin/support_chats.html', {
        'selected_chat_id': chat.id,
        'initial_chats': _chat_rows(),
    })


@user_passes_test(is_admin)
def admin_support_list_json(request):
    return JsonResponse({'chats': _chat_rows(request.GET.get('status', 'all'))})


@user_passes_test(is_admin)
def admin_support_messages_json(request, chat_id):
    """Messages of one conversation. `after` returns only newer ones (for live polling)."""
    chat = get_object_or_404(SupportChat.objects.select_related('user'), id=chat_id)
    try:
        after = int(request.GET.get('after', 0))
    except ValueError:
        after = 0
    # Opening / watching a thread marks the customer's messages as read.
    chat.messages.filter(sender_type='USER', is_read=False).update(is_read=True)
    msgs = chat.messages.filter(id__gt=after).order_by('created_at', 'id')
    return JsonResponse({
        'chat': {
            'id': chat.id, 'status': chat.status, 'username': chat.user.username, 'email': chat.user.email,
            'name': f'{chat.user.legal_first_name} {chat.user.legal_last_name}'.strip() or chat.user.username,
            'user_url': reverse('admin_user_detail', args=[chat.user_id]),
        },
        'messages': [_message_json(m) for m in msgs],
    })


@user_passes_test(is_admin)
def admin_support_send(request, chat_id):
    """Send a reply (JSON). Emails the customer."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request method'}, status=405)
    try:
        text = (json.loads(request.body or b'{}').get('message') or '').strip()
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'Invalid request'}, status=400)
    if not text:
        return JsonResponse({'error': 'Message cannot be empty'}, status=400)
    if len(text) > MAX_CHAT_MESSAGE_LENGTH:
        return JsonResponse({'error': f'Message is too long (max {MAX_CHAT_MESSAGE_LENGTH} characters).'}, status=400)

    chat = get_object_or_404(SupportChat.objects.select_related('user'), id=chat_id)
    chat = _reply_target(chat)
    msg = _post_support_message(request, chat, text)
    return JsonResponse({'success': True, 'chat_id': chat.id, 'message': _message_json(msg)})


@user_passes_test(is_admin)
def admin_support_new(request):
    """Start a conversation with any user (JSON). Always emails them."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request method'}, status=405)
    try:
        data = json.loads(request.body or b'{}')
        user_id = int(data.get('user_id'))
        text = (data.get('message') or '').strip()
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({'error': 'Invalid request'}, status=400)
    if not text:
        return JsonResponse({'error': 'Message cannot be empty'}, status=400)
    if len(text) > MAX_CHAT_MESSAGE_LENGTH:
        return JsonResponse({'error': f'Message is too long (max {MAX_CHAT_MESSAGE_LENGTH} characters).'}, status=400)

    target = get_object_or_404(User, id=user_id)
    chat = SupportChat.objects.filter(user=target, status='ACTIVE').first()
    created = chat is None
    if created:
        chat = SupportChat.objects.create(user=target, status='ACTIVE')
    chat.user = target
    msg = _post_support_message(request, chat, text, is_new_conversation=True)
    return JsonResponse({'success': True, 'chat_id': chat.id, 'message': _message_json(msg)})


@user_passes_test(is_admin)
def admin_support_users_json(request):
    """User search for the "new message" box."""
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'users': []})
    users = User.objects.filter(
        Q(username__icontains=q) | Q(email__icontains=q) |
        Q(legal_first_name__icontains=q) | Q(legal_last_name__icontains=q)
    ).order_by('username')[:8]
    return JsonResponse({'users': [{
        'id': u.id, 'username': u.username, 'email': u.email,
        'name': f'{u.legal_first_name} {u.legal_last_name}'.strip() or u.username,
    } for u in users]})


@user_passes_test(is_admin)
def admin_activities_list(request):
    """View all user activities"""
    user_filter = request.GET.get('user', '')
    action_type = request.GET.get('action', 'all')
    
    activities = UserActivity.objects.select_related('user').order_by('-timestamp')
    
    if user_filter:
        activities = activities.filter(user__username__icontains=user_filter)
    
    if action_type != 'all':
        activities = activities.filter(action_type=action_type.upper())
    
    # Get statistics
    today = timezone.now().date()
    today_count = UserActivity.objects.filter(timestamp__date=today).count()
    
    five_minutes_ago = timezone.now() - timedelta(minutes=5)
    recent_count = UserActivity.objects.filter(timestamp__gte=five_minutes_ago).count()
    
    # Count unique users
    unique_users_count = UserActivity.objects.values('user').distinct().count()
    
    # Limit results for performance
    activities = activities[:200]
    
    context = {
        'activities': activities,
        'user_filter': user_filter,
        'action_type': action_type,
        'today_count': today_count,
        'recent_count': recent_count,
        'unique_users': unique_users_count,
    }
    
    return render(request, 'custom_admin/activities_list.html', context)


@user_passes_test(is_admin)
def admin_settings(request):
    """Platform settings"""
    settings, created = PlatformSettings.objects.get_or_create(id=1)
    wallets = WalletAddress.objects.all()
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'update_settings':
            settings.maintenance_mode = request.POST.get('maintenance_mode') == 'on'
            settings.minimum_withdrawal = Decimal(request.POST.get('minimum_withdrawal'))
            settings.trading_fee_percentage = Decimal(request.POST.get('trading_fee'))
            settings.withdrawal_fee_percentage = Decimal(request.POST.get('withdrawal_fee'))
            settings.copy_trade_auto_invest = request.POST.get('copy_trade_auto_invest') == 'on'
            settings.save()
            
            messages.success(request, 'Settings updated successfully')
        
        elif action == 'add_wallet':
            cryptocurrency = request.POST.get('cryptocurrency')
            wallet_address = request.POST.get('wallet_address')

            if WalletAddress.objects.filter(cryptocurrency=cryptocurrency).exists():
                messages.error(request, f'A {cryptocurrency} wallet already exists. Edit it instead of adding a new one.')
            else:
                WalletAddress.objects.create(
                    cryptocurrency=cryptocurrency,
                    wallet_address=wallet_address
                )
                messages.success(request, f'{cryptocurrency} wallet added successfully')

        return redirect('admin_settings')

    context = {
        'settings': settings,
        'wallets': wallets,
    }

    return render(request, 'custom_admin/settings.html', context)


@user_passes_test(is_admin)
def admin_wallet_edit(request, wallet_id):
    """Edit a company wallet address."""
    wallet = get_object_or_404(WalletAddress, id=wallet_id)
    if request.method == 'POST':
        wallet.wallet_address = request.POST.get('wallet_address', wallet.wallet_address).strip()
        wallet.save()
        messages.success(request, f'{wallet.cryptocurrency} wallet updated successfully.')
    return redirect('admin_settings')


@user_passes_test(is_admin)
def admin_wallet_delete(request, wallet_id):
    """Delete a company wallet address."""
    if request.method == 'POST':
        wallet = get_object_or_404(WalletAddress, id=wallet_id)
        crypto = wallet.cryptocurrency
        wallet.delete()
        messages.success(request, f'{crypto} wallet deleted successfully.')
    return redirect('admin_settings')


@user_passes_test(is_admin)
def admin_wallet_toggle(request, wallet_id):
    """Toggle active/inactive status of a company wallet address."""
    if request.method == 'POST':
        wallet = get_object_or_404(WalletAddress, id=wallet_id)
        wallet.is_active = not wallet.is_active
        wallet.save()
        status = 'activated' if wallet.is_active else 'deactivated'
        messages.success(request, f'{wallet.cryptocurrency} wallet {status}.')
    return redirect('admin_settings')


@user_passes_test(is_admin)
def admin_admins_list(request):
    """List admin/staff accounts and promote/revoke access."""
    admins = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).order_by('-is_superuser', 'username')

    context = {'admins': admins}
    return render(request, 'custom_admin/admins_list.html', context)


@user_passes_test(is_admin)
def admin_promote_user(request):
    """Grant admin (staff) access to an existing user by username or email."""
    if request.method == 'POST':
        identifier = request.POST.get('identifier', '').strip()
        make_superuser = bool(request.POST.get('make_superuser'))

        user = User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier)).first()
        if not user:
            messages.error(request, f'No user found matching "{identifier}".')
        else:
            user.is_staff = True
            if make_superuser:
                user.is_superuser = True
            user.save()

            SystemLog.objects.create(
                level='WARNING',
                action='PROMOTE_ADMIN',
                description=f'Admin granted {"super admin" if make_superuser else "admin"} access to {user.username}',
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )
            messages.success(request, f'{user.username} is now an admin.')

    return redirect('admin_admins_list')


@user_passes_test(is_admin)
def admin_revoke_admin(request, user_id):
    """Revoke admin (staff/superuser) access from a user."""
    if request.method == 'POST':
        user = get_object_or_404(User, id=user_id)
        if user.id == request.user.id:
            messages.error(request, "You can't revoke your own admin access.")
        else:
            user.is_staff = False
            user.is_superuser = False
            user.save()

            SystemLog.objects.create(
                level='WARNING',
                action='REVOKE_ADMIN',
                description=f'Admin revoked admin access from {user.username}',
                user=request.user,
                ip_address=request.META.get('REMOTE_ADDR')
            )
            messages.success(request, f'Admin access revoked for {user.username}.')

    return redirect('admin_admins_list')


# ── Transactions: create / delete ─────────────────────────────────────────────

@user_passes_test(is_admin)
def admin_transaction_create(request):
    """Manually log a transaction for a user (e.g. a correction or bonus credit)."""
    if request.method == 'POST':
        target = request.POST.get('username', '').strip()
        user = User.objects.filter(Q(username__iexact=target) | Q(email__iexact=target)).first()

        if not user:
            messages.error(request, f'No user found matching "{target}".')
            return redirect('admin_transactions_list')

        transaction_type = request.POST.get('transaction_type', 'DEPOSIT')
        amount = Decimal(request.POST.get('amount', '0'))
        status = request.POST.get('status', 'PENDING')
        description = request.POST.get('description', '').strip()

        transaction = Transaction.objects.create(
            user=user,
            transaction_type=transaction_type,
            amount=amount,
            status=status,
            description=description or f'Manually created by admin {request.user.username}',
        )

        # Apply the same balance effects as the approve() flow when created as already-settled.
        if status in ('APPROVED', 'COMPLETED'):
            if transaction_type == 'DEPOSIT':
                user.account_balance += amount
                user.total_deposit += amount
                user.save()
            elif transaction_type == 'WITHDRAWAL' and user.account_balance >= amount:
                user.account_balance -= amount
                user.total_withdrawal += amount
                user.save()
            elif transaction_type in ('BONUS', 'PROFIT'):
                user.account_balance += amount
                user.save()

        if status in ('APPROVED', 'COMPLETED'):
            user.refresh_from_db()
            _notify_transaction_approved(request, transaction, user)

        SystemLog.objects.create(
            level='INFO',
            action='CREATE_TRANSACTION',
            description=f'Admin manually created {transaction_type} of ${amount} for {user.username}',
            user=request.user,
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f'Transaction created for {user.username}.')
    return redirect('admin_transactions_list')


@user_passes_test(is_admin)
def admin_transaction_delete(request, transaction_id):
    """Delete a transaction record. Only allowed while still PENDING, so no
    balance change ever needs to be reversed."""
    if request.method == 'POST':
        transaction = get_object_or_404(Transaction, id=transaction_id)
        if transaction.status != 'PENDING':
            messages.error(request, 'Only pending transactions can be deleted. Reject it first if needed.')
        else:
            transaction.delete()
            messages.success(request, 'Transaction deleted.')
    return redirect('admin_transactions_list')


# ── Live Trades (self-service Buy/Sell/Swap on the Live Trading page) ────────

@user_passes_test(is_admin)
def admin_live_trades_list(request):
    """List every self-service Live Trading swap across all users, read-only."""
    q = request.GET.get('q', '').strip()

    trades = Trade.objects.select_related('user').order_by('-created_at')
    if q:
        trades = trades.filter(
            Q(user__username__icontains=q) | Q(user__email__icontains=q) |
            Q(from_currency__icontains=q) | Q(to_currency__icontains=q)
        )

    total_volume = trades.aggregate(total=Sum('from_amount'))['total'] or Decimal('0')
    total_fees = trades.aggregate(total=Sum('fee'))['total'] or Decimal('0')

    context = {
        'trades': trades[:200],
        'q': q,
        'trades_count': trades.count(),
        'total_volume': total_volume,
        'total_fees': total_fees,
    }
    return render(request, 'custom_admin/live_trades_list.html', context)


# ── Trade History (admin-curated profit/loss log) ─────────────────────────────

@user_passes_test(is_admin)
def admin_trade_history_list(request):
    """List every trade history entry across all users, with an add form."""
    q = request.GET.get('q', '').strip()

    entries = TradeHistory.objects.select_related('user').order_by('-created_at')
    if q:
        entries = entries.filter(Q(user__username__icontains=q) | Q(user__email__icontains=q) | Q(instrument__icontains=q))

    total_profit = entries.filter(profit_loss__gt=0).aggregate(total=Sum('profit_loss'))['total'] or Decimal('0')
    total_loss = entries.filter(profit_loss__lt=0).aggregate(total=Sum('profit_loss'))['total'] or Decimal('0')

    all_users = User.objects.order_by('username').values('id', 'username', 'email')

    context = {
        'entries': entries[:200],
        'q': q,
        'total_profit': total_profit,
        'total_loss': total_loss,
        'entries_count': entries.count(),
        'all_users': list(all_users),
    }
    return render(request, 'custom_admin/trade_history_list.html', context)


@user_passes_test(is_admin)
def admin_trade_history_create(request):
    """Add a trade history entry for a user, picked from the dropdown by id."""
    if request.method == 'POST':
        user = User.objects.filter(id=request.POST.get('user_id')).first()

        if not user:
            messages.error(request, 'Please pick a user from the list.')
            return redirect('admin_trade_history_list')

        instrument = request.POST.get('instrument', '').strip()
        try:
            profit_amount = Decimal(request.POST.get('profit_amount') or 0)
            loss_amount = Decimal(request.POST.get('loss_amount') or 0)
            commission = Decimal(request.POST.get('commission') or 0)
        except Exception:
            messages.error(request, 'Enter valid numbers for profit, loss and commission.')
            return redirect('admin_trade_history_list')

        if not instrument:
            messages.error(request, 'Enter what was traded.')
            return redirect('admin_trade_history_list')

        if profit_amount > 0 and loss_amount > 0:
            messages.error(request, 'Enter either a profit or a loss for this trade, not both.')
            return redirect('admin_trade_history_list')

        profit_loss = profit_amount - loss_amount

        entry = TradeHistory.objects.create(
            user=user,
            instrument=instrument,
            profit_loss=profit_loss,
            commission=commission,
            notes=request.POST.get('notes', '').strip(),
            recorded_by=request.user,
        )

        user.account_balance += entry.net_amount
        user.total_profit += profit_loss
        user.save()

        SystemLog.objects.create(
            level='INFO',
            action='ADD_TRADE_HISTORY',
            description=f'Admin logged a {"profit" if entry.is_profit else "loss"} of ${abs(profit_loss)} on {instrument} for {user.username}',
            user=request.user,
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f'Trade entry added for {user.username}')

    return redirect('admin_trade_history_list')


@user_passes_test(is_admin)
def admin_trade_history_delete(request, entry_id):
    """Delete a trade history entry and reverse its balance effect."""
    if request.method == 'POST':
        entry = get_object_or_404(TradeHistory, id=entry_id)
        user = entry.user

        user.account_balance -= entry.net_amount
        user.total_profit -= entry.profit_loss
        user.save()

        entry.delete()
        messages.success(request, 'Trade entry removed and balance adjusted back')

    return redirect('admin_trade_history_list')


# ── Support chats: close / delete ─────────────────────────────────────────────

@user_passes_test(is_admin)
def admin_chat_close(request, chat_id):
    """Toggle a support chat between ACTIVE and CLOSED."""
    if request.method != 'POST':
        return redirect('admin_support_chats')
    wants_json = request.headers.get('x-requested-with') == 'fetch'
    chat = get_object_or_404(SupportChat, id=chat_id)
    if chat.status == 'ACTIVE':
        chat.status = 'CLOSED'
        chat.closed_at = timezone.now()
    else:
        if SupportChat.objects.filter(user=chat.user, status='ACTIVE').exclude(pk=chat.pk).exists():
            msg = f'{chat.user.username} already has another active chat - close that one first.'
            if wants_json:
                return JsonResponse({'error': msg}, status=409)
            messages.error(request, msg)
            return redirect('admin_chat_detail', chat_id=chat_id)
        chat.status = 'ACTIVE'
        chat.closed_at = None
    chat.save()
    if wants_json:
        return JsonResponse({'ok': True, 'status': chat.status})
    messages.success(request, f'Chat #{chat.id} marked as {chat.status.title()}.')
    return redirect('admin_chat_detail', chat_id=chat_id)


@user_passes_test(is_admin)
def admin_chat_delete(request, chat_id):
    """Permanently delete a support chat and its messages."""
    if request.method == 'POST':
        chat = get_object_or_404(SupportChat, id=chat_id)
        chat.delete()
        if request.headers.get('x-requested-with') == 'fetch':
            return JsonResponse({'ok': True})
        messages.success(request, f'Chat #{chat_id} deleted.')
    return redirect('admin_support_chats')


@user_passes_test(is_admin)
def admin_system_logs(request):
    """View system logs"""
    level = request.GET.get('level', 'all')
    
    logs = SystemLog.objects.select_related('user').order_by('-timestamp')
    
    if level != 'all':
        logs = logs.filter(level=level.upper())
    
    logs = logs[:200]
    
    context = {
        'logs': logs,
        'level': level,
    }
    
    return render(request, 'custom_admin/system_logs.html', context)