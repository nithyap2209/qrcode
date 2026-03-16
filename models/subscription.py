from sqlalchemy.orm import relationship, joinedload
from flask import Blueprint, render_template, redirect, url_for, flash, session, request, jsonify, send_file, current_app
from flask_login import login_required, current_user
from datetime import datetime, timedelta, UTC
from sqlalchemy import or_, func, case
from functools import wraps
from utils.pdf_generator import generate_invoice_pdf
import hmac
import hashlib
import os
from io import BytesIO
import logging
from razorpay import Client
from functools import wraps
import traceback
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from .qr_models import QRCode, Scan
from .database import db
from .user import User, EmailLog
from .payment import Payment, InvoiceAddress
from .usage_log import UsageLog
from app_config import razorpay_client
from flask import current_app
from flask import request


# Initialize blueprint
subscription_bp = Blueprint('subscription', __name__, url_prefix='/subscription')

# Initialize logger
logger = logging.getLogger(__name__)

# Subscription.py changes
class Subscription(db.Model):
    __tablename__ = 'subscriptions'
    
    S_ID = db.Column(db.Integer, primary_key=True)
    plan = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    days = db.Column(db.Integer, nullable=False)
    tier = db.Column(db.Integer, nullable=False)
    features = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)
    plan_type = db.Column(db.String(50), nullable=False, default='Normal')
    # New columns
    design = db.Column(db.Text, nullable=True)  # Comma-separated list of designs
    analytics = db.Column(db.Integer, default=0)  # Integer value for analytics count
    qr_count = db.Column(db.Integer, default=0)  # Integer value for QR code count
    scan_limit = db.Column(db.Integer, default=0)  # Maximum scans allowed
    subscribed_users = relationship("SubscribedUser", back_populates="subscription", overlaps="subscribers")

    def __repr__(self):
        return f"<Subscription {self.plan}>"
        
    @property
    def daily_price(self):
        """Calculate price per day"""
        return self.price / self.days if self.days > 0 else 0

    @property
    def display_plan_type(self):
        return self.plan_type or 'Normal'
    
    # Helper method to get designs as a list
    def get_designs(self):
        """Return list of allowed designs"""
        if not self.design:
            return []
        return [d.strip() for d in self.design.split(',')]
    
    # Check if a specific design is allowed
    def is_design_allowed(self, design_name):
        """Check if a specific design is allowed in this subscription (robustly)."""
        if not self.design:
            return False

        # Normalize the requested design name by removing spaces and underscores
        normalized_design_name = design_name.strip().lower().replace(" ", "").replace("_", "")

        # Normalize the allowed designs from the database in the same way
        allowed_designs = self.get_designs()
        normalized_allowed = [d.strip().lower().replace(" ", "").replace("_", "") for d in allowed_designs]

        return normalized_design_name in normalized_allowed


class SubscribedUser(db.Model):
    __tablename__ = 'subscribed_users'
    
    id = db.Column(db.Integer, primary_key=True)
    U_ID = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    S_ID = db.Column(db.Integer, db.ForeignKey('subscriptions.S_ID'), nullable=False)
    start_date = db.Column(db.DateTime, default=datetime.now(UTC))
    end_date = db.Column(db.DateTime, nullable=False)
    current_usage = db.Column(db.Integer, default=0)  # Keep for backward compatibility
    last_usage_reset = db.Column(db.DateTime, default=datetime.now(UTC))
    is_auto_renew = db.Column(db.Boolean, default=True)
    _is_active = db.Column('is_active', db.Boolean, default=True, nullable=False)
    # New columns for tracking usage
    analytics_used = db.Column(db.Integer, default=0)
    qr_generated = db.Column(db.Integer, default=0)
    scans_used = db.Column(db.Integer, default=0)  # Track total scans used

    # Individual user limit overrides (NULL means use subscription default)
    user_analytics_limit = db.Column(db.Integer, nullable=True)  # User-specific analytics limit
    user_qr_limit = db.Column(db.Integer, nullable=True)  # User-specific QR limit
    user_scan_limit = db.Column(db.Integer, nullable=True)  # User-specific scan limit

    # Expiry notification tracking
    expiry_notification_sent = db.Column(db.Boolean, default=False)  # Track if expiry notification was sent
    expiry_notification_sent_at = db.Column(db.DateTime, nullable=True)  # When the notification was sent

    user = db.relationship('User', backref=db.backref('subscriptions', lazy=True))
    subscription = db.relationship('Subscription', backref=db.backref('subscribers', lazy=True))

    # Properties to get effective limits (user-specific or subscription default)
    @property
    def effective_analytics_limit(self):
        """Get the effective analytics limit for this user"""
        return self.user_analytics_limit if self.user_analytics_limit is not None else self.subscription.analytics

    @property
    def effective_qr_limit(self):
        """Get the effective QR code limit for this user"""
        return self.user_qr_limit if self.user_qr_limit is not None else self.subscription.qr_count

    @property
    def effective_scan_limit(self):
        """Get the effective scan limit for this user"""
        return self.user_scan_limit if self.user_scan_limit is not None else self.subscription.scan_limit

    # Properties to calculate percentages based on effective limits
    @property
    def analytics_percent(self):
        """Calculate analytics usage percentage based on effective limit"""
        if self.effective_analytics_limit <= 0:
            return 0
        return min(100, (self.analytics_used / self.effective_analytics_limit) * 100)

    @property
    def qr_percent(self):
        """Calculate QR code usage percentage based on effective limit"""
        if self.effective_qr_limit <= 0:
            return 0
        return min(100, (self.qr_generated / self.effective_qr_limit) * 100)

    @property
    def scan_percent(self):
        """Calculate scan usage percentage based on effective limit"""
        if self.effective_scan_limit <= 0:
            return 0
        scans_used = self.scans_used or 0
        return min(100, (scans_used / self.effective_scan_limit) * 100)

    def get_start_date(self):
        """Return timezone-aware start date"""
        if self.start_date.tzinfo is None:
            return self.start_date.replace(tzinfo=UTC)
        return self.start_date

    def get_end_date(self):
        """Return timezone-aware end date"""
        if self.end_date.tzinfo is None:
            return self.end_date.replace(tzinfo=UTC)
        return self.end_date

    def get_last_usage_reset(self):
        """Return timezone-aware last usage reset date"""
        if self.last_usage_reset and self.last_usage_reset.tzinfo is None:
            return self.last_usage_reset.replace(tzinfo=UTC)
        return self.last_usage_reset

    @property
    def days_remaining(self):
        """Calculate remaining days using timezone-aware dates"""
        now = datetime.now(UTC)
        end_date = self.get_end_date()
        
        if end_date <= now:
            return 0
        
        remaining_seconds = (end_date - now).total_seconds()
        return max(0, int(remaining_seconds / (24 * 3600)))

    def time_remaining_percent(self):
        """Calculate percent of subscription time remaining"""
        now = datetime.now(UTC)
        start_date = self.get_start_date()
        end_date = self.get_end_date()
        
        # If already expired, return 0
        if end_date <= now:
            return 0

        total_duration = (end_date - start_date).total_seconds()
        time_remaining = (end_date - now).total_seconds()

        if total_duration <= 0:
            return 0

        percent = (time_remaining / total_duration) * 100
        return round(percent)
    
    def remaining_value(self):
        now = datetime.now(UTC)
        start_date = self.start_date.replace(tzinfo=UTC) if self.start_date.tzinfo is None else self.start_date
        end_date = self.end_date.replace(tzinfo=UTC) if self.end_date.tzinfo is None else self.end_date
        
        if end_date <= now:
            return 0
        
        total_days = (end_date - start_date).total_seconds() / (24 * 3600)
        remaining_days = (end_date - now).total_seconds() / (24 * 3600)
        subscription = Subscription.query.get(self.S_ID)
        daily_rate = subscription.price / total_days if total_days > 0 else 0
        
        return daily_rate * remaining_days
    
    @property
    def daily_usage_percent(self):
        if not hasattr(self.subscription, 'usage_per_day') or not self.subscription.usage_per_day:
            return 0
        return min(100, (self.current_usage / self.subscription.usage_per_day) * 100)
    
    @property
    def is_active(self):
        now = datetime.now(UTC)
        # Ensure end_date is timezone-aware before comparison
        end_date = self.end_date.replace(tzinfo=UTC) if self.end_date.tzinfo is None else self.end_date
        return self._is_active and end_date > now

    @is_active.setter
    def is_active(self, value):
        self._is_active = value

    # Helper method to check if a design is allowed for this user
    def is_design_allowed(self, design_name):
        """Check if this subscription allows access to a specific design"""
        return self.subscription.is_design_allowed(design_name)
    
    # Methods for analytics tracking
    def get_analytics_limit(self):
        """Get analytics limit from subscription"""
        return self.subscription.analytics
    
    def get_analytics_remaining(self):
        """Get remaining analytics usage"""
        limit = self.get_analytics_limit()
        return max(0, limit - self.analytics_used)
    
    def increment_analytics(self):
        """Increment analytics usage and return whether successful"""
        if self.analytics_used >= self.subscription.analytics:
            return False
        self.analytics_used += 1
        db.session.commit()
        return True
    
    # Methods for QR code tracking
    def get_qr_limit(self):
        """Get QR code generation limit from subscription"""
        return self.subscription.qr_count
    
    def get_qr_remaining(self):
        """Get remaining QR codes that can be generated"""
        limit = self.get_qr_limit()
        return max(0, limit - self.qr_generated)
    
    def increment_qr(self):
        """Increment QR code usage and return whether successful"""
        if self.qr_generated >= self.subscription.qr_count:
            return False
        self.qr_generated += 1
        db.session.commit()
        return True

    # Methods for scan tracking (keep only non-duplicate methods)
    def get_scan_limit(self):
        """Get scan limit from subscription"""
        return self.effective_scan_limit

    def get_scans_remaining(self):
        """Get remaining scans that can be performed"""
        limit = self.get_scan_limit()
        return max(0, limit - (self.scans_used or 0))

    def increment_scan(self):
        """Increment scan usage and return whether successful"""
        if (self.scans_used or 0) >= self.effective_scan_limit:
            return False
        self.scans_used = (self.scans_used or 0) + 1
        db.session.commit()
        return True

    def can_scan(self):
        """Check if user can perform more scans"""
        return (self.scans_used or 0) < self.effective_scan_limit

    # Properties to check if user has bonus limits
    @property
    def has_analytics_bonus(self):
        """Check if user has bonus analytics limit"""
        return self.user_analytics_limit is not None and self.user_analytics_limit > self.subscription.analytics

    @property
    def analytics_bonus_amount(self):
        """Get the bonus analytics amount"""
        if not self.has_analytics_bonus:
            return 0
        return self.user_analytics_limit - self.subscription.analytics

    @property
    def has_qr_bonus(self):
        """Check if user has bonus QR limit"""
        return self.user_qr_limit is not None and self.user_qr_limit > self.subscription.qr_count

    @property
    def qr_bonus_amount(self):
        """Get the bonus QR amount"""
        if not self.has_qr_bonus:
            return 0
        return self.user_qr_limit - self.subscription.qr_count

    @property
    def has_scan_bonus(self):
        """Check if user has bonus scan limit"""
        return self.user_scan_limit is not None and self.user_scan_limit > self.subscription.scan_limit

    @property
    def scan_bonus_amount(self):
        """Get the bonus scan amount"""
        if not self.has_scan_bonus:
            return 0
        return self.user_scan_limit - self.subscription.scan_limit

    # User-side template helper methods
    def get_effective_analytics_used(self):
        """Get analytics used for user templates"""
        return self.analytics_used

    def get_effective_analytics_limit(self):
        """Get analytics limit for user templates (uses user-specific limit if set)"""
        return self.effective_analytics_limit

    def get_effective_analytics_percent(self):
        """Get analytics percentage for user templates (based on effective limit)"""
        return self.analytics_percent

    def get_effective_qr_used(self):
        """Get QR codes used for user templates"""
        return self.qr_generated

    def get_effective_qr_limit(self):
        """Get QR code limit for user templates (uses user-specific limit if set)"""
        return self.effective_qr_limit

    def get_effective_qr_percent(self):
        """Get QR percentage for user templates (based on effective limit)"""
        return self.qr_percent

    def get_effective_scans_used(self):
        """Get scans used for user templates"""
        return self.scans_used or 0

    def get_effective_scans_limit(self):
        """Get scan limit for user templates (uses user-specific limit if set)"""
        return self.effective_scan_limit

    def get_effective_scans_percent(self):
        """Get scan percentage for user templates (based on effective limit)"""
        return self.scan_percent

    def get_effective_scan_percent(self):
        """Alias for get_effective_scans_percent (based on effective limit)"""
        return self.scan_percent

    def get_effective_scans_remaining(self):
        """Get remaining scans for user templates (based on effective limit)"""
        limit = self.effective_scan_limit or 0
        used = self.scans_used or 0
        return max(0, limit - used)

    @property
    def actual_scans_remaining(self):
        """Get actual remaining scans (alias for get_effective_scans_remaining)"""
        return self.get_effective_scans_remaining()

    @property
    def dynamic_qr_count(self):
        """Get count of dynamic QR codes"""
        return getattr(self, '_dynamic_qr_count', 0)

    @dynamic_qr_count.setter
    def dynamic_qr_count(self, value):
        """Set count of dynamic QR codes"""
        self._dynamic_qr_count = value

    # Methods to modify user-specific limits (without affecting subscription plans)
    def add_analytics_limit(self, count):
        """Add to user-specific analytics limit and return new limit"""
        # Initialize user limit with subscription default if not set
        if self.user_analytics_limit is None:
            self.user_analytics_limit = self.subscription.analytics

        self.user_analytics_limit += count
        if self.user_analytics_limit < 0:
            self.user_analytics_limit = 0
        db.session.commit()
        return self.user_analytics_limit

    def subtract_analytics_limit(self, count):
        """Subtract from user-specific analytics limit and return new limit"""
        # Initialize user limit with subscription default if not set
        if self.user_analytics_limit is None:
            self.user_analytics_limit = self.subscription.analytics

        new_limit = self.user_analytics_limit - count
        if new_limit < 0:
            self.user_analytics_limit = 0
        else:
            self.user_analytics_limit = new_limit
        db.session.commit()
        return self.user_analytics_limit

    def add_qr_limit(self, count):
        """Add to user-specific QR code limit and return new limit"""
        # Initialize user limit with subscription default if not set
        if self.user_qr_limit is None:
            self.user_qr_limit = self.subscription.qr_count

        self.user_qr_limit += count
        if self.user_qr_limit < 0:
            self.user_qr_limit = 0
        db.session.commit()
        return self.user_qr_limit

    def subtract_qr_limit(self, count):
        """Subtract from user-specific QR code limit and return new limit"""
        # Initialize user limit with subscription default if not set
        if self.user_qr_limit is None:
            self.user_qr_limit = self.subscription.qr_count

        new_limit = self.user_qr_limit - count
        if new_limit < 0:
            self.user_qr_limit = 0
        else:
            self.user_qr_limit = new_limit
        db.session.commit()
        return self.user_qr_limit

    def add_scan_limit(self, count):
        """Add to user-specific scan limit and return new limit"""
        # Initialize user limit with subscription default if not set
        if self.user_scan_limit is None:
            self.user_scan_limit = self.subscription.scan_limit

        self.user_scan_limit += count
        if self.user_scan_limit < 0:
            self.user_scan_limit = 0
        db.session.commit()
        return self.user_scan_limit

    def subtract_scan_limit(self, count):
        """Subtract from user-specific scan limit and return new limit"""
        # Initialize user limit with subscription default if not set
        if self.user_scan_limit is None:
            self.user_scan_limit = self.subscription.scan_limit

        new_limit = self.user_scan_limit - count
        if new_limit < 0:
            self.user_scan_limit = 0
        else:
            self.user_scan_limit = new_limit
        db.session.commit()
        return self.user_scan_limit
class SubscriptionHistory(db.Model):
    __tablename__ = 'subscription_history'
    
    id = db.Column(db.Integer, primary_key=True)
    U_ID = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    S_ID = db.Column(db.Integer, db.ForeignKey('subscriptions.S_ID'), nullable=False)
    action = db.Column(db.String(20), nullable=False)
    previous_S_ID = db.Column(db.Integer, db.ForeignKey('subscriptions.S_ID'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now(UTC))
    
    user = relationship("User", backref="subscription_history")
    subscription = relationship("Subscription", foreign_keys=[S_ID])
    previous_subscription = relationship("Subscription", foreign_keys=[previous_S_ID])
    
    def __repr__(self):
        return f"<SubscriptionHistory {self.action} for {self.user.name}>"

def get_razorpay_client():
    """Get Razorpay client instance using app configuration"""
    try:
        return current_app.config['RAZORPAY_CLIENT']
    except KeyError:
        # Fallback to creating new client if not in app config
        return Client(auth=(current_app.config['RAZORPAY_KEY_ID'], current_app.config['RAZORPAY_KEY_SECRET']))

def verify_razorpay_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature, razorpay_key_secret):
    """
    Verify Razorpay payment signature using HMAC SHA-256
    
    Args:
        razorpay_order_id (str): Order ID from Razorpay
        razorpay_payment_id (str): Payment ID from Razorpay
        razorpay_signature (str): Signature from Razorpay
        razorpay_key_secret (str): Razorpay key secret
    
    Returns:
        bool: True if signature is valid, False otherwise
    """
    try:
        # Create signature payload
        payload = f"{razorpay_order_id}|{razorpay_payment_id}"
        
        # Generate expected signature
        generated_signature = hmac.new(
            razorpay_key_secret.encode('utf-8'), 
            payload.encode('utf-8'), 
            hashlib.sha256
        ).hexdigest()
        
        # Use constant-time comparison to prevent timing attacks
        return hmac.compare_digest(generated_signature, razorpay_signature)
    
    except Exception as e:
        current_app.logger.error(f"Signature verification error: {str(e)}")
        return False

def send_payment_confirmation_email(user, payment, subscription):
    """Send payment confirmation email to user with logging"""
    try:
        from flask import current_app, request, url_for
        from .user import EmailLog

        print(f"Attempting to send email to {user.company_email}")
        print(f"Payment amount: {payment.total_amount}")
        print(f"Subscription plan: {subscription.plan}")

        # Validate email configuration
        start_date = datetime.now(UTC)
        end_date = start_date + timedelta(days=subscription.days)

        # FIXED: Get request context safely
        try:
            # Try to get from request context
            host_url = request.host_url if request else "https://your-domain.com/"
            dashboard_url = url_for('dashboard') if request else "/dashboard"
        except RuntimeError:
            # Fallback when outside request context
            host_url = "https://your-domain.com/"
            dashboard_url = "/dashboard"

        # Calculate subscription dates
        subject = f"Payment Confirmation - {subscription.plan} Subscription - QR Dada"

        # Create email body (plain text)
        body = f"""Dear {user.name},

Thank you for your payment! Your subscription to QR Dada has been successfully activated.

PAYMENT DETAILS:
- Payment ID: {payment.razorpay_payment_id or payment.razorpay_order_id}
- Invoice Number: {payment.invoice_number}
- Amount Paid: ₹{payment.total_amount:.2f} {payment.currency}
- Payment Date: {payment.created_at.strftime('%B %d, %Y at %I:%M %p UTC')}
- Payment Status: COMPLETED

SUBSCRIPTION DETAILS:
- Plan: {subscription.plan}
- QR Codes Allowed: {subscription.qr_count} per month
- Analytics Operations: {subscription.analytics} per month
- Subscription Period: {subscription.days} days
- Start Date: {start_date.strftime('%B %d, %Y')}
- End Date: {end_date.strftime('%B %d, %Y')}

WHAT'S NEXT?
✓ Your subscription is now ACTIVE
✓ You can start creating QR codes immediately
✓ Access your dashboard to manage your account
✓ Download your invoice from the subscription page

LOGIN DETAILS:
- Email: {user.company_email}
- Dashboard: {host_url.rstrip('/')}{dashboard_url}

SUPPORT:
If you have any questions or need assistance, please contact our support team.

Thank you for choosing QR DADA!

Best regards,
The QR DADA Team
Website: {host_url}
"""

        # Get logo URL
        try:
            logo_url = url_for('static', filename='images/qr.png', _external=True, _scheme='https') if request else "https://qrdada.com/static/images/qr.png"
        except RuntimeError:
            logo_url = "https://qrdada.com/static/images/qr.png"

        # HTML version
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Payment Confirmation - QR Dada</title>
</head>
<body style="margin: 0; padding: 0; font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8fafc;">
    <table role="presentation" style="width: 100%; border-collapse: collapse; background-color: #f8fafc;">
        <tr>
            <td align="center" style="padding: 40px 20px;">
                <table role="presentation" style="width: 100%; max-width: 600px; border-collapse: collapse; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);">
                    <!-- Logo Header -->
                    <tr>
                        <td align="center" style="padding: 40px 40px 20px 40px; background: linear-gradient(-45deg, #0ea5e9, #8b5cf6); border-radius: 12px 12px 0 0;">
                            <img src="{logo_url}" alt="QR Dada Logo" style="width: 80px; height: auto; display: block; background-color: #ffffff; padding: 10px; border-radius: 8px; margin-bottom: 20px;">
                            <h1 style="margin: 0; font-size: 28px; font-weight: 700; color: #ffffff;">Payment Successful!</h1>
                            <p style="margin: 8px 0 0 0; color: rgba(255, 255, 255, 0.9); font-size: 16px;">Your QR Dada subscription is now active</p>
                        </td>
                    </tr>

                    <!-- Main Content -->
                    <tr>
                        <td style="padding: 40px;">
                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                Hello <strong>{user.name}</strong>,
                            </p>

                            <p style="margin: 0 0 30px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                Thank you for subscribing to QR Dada! Your payment has been successfully processed and your subscription is now active.
                            </p>

                            <!-- Payment Details -->
                            <h3 style="margin: 0 0 15px 0; font-size: 18px; font-weight: 600; color: #1e293b; padding-bottom: 8px; border-bottom: 2px solid #e2e8f0;">Payment Details</h3>
                            <table role="presentation" style="width: 100%; border-collapse: collapse; background-color: #f8fafc; border-radius: 8px; margin-bottom: 25px;">
                                <tr>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0;"><strong style="color: #4a5568;">Payment ID:</strong></td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: right; color: #2d3748;">{payment.razorpay_payment_id or payment.razorpay_order_id}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0;"><strong style="color: #4a5568;">Invoice Number:</strong></td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: right; color: #2d3748;">{payment.invoice_number}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0;"><strong style="color: #4a5568;">Amount Paid:</strong></td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: right; color: #10b981; font-size: 20px; font-weight: bold;">₹{payment.total_amount:.2f}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0;"><strong style="color: #4a5568;">Payment Date:</strong></td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: right; color: #2d3748;">{payment.created_at.strftime('%B %d, %Y at %I:%M %p')}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px;"><strong style="color: #4a5568;">Status:</strong></td>
                                    <td style="padding: 12px; text-align: right;"><span style="background: #10b981; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;">COMPLETED</span></td>
                                </tr>
                            </table>

                            <!-- Subscription Details -->
                            <h3 style="margin: 0 0 15px 0; font-size: 18px; font-weight: 600; color: #1e293b; padding-bottom: 8px; border-bottom: 2px solid #e2e8f0;">Subscription Details</h3>
                            <table role="presentation" style="width: 100%; border-collapse: collapse; background-color: #f8fafc; border-radius: 8px; margin-bottom: 25px;">
                                <tr>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0;"><strong style="color: #4a5568;">Plan:</strong></td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: right; color: #2d3748;">{subscription.plan}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0;"><strong style="color: #4a5568;">QR Codes:</strong></td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: right; color: #2d3748;">{subscription.qr_count} per month</td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0;"><strong style="color: #4a5568;">Analytics:</strong></td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: right; color: #2d3748;">{subscription.analytics} operations</td>
                                </tr>
                                <tr>
                                    <td style="padding: 12px;"><strong style="color: #4a5568;">Valid Until:</strong></td>
                                    <td style="padding: 12px; text-align: right; color: #2d3748;">{end_date.strftime('%B %d, %Y')}</td>
                                </tr>
                            </table>

                            <!-- Get Started -->
                            <h3 style="margin: 0 0 15px 0; font-size: 18px; font-weight: 600; color: #1e293b; padding-bottom: 8px; border-bottom: 2px solid #e2e8f0;">Get Started</h3>
                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                Your subscription is now active! You can start creating QR codes immediately.
                            </p>

                            <!-- CTA Button -->
                            <table role="presentation" style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                                <tr>
                                    <td align="center">
                                        <a href="{host_url.rstrip('/')}{dashboard_url}" style="display: inline-block; padding: 16px 40px; background: linear-gradient(-45deg, #0ea5e9, #8b5cf6); color: #ffffff; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 16px; box-shadow: 0 4px 6px rgba(124, 58, 237, 0.3);">
                                            Access Your Dashboard
                                        </a>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 20px 0 0 0; font-size: 14px; line-height: 1.6; color: #64748b; background-color: #f8fafc; padding: 15px; border-radius: 6px;">
                                <strong style="color: #1e293b;">Your Login Email:</strong> {user.company_email}<br>
                                <strong style="color: #1e293b;">Dashboard URL:</strong> {host_url.rstrip('/')}{dashboard_url}
                            </p>
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px 40px; background-color: #f8fafc; border-radius: 0 0 12px 12px; border-top: 1px solid #e2e8f0;">
                            <p style="margin: 0 0 10px 0; font-size: 14px; color: #64748b; text-align: center;">
                                If you have any questions or need assistance, please contact our support team.
                            </p>

                            <p style="margin: 10px 0; font-size: 14px; color: #64748b; text-align: center;">
                                <strong style="color: #1e293b;">QR Dada</strong> • {host_url}
                            </p>

                            <p style="margin: 15px 0 0 0; font-size: 12px; color: #94a3b8; text-align: center;">
                                © {datetime.now().year} QR Dada. All rights reserved.<br>
                                This is an automated email. Please do not reply.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""

        print("Attempting to send payment email using SMTP...")

        # Generate invoice PDF
        try:
            invoice_buffer = generate_invoice_pdf(payment)
            invoice_pdf = invoice_buffer.getvalue()
            invoice_filename = f"Invoice_{payment.invoice_number}.pdf"
            attachments = [(invoice_pdf, invoice_filename, 'application/pdf')]
            print(f"Invoice PDF generated: {invoice_filename}")
        except Exception as pdf_error:
            print(f"Warning: Failed to generate invoice PDF: {str(pdf_error)}")
            attachments = None

        # Use the new email service for payment emails
        from utils.email_service import email_service
        email_service.send_payment_email(
            to=user.company_email,
            subject=subject,
            body=body,
            html=html,
            attachments=attachments
        )
        print(f"SUCCESS: Payment confirmation email sent to {user.company_email}")
        
        # FIXED: Log successful email with proper error handling
        try:
            EmailLog.log_email(
                recipient_email=user.company_email,
                recipient_name=user.name,
                email_type="payment_confirmation",
                subject=subject,
                user_id=user.id,
                status="sent",
                metadata={
                    "payment_id": payment.razorpay_payment_id,
                    "invoice_number": payment.invoice_number,
                    "amount": float(payment.total_amount),
                    "subscription_plan": subscription.plan,
                    "subscription_days": subscription.days
                }
            )
        except Exception as log_error:
            current_app.logger.error(f"Failed to log email: {str(log_error)}")
            # Don't fail the email sending if logging fails
        
        current_app.logger.info(f"Payment confirmation email sent successfully to {user.company_email} for payment {payment.razorpay_payment_id}")
        print(f"Payment confirmation email sent to {user.company_email}")
        return True
        
    except Exception as e:
        # Log failed email
        try:
            from .user import EmailLog
            EmailLog.log_email(
                recipient_email=user.company_email,
                recipient_name=user.name,
                email_type="payment_confirmation",
                subject=f"Payment Confirmation - {subscription.plan} Subscription - QR Dada",
                user_id=user.id,
                status="failed",
                error_message=str(e),
                metadata={
                    "payment_id": payment.razorpay_payment_id if payment else None,
                    "invoice_number": payment.invoice_number if payment else None,
                    "amount": float(payment.total_amount) if payment else None,
                    "subscription_plan": subscription.plan if subscription else None
                }
            )
        except:
            # If even logging fails, just continue
            pass
        
        error_msg = f"ERROR: Failed to send payment confirmation email: {str(e)}"
        print(error_msg)
        current_app.logger.error(error_msg)
        import traceback
        traceback.print_exc()
        return False


def test_email_configuration():
    """Test email configuration to verify it's working with logging"""
    try:
        from utils.email_service import email_service
        from .user import EmailLog

        print("Testing email configuration...")

        # Send test email using support account
        subject = 'QR Dada - Email Configuration Test'
        body = """This is a test email to verify that email configuration is working correctly.

If you receive this email, your email configuration is set up properly.

Test Details:
- SMTP server connection: OK
- Authentication: OK
- Email sending: OK

You can now send emails to users.
"""

        # Send test email to support account (to self)
        result = email_service.send_support_email(
            to='support@qrdada.com',
            subject=subject,
            body=body
        )

        if not result:
            print("ERROR: Failed to send test email")
            return False

        print("SUCCESS: Test email sent successfully!")
        
        # Log successful test email
        EmailLog.log_email(
            recipient_email='support@qrdada.com',
            recipient_name='System Admin',
            email_type='test_email',
            subject=subject,
            user_id=None,  # No user associated with test email
            status='sent',
            metadata={'test_type': 'configuration_test', 'sent_to_self': True}
        )

        return True

    except Exception as e:
        # Log failed test email
        try:
            from .user import EmailLog
            EmailLog.log_email(
                recipient_email='support@qrdada.com',
                recipient_name='System Admin',
                email_type='test_email',
                subject='QR Dada - Email Configuration Test',
                user_id=None,
                status='failed',
                error_message=str(e),
                metadata={'test_type': 'configuration_test', 'sent_to_self': True}
            )
        except:
            # If even logging fails, just continue
            pass
            
        print(f"ERROR: Email test failed - {str(e)}")
        import traceback
        traceback.print_exc()
        return False
def has_active_subscription(user_id):
    """
    Strict check to ensure only ONE active subscription exists
    - Must be active
    - End date in the future
    - Exactly one active subscription
    """
    active_subs = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .count()
    )
    return active_subs == 1

def fix_multiple_active_subscriptions(user_id):
    """
    Fix issue where a user has multiple active subscriptions by keeping only the most recent one
    """
    active_subs = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .order_by(SubscribedUser.start_date.desc())  # Most recent first
        .all()
    )
    
    if len(active_subs) > 1:
        # Keep the first (newest) subscription active and mark others as inactive
        for i, sub in enumerate(active_subs):
            if i > 0:  # Skip the first one
                sub._is_active = False
        
        db.session.commit()
        return True
    
    return False

def deactivate_expired_subscriptions(user_id):
    """
    Make sure all expired subscriptions are marked as inactive
    """
    now = datetime.now(UTC)
    expired_subs = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date <= now)
        .filter(SubscribedUser._is_active == True)
        .all()
    )
    
    if expired_subs:
        for sub in expired_subs:
            sub._is_active = False
        
        db.session.commit()
        return True
    
    return False

# Helper function to increment usage with daily reset
def increment_usage(user_id):
    sub = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .first()
    )
    
    if sub:
        # Check if we need to reset the usage counter (new day)
        today = datetime.now(UTC).date()
        last_reset_date = getattr(sub, 'last_usage_reset', None)
        
        if not last_reset_date or last_reset_date.date() < today:
            # Reset counter for new day
            sub.current_usage = 0
            sub.last_usage_reset = datetime.now(UTC)
        
        # Increment usage
        sub.current_usage += 1
        db.session.commit()
        
        # Check if daily limit reached
        if sub.current_usage > sub.subscription.usage_per_day:
            return False
    
    return True

def record_usage_log(user_id, subscription_id, operation_type, details=None):
    """
    Record a usage log entry for a subscription
    
    Args:
        user_id (int): ID of the user
        subscription_id (int): ID of the SubscribedUser record (not the subscription plan ID)
        operation_type (str): Type of operation performed (e.g., 'url_analysis', 'keyword_search')
        details (str, optional): Additional details about the operation in JSON format
    
    Returns:
        bool: True if recording succeeded, False otherwise
    """
    try:
        # Create new usage log entry
        usage_log = UsageLog(
            user_id=user_id,
            subscription_id=subscription_id,
            operation_type=operation_type,
            details=details,
            timestamp=datetime.now(UTC)
        )
        
        db.session.add(usage_log)
        db.session.commit()
        return True
        
    except Exception as e:
        current_app.logger.error(f"Error recording usage log: {str(e)}")
        db.session.rollback()
        return False

def subscription_required(operation_type='general'):
    """
    Decorator to check if a user has an active subscription with available usage
    
    Args:
        operation_type (str): Type of operation ('general', 'analytics', 'qr_code')
        
    Returns:
        Function: Decorated function that checks subscription before execution
    """
    def decorator(f):
        @wraps(f)
        def wrap(*args, **kwargs):
            try:
                # Log function being decorated
                current_app.logger.info(f"Checking subscription for route: {f.__name__} (operation: {operation_type})")
                
                # Check if user is logged in
                if 'user_id' not in session:
                    flash("You need to log in first.", "warning")
                    return redirect(url_for('login'))
                
                user_id = session.get('user_id')
                
                # First, check if the user has an active subscription
                now = datetime.now(UTC)
                active_subscription = (
                    SubscribedUser.query
                    .filter(SubscribedUser.U_ID == user_id)
                    .filter(SubscribedUser.end_date > now)
                    .filter(SubscribedUser._is_active == True)
                    .first()
                )
                
                if not active_subscription:
                    current_app.logger.warning(f"User {user_id} attempted to access premium feature without subscription")
                    flash("You need an active subscription to access this feature.", "warning")
                    return redirect(url_for('subscription.user_subscriptions'))
                
                # Check operation type and increment usage accordingly
                usage_result = True
                
                if operation_type == 'analytics':
                    usage_result = increment_analytics_usage(user_id)
                    if not usage_result:
                        flash("You have reached your analytics usage limit for this subscription plan.", "warning")
                        return redirect(url_for('subscription.user_subscriptions'))
                        
                elif operation_type == 'qr_code':
                    usage_result = increment_qr_usage(user_id)
                    if not usage_result:
                        flash("You have reached your QR code generation limit for this subscription plan.", "warning")
                        return redirect(url_for('subscription.user_subscriptions'))
                        
                else:
                    # Legacy usage tracking for backward compatibility
                    usage_result = increment_usage(user_id)
                    if not usage_result:
                        flash("Daily usage limit reached for your subscription plan.", "warning")
                        return redirect(url_for('subscription.user_subscriptions'))
                
                current_app.logger.info(f"Usage increment result: {usage_result}")
                
                # Record this usage in the log
                record_usage_log(
                    user_id=user_id,
                    subscription_id=active_subscription.id,  # Note: this is the SubscribedUser.id, not S_ID
                    operation_type=operation_type,
                    details=f"Accessed {f.__name__}"
                )
                
                # If all checks pass, execute the decorated function
                return f(*args, **kwargs)
            
            except Exception as e:
                # Comprehensive error logging
                current_app.logger.error(f"Subscription check error in {f.__name__}: {e}")
                import traceback
                current_app.logger.error(traceback.format_exc())
                flash("Subscription verification failed. Please try again later.", "danger")
                return redirect(url_for('subscription.user_subscriptions'))
        
        return wrap
    return decorator

def process_auto_renewals():
    """Process auto-renewals for expiring subscriptions"""
    # Get subscriptions expiring in the next 24 hours with auto-renew enabled
    now = datetime.now(UTC)
    expiring_soon = (
        SubscribedUser.query
        .filter(SubscribedUser.is_auto_renew == True)
        .filter(SubscribedUser._is_active == True)  # Only active subscriptions
        .filter(SubscribedUser.end_date <= now + timedelta(days=1))
        .filter(SubscribedUser.end_date > now)
        .options(joinedload(SubscribedUser.subscription))
        .all()
    )
    
    for sub in expiring_soon:
        try:
            # Deactivate current subscription before renewal
            sub._is_active = False
            
            # Get subscription details
            subscription = sub.subscription
            
            # Create Razorpay order for renewal
            payment = Payment(
                base_amount=subscription.price,
                user_id=sub.U_ID,
                subscription_id=sub.S_ID,
                razorpay_order_id=None,  # Will be set by Razorpay
                status='created',
                payment_type='renewal'
            )
            
            # Create Razorpay order
            
            razorpay_client = get_razorpay_client()
            razorpay_order = razorpay_client.order.create({
                'amount': int(payment.total_amount * 100),
                'currency': 'INR',
                'payment_capture': '1'
            })
            
            # Update with Razorpay order ID
            payment.razorpay_order_id = razorpay_order['id']
            db.session.add(payment)
            db.session.commit()
            
            # Send email notification to user about upcoming renewal
            # (implementation depends on your email system)
            
        except Exception as e:
            current_app.logger.error(f"Auto-renewal failed for user {sub.U_ID}: {str(e)}")
    
    # Handle expired subscriptions
    expired = (
        SubscribedUser.query
        .filter(SubscribedUser._is_active == True)  # Only active subscriptions
        .filter(SubscribedUser.end_date < now)
        .all()
    )
    
    for sub in expired:
        # Set subscription as inactive
        sub._is_active = False
        
        # Add history entry for expired subscription
        history_entry = SubscriptionHistory(
            U_ID=sub.U_ID,
            S_ID=sub.S_ID,
            action='expire',
            previous_S_ID=sub.S_ID,
            created_at=now
        )
        db.session.add(history_entry)
    
    db.session.commit()

def generate_invoice_pdf(payment):
    """
    Generate a modern, visually aesthetic PDF invoice for a specific payment
    
    :param payment: Payment model instance
    :return: BytesIO buffer containing the PDF
    """
    from io import BytesIO
    import os
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import inch, mm
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
    from num2words import num2words

    # Define brand colors to match the logo
    brand_color = colors.Color(0.73, 0.20, 0.04)  # Rust/orange color from logo
    secondary_color = colors.Color(0.95, 0.95, 0.95)  # Light gray for backgrounds
    text_color = colors.Color(0.25, 0.25, 0.25)  # Dark gray for text

    # Prepare buffer and document with reduced margins
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=A4, 
        leftMargin=12*mm, 
        rightMargin=12*mm, 
        topMargin=12*mm, 
        bottomMargin=12*mm
    )
    width, height = A4
    
    # Create custom styles
    brand_title_style = ParagraphStyle(
        name='BrandTitleCustom',
        fontName='Helvetica-Bold',
        fontSize=16,
        textColor=brand_color,
        spaceAfter=3,
        alignment=TA_CENTER
    )
    
    company_name_style = ParagraphStyle(
        name='CompanyNameCustom',
        fontName='Helvetica-Bold',
        fontSize=12,
        textColor=text_color,
        spaceAfter=2
    )
    
    invoice_title_style = ParagraphStyle(
        name='InvoiceTitleCustom',
        fontName='Helvetica-Bold',
        fontSize=16,
        alignment=TA_RIGHT,
        textColor=brand_color,
        spaceAfter=4
    )
    
    section_title_style = ParagraphStyle(
        name='SectionTitleCustom',
        fontName='Helvetica-Bold',
        fontSize=9,
        textColor=text_color,
        spaceAfter=2
    )
    
    normal_style = ParagraphStyle(
        name='NormalCustom',
        fontName='Helvetica',
        fontSize=8,
        textColor=text_color,
        leading=10
    )
    
    right_aligned_style = ParagraphStyle(
        name='RightAlignedCustom',
        fontName='Helvetica',
        fontSize=9,
        alignment=TA_RIGHT,
        textColor=text_color
    )
    
    center_aligned_style = ParagraphStyle(
        name='CenterAlignedCustom',
        fontName='Helvetica',
        fontSize=9,
        alignment=TA_CENTER,
        textColor=text_color
    )

    # Prepare elements
    elements = []
    
    # Logo and Title side by side
    logo_path = os.path.join('assert', '4d-logo.webp')
    
    try:
        logo = Image(logo_path, width=1.5*inch, height=0.75*inch)
        header_data = [[
            logo, 
            Paragraph("TAX INVOICE", invoice_title_style)
        ]]
        
        header_table = Table(header_data, colWidths=[doc.width/2, doc.width/2])
        header_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        elements.append(header_table)
    except:
        # Fallback if logo not found
        elements.append(Paragraph("TAX INVOICE", invoice_title_style))
    
    elements.append(Spacer(1, 5))
    
    # Company Details Section
    company_details = [
        [Paragraph("<b>Company Name:</b>", section_title_style)],
        [Paragraph("M/s. Fourth Dimension Media Solutions Pvt Ltd", normal_style)],
        [Paragraph("State & Code: Tamil Nadu (33)", normal_style)],
        [Paragraph("GSTIN: 33AABCF6993P1ZY", normal_style)],
        [Paragraph("PAN: AABCF6993P", normal_style)],
        [Paragraph("CIN: U22130TN2011PTC079276", normal_style)]
    ]
    
    company_table = Table(company_details, colWidths=[doc.width])
    company_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1)
    ]))
    elements.append(company_table)
    elements.append(Spacer(1, 5))
    
    # Bill To and Invoice Details Section (two columns)
    # Get customer details from payment object
    if payment.invoice_address:
        addr = payment.invoice_address
        bill_to_content = [
            [Paragraph("<b>Bill To,</b>", section_title_style)],
            [Paragraph(f"M/s. {addr.company_name or addr.full_name}", normal_style)],
            [Paragraph(f"{addr.street_address}", normal_style)],
            [Paragraph(f"{addr.city} - {addr.postal_code}", normal_style)],
            [Paragraph(f"{addr.state}, India", normal_style)],
            [Paragraph(f"GST No. {addr.gst_number or 'N/A'}", normal_style)],
            [Paragraph(f"PAN No. {addr.pan_number or 'N/A'}", normal_style)]
        ]
    else:
        user = payment.user
        bill_to_content = [
            [Paragraph("<b>Bill To,</b>", section_title_style)],
            [Paragraph(f"M/s. {user.name}", normal_style)],
            [Paragraph(f"Email: {user.company_email}", normal_style)]
        ]
    
    # Invoice details
    invoice_details_content = [
        [Paragraph(f"<b>Invoice No:</b> {payment.invoice_number}", normal_style)],
        [Paragraph(f"<b>Date:</b> {payment.invoice_date.strftime('%d/%m/%Y')}", normal_style)],
        [Spacer(1, 5)],
        [Paragraph(f"<b>Reverse Charge (Yes/No):</b> No", normal_style)],
        [Paragraph(f"<b>Place of supply:</b> Tamil Nadu (33)", normal_style)]
    ]
    
    # Create two-column layout for bill to and invoice details
    bill_invoice_data = [[
        Table(bill_to_content),
        Table(invoice_details_content)
    ]]
    
    bill_invoice_table = Table(bill_invoice_data, colWidths=[doc.width*0.6, doc.width*0.4])
    bill_invoice_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, 0), 'LEFT')
    ]))
    elements.append(bill_invoice_table)
    elements.append(Spacer(1, 8))
    
    # Service Details Table
    # Table headers
    headers = ['Sl No', 'Description of Service', 'SAC/HSN', 'Qty', 'Rate', 'Amount (Rs)']
    
    # Calculate amounts
    base_amount = payment.base_amount
    cgst_rate = payment.gst_rate / 2
    sgst_rate = payment.gst_rate / 2
    cgst_amount = payment.gst_amount / 2
    sgst_amount = payment.gst_amount / 2
    total_amount = payment.total_amount
    
    # Build table data
    table_data = []
    table_data.append(headers)
    
    # Service row
    table_data.append([
        '1.',
        f'Digital Service - {payment.subscription.plan}',
        '998314',
        '1',
        f'{base_amount:.2f}',
        f'{base_amount:.2f}'
    ])
    
    # Totals
    table_data.append(['', '', '', '', 'Total', f'{base_amount:.2f}'])
    table_data.append(['', '', '', '', f'CGST @ {cgst_rate*100:.0f}%', f'{cgst_amount:.2f}'])
    table_data.append(['', '', '', '', f'SGST @ {sgst_rate*100:.0f}%', f'{sgst_amount:.2f}'])
    
    # Create service table
    col_widths = [doc.width*0.08, doc.width*0.35, doc.width*0.12, doc.width*0.08, doc.width*0.17, doc.width*0.2]
    service_table = Table(table_data, colWidths=col_widths)
    
    service_table.setStyle(TableStyle([
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), brand_color),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
        ('TOPPADDING', (0, 0), (-1, 0), 4),
        
        # Data rows
        ('ALIGN', (0, 1), (0, -1), 'CENTER'),  # Sl No
        ('ALIGN', (1, 1), (1, -1), 'LEFT'),    # Description
        ('ALIGN', (2, 1), (2, -1), 'CENTER'),  # SAC/HSN
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),  # Qty
        ('ALIGN', (4, 1), (4, -1), 'RIGHT'),   # Rate
        ('ALIGN', (5, 1), (5, -1), 'RIGHT'),   # Amount
        
        # Borders
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('TOPPADDING', (0, 1), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 3),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        
        # Total rows have special formatting
        ('FONTNAME', (4, 2), (5, -1), 'Helvetica-Bold'),
    ]))
    
    elements.append(service_table)
    
    # Total Invoice Value
    total_table_data = [
        ['Total Invoice Value', f'{total_amount:.2f}']
    ]
    
    total_table = Table(total_table_data, colWidths=[doc.width*0.8, doc.width*0.2])
    total_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), secondary_color),
        ('TEXTCOLOR', (0, 0), (-1, -1), brand_color),
        ('ALIGN', (0, 0), (0, 0), 'RIGHT'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.grey),
        ('RIGHTPADDING', (1, 0), (1, 0), 10),
    ]))
    elements.append(total_table)
    elements.append(Spacer(1, 5))
    
    # Rupees in words
    amount_words = num2words(int(total_amount), lang='en_IN').title()
    words_data = [[f'Rupees in words: {amount_words} Rupees Only']]

    words_table = Table(words_data, colWidths=[doc.width])
    words_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(words_table)
    elements.append(Spacer(1, 15))
    
    # Signature area
    signature_data = [
        ['', 'For Fourth Dimension Media Solutions (P) Ltd'],
        ['', ''],
        ['', 'Authorised Signatory']
    ]
    
    signature_table = Table(signature_data, colWidths=[doc.width*0.6, doc.width*0.4])
    signature_table.setStyle(TableStyle([
        ('ALIGN', (1, 0), (1, -1), 'CENTER'),
        ('FONTNAME', (1, 0), (1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (1, 0), (1, -1), 9),
    ]))
    elements.append(signature_table)
    elements.append(Spacer(1, 15))
    
    # Terms & Conditions and Bank Details
    terms_conditions = [
        [Paragraph("<b>Terms & Condition</b>", section_title_style)],
        [Paragraph("• All disputes are subject to Chennai Jurisdiction only", normal_style)],
        [Paragraph('• Kindly Make all payments favoring "Fourth Dimension Media Solutions Pvt Ltd"', normal_style)],
        [Paragraph("• Payment terms: Immediate", normal_style)],
        [Paragraph("• Bank Name: City Union Bank., Tambaram West, Chennai -45", normal_style)],
        [Paragraph("  Account No: 512120020019966", normal_style)],
        [Paragraph("  Account Type: OD", normal_style)],
        [Paragraph("  IFSC Code: CIUB0000117", normal_style)]
    ]
    
    terms_table = Table(terms_conditions, colWidths=[doc.width])
    terms_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
        ('FONTSIZE', (0, 1), (-1, -1), 7),  # Smaller font for terms
    ]))
    elements.append(terms_table)
    
    # Build PDF
    doc.build(elements)
    
    # Reset buffer position
    buffer.seek(0)
    
    return buffer
# Add this helper function to subscription.py

def can_create_dynamic_qr(user_id):
    """
    Check if a user can create dynamic QR codes based on their subscription
    
    Args:
        user_id (int): ID of the user
        
    Returns:
        bool: True if the user can create dynamic QR codes, False otherwise
    """
    try:
        # Get the user's active subscription
        active_subscription = (
            SubscribedUser.query
            .filter(SubscribedUser.U_ID == user_id)
            .filter(SubscribedUser.end_date > datetime.now(UTC))
            .filter(SubscribedUser._is_active == True)
            .first()
        )
        
        if not active_subscription:
            return False
            
        subscription_plan = active_subscription.subscription
        
        plan_type_lower = subscription_plan.plan_type.lower()
        plan_name_lower = subscription_plan.plan.lower()

        # Allow dynamic QR codes if:
        # 1. Plan type is specifically "Dynamic" (case insensitive), OR
        # 2. Plan type is not "Normal" and tier >= 2, OR
        # 3. Plan name contains "dynamic" (case insensitive)
        return (
            plan_type_lower == 'dynamic' or
            'dynamic' in plan_name_lower or
            (plan_type_lower != 'normal' and subscription_plan.tier >= 2)
        )
        
    except Exception as e:
        current_app.logger.error(f"Error checking dynamic QR access: {str(e)}")
        return False

# Update the SubscribedUser model to include a method for checking dynamic QR access
# Add this method to the SubscribedUser class:

def can_create_dynamic_qr_codes(self):
    """
    Check if this subscription allows creating dynamic QR codes
    
    Returns:
        bool: True if dynamic QR codes are allowed, False otherwise
    """
    # Check if subscription is active and not expired
    if not self.is_active:
        return False
        
    subscription_plan = self.subscription
    
    subscription_plan = self.subscription
    plan_type_lower = subscription_plan.plan_type.lower()
    plan_name_lower = subscription_plan.plan.lower()

    # Allow dynamic QR codes if:
    # 1. Plan type is specifically "Dynamic" (case insensitive), OR
    # 2. Plan type is not "Normal" and tier >= 2, OR
    # 3. Plan name contains "dynamic" (case insensitive)
    return (
        plan_type_lower == 'dynamic' or
        'dynamic' in plan_name_lower or
        (plan_type_lower != 'normal' and subscription_plan.tier >= 2)
    )

# You can also add this method to the Subscription model for direct checking:

def allows_dynamic_qr(self):
    """
    Check if this subscription plan allows dynamic QR codes

    Returns:
        bool: True if dynamic QR codes are allowed, False otherwise
    """
    plan_type_lower = self.plan_type.lower()
    plan_name_lower = self.plan.lower()

    # Allow dynamic QR codes if:
    # 1. Plan type is specifically "Dynamic" (case insensitive), OR
    # 2. Plan type is not "Normal" and tier >= 2, OR
    # 3. Plan name contains "dynamic" (case insensitive)
    return (
        plan_type_lower == 'dynamic' or
        'dynamic' in plan_name_lower or
        (plan_type_lower != 'normal' and self.tier >= 2)
    )


def get_dynamic_qr_codes(user_id):
    """Get all dynamic QR codes for a user."""
    qr_codes = QRCode.query.filter_by(user_id=user_id).all()
    dynamic_qr_codes = []
    
    for qr_code in qr_codes:
        if hasattr(qr_code, 'is_dynamic') and qr_code.is_dynamic:
            dynamic_qr_codes.append(qr_code)
    
    return dynamic_qr_codes

def calculate_actual_analytics_usage(user_id):
    """Calculate actual analytics usage by counting scans of dynamic QR codes."""
    dynamic_qr_codes = get_dynamic_qr_codes(user_id)
    
    total_scans = 0
    for qr_code in dynamic_qr_codes:
        scans = Scan.query.filter_by(qr_code_id=qr_code.id).count()  # Use count() for efficiency
        total_scans += scans
    
    return total_scans

def get_analytics_usage_data(user_id, subscription):
    """Get comprehensive analytics usage data with actual counts."""
    if not subscription or not subscription.subscription.analytics:
        return {
            'analytics_used': 0,
            'analytics_remaining': 0,
            'analytics_total': 0,
            'analytics_percent': 0,
            'dynamic_qr_count': 0
        }
    
    # Get actual analytics operations
    total_analytics_operations = calculate_actual_analytics_usage(user_id)
    dynamic_qr_count = len(get_dynamic_qr_codes(user_id))
    
    analytics_total = subscription.subscription.analytics
    analytics_remaining = max(0, analytics_total - total_analytics_operations)
    analytics_percent = min(100, (total_analytics_operations / analytics_total) * 100) if analytics_total > 0 else 0
    
    return {
        'analytics_used': total_analytics_operations,
        'analytics_remaining': analytics_remaining,
        'analytics_total': analytics_total,
        'analytics_percent': analytics_percent,
        'dynamic_qr_count': dynamic_qr_count
    }
# Add these functions to subscription.py

def send_subscription_expiry_notification(user, subscription, hours_remaining=24):
    """
    Send subscription expiry notification email to user with logging
    """
    try:
        from utils.email_service import email_service
        from .user import EmailLog
        from flask import url_for, request, current_app, has_request_context
        import os

        print(f"Sending expiry notification to: {user.company_email}")
        print(f"Subscription expires: {subscription.end_date}")
        print(f"Hours remaining: {hours_remaining}")

        # Get subscription plan details
        plan = subscription.subscription

        # Create email message
        subject = f"Your {plan.plan} Subscription Expires in {hours_remaining} Hours - QR Dada"

        # Get base URL - use config or environment variable for scheduler context
        base_url = os.getenv('SITE_URL', 'https://qrdada.com')
        if has_request_context():
            # Get from request context if available
            base_url = request.host_url.rstrip('/')

        # Generate renewal URL
        if has_request_context():
            renewal_url = url_for('subscription.user_subscriptions', _external=True)
        else:
            # No request context, build URL manually
            renewal_url = f"{base_url}/subscription/subscriptions"

        # Email body content (keeping your existing content)
        body = f"""Dear {user.name},

This is a friendly reminder that your QR Dada subscription is about to expire.

SUBSCRIPTION DETAILS:
═══════════════════════════════════════════════════════════════
Plan: {plan.plan}
Expires: {subscription.end_date.strftime('%B %d, %Y at %I:%M %p UTC')}
Time Remaining: {hours_remaining} hours

CURRENT USAGE:
═══════════════════════════════════════════════════════════════
QR Codes Used: {subscription.qr_generated}/{plan.qr_count}
Analytics Used: {subscription.analytics_used}/{plan.analytics}

WHAT HAPPENS NEXT?
═══════════════════════════════════════════════════════════════
• Your subscription will expire in {hours_remaining} hours
• After expiry, you won't be able to create new QR codes
• Your existing QR codes will continue to work
• You can renew anytime to continue using premium features

RENEW NOW:
═══════════════════════════════════════════════════════════════
To continue using QR Dada without interruption, please renew your subscription:

{renewal_url}

SUPPORT:
═══════════════════════════════════════════════════════════════
If you have any questions or need assistance, please contact our support team.

Thank you for using QR Dada!

Best regards,
The QR Dada Team
Website: {base_url}
"""

        # HTML version - matching the verification email template style
        logo_url = "https://qrdada.com/static/images/qr.png"
        html = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Subscription Expiry Reminder - QR Dada</title>
</head>
<body style="margin: 0; padding: 0; font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8fafc;">
    <table role="presentation" style="width: 100%; border-collapse: collapse; background-color: #f8fafc;">
        <tr>
            <td align="center" style="padding: 40px 20px;">
                <table role="presentation" style="width: 100%; max-width: 600px; border-collapse: collapse; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);">
                    <!-- Logo Header -->
                    <tr>
                        <td align="center" style="padding: 40px 40px 20px 40px; background: linear-gradient(-45deg, #0ea5e9, #8b5cf6); border-radius: 12px 12px 0 0;">
                            <img src="{logo_url}" alt="QR Dada Logo" style="width: 80px; height: auto; display: block; background-color: #ffffff; padding: 10px; border-radius: 8px;">
                        </td>
                    </tr>

                    <!-- Main Content -->
                    <tr>
                        <td style="padding: 40px;">
                            <h1 style="margin: 0 0 20px 0; font-size: 28px; font-weight: 700; color: #1e293b; text-align: center;">
                                Subscription Expiring Soon
                            </h1>

                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                Hi <strong>{user.name}</strong>,
                            </p>

                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                This is a friendly reminder that your <strong>{plan.plan}</strong> subscription is about to expire. To continue enjoying all premium features without interruption, please renew your subscription.
                            </p>

                            <!-- Warning Box -->
                            <div style="margin: 30px 0; padding: 20px; background-color: #fef3c7; border-left: 4px solid #f59e0b; border-radius: 6px;">
                                <p style="margin: 0; font-size: 14px; line-height: 1.6; color: #92400e;">
                                    <strong>⏰ Time Remaining:</strong> {hours_remaining} hours<br>
                                    <strong>Expires:</strong> {subscription.end_date.strftime('%B %d, %Y at %I:%M %p')}
                                </p>
                            </div>

                            <!-- Subscription Details Box -->
                            <div style="margin: 20px 0; padding: 20px; background-color: #f1f5f9; border-radius: 8px;">
                                <h3 style="margin: 0 0 15px 0; font-size: 16px; font-weight: 600; color: #1e293b;">
                                    Subscription Details
                                </h3>
                                <table style="width: 100%; border-collapse: collapse;">
                                    <tr>
                                        <td style="padding: 8px 0; font-size: 14px; color: #475569;">Plan:</td>
                                        <td style="padding: 8px 0; font-size: 14px; color: #1e293b; font-weight: 600; text-align: right;">{plan.plan}</td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 8px 0; font-size: 14px; color: #475569;">QR Codes Used:</td>
                                        <td style="padding: 8px 0; font-size: 14px; color: #1e293b; font-weight: 600; text-align: right;">{subscription.qr_generated}/{plan.qr_count}</td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 8px 0; font-size: 14px; color: #475569;">Analytics Used:</td>
                                        <td style="padding: 8px 0; font-size: 14px; color: #1e293b; font-weight: 600; text-align: right;">{subscription.analytics_used}/{plan.analytics}</td>
                                    </tr>
                                </table>
                            </div>

                            <!-- CTA Button -->
                            <table role="presentation" style="width: 100%; border-collapse: collapse; margin: 30px 0;">
                                <tr>
                                    <td align="center">
                                        <a href="{renewal_url}" style="display: inline-block; padding: 16px 40px; background: linear-gradient(-45deg, #0ea5e9, #8b5cf6); color: #ffffff; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 16px; box-shadow: 0 4px 6px rgba(124, 58, 237, 0.3);">
                                            Renew Subscription Now
                                        </a>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 20px 0; font-size: 14px; line-height: 1.6; color: #64748b; text-align: center;">
                                Or copy and paste this link into your browser:
                            </p>

                            <p style="margin: 0 0 20px 0; font-size: 13px; line-height: 1.6; color: #8b5cf6; word-break: break-all; text-align: center; background-color: #f1f5f9; padding: 12px; border-radius: 6px;">
                                {renewal_url}
                            </p>

                            <!-- What Happens Next Box -->
                            <div style="margin: 20px 0; padding: 20px; background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;">
                                <h3 style="margin: 0 0 10px 0; font-size: 14px; font-weight: 600; color: #1e293b;">
                                    What happens after expiry?
                                </h3>
                                <ul style="margin: 0; padding-left: 20px; font-size: 14px; color: #475569; line-height: 1.8;">
                                    <li>You won't be able to create new QR codes</li>
                                    <li>Your existing QR codes will continue to work</li>
                                    <li>You can renew anytime to restore premium features</li>
                                </ul>
                            </div>

                            <p style="margin: 20px 0 0 0; font-size: 14px; line-height: 1.6; color: #64748b;">
                                If you have any questions or need assistance, please contact our support team.
                            </p>
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px 40px; background-color: #f8fafc; border-radius: 0 0 12px 12px; border-top: 1px solid #e2e8f0;">
                            <p style="margin: 0 0 10px 0; font-size: 14px; color: #64748b; text-align: center;">
                                Thanks,<br>
                                <strong style="color: #1e293b;">The QR Dada Team</strong>
                            </p>

                            <p style="margin: 20px 0 0 0; font-size: 12px; color: #94a3b8; text-align: center;">
                                © {datetime.now().year} QR Dada. All rights reserved.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
'''

        # Send the email using support email
        print("Attempting to send expiry notification email using SMTP...")
        from utils.email_service import email_service
        email_sent = email_service.send_support_email(
            to=user.company_email,
            subject=subject,
            body=body,
            html=html
        )

        if not email_sent:
            raise Exception(f"SMTP email service returned False for {user.company_email}")

        print(f"SUCCESS: Subscription expiry notification sent to {user.company_email}")
        
        # Log successful email
        EmailLog.log_email(
            recipient_email=user.company_email,
            recipient_name=user.name,
            email_type='subscription_expiry',
            subject=subject,
            user_id=user.id,
            status='sent',
            metadata={
                'subscription_plan': plan.plan,
                'hours_remaining': hours_remaining,
                'expiry_date': subscription.end_date.isoformat(),
                'qr_used': subscription.qr_generated,
                'qr_limit': plan.qr_count,
                'analytics_used': subscription.analytics_used,
                'analytics_limit': plan.analytics
            }
        )
        
        # Log the successful email
        current_app.logger.info(f"Subscription expiry notification sent to {user.company_email} for subscription ending {subscription.end_date}")
        
        return True
        
    except Exception as e:
        # Log failed email
        try:
            from .user import EmailLog
            EmailLog.log_email(
                recipient_email=user.company_email,
                recipient_name=user.name,
                email_type='subscription_expiry',
                subject=f"Your {subscription.subscription.plan} Subscription Expires in {hours_remaining} Hours - QR Dada",
                user_id=user.id,
                status='failed',
                error_message=str(e),
                metadata={
                    'subscription_plan': subscription.subscription.plan,
                    'hours_remaining': hours_remaining,
                    'expiry_date': subscription.end_date.isoformat()
                }
            )
        except:
            # If even logging fails, just continue
            pass
            
        error_msg = f"ERROR: Failed to send subscription expiry notification: {str(e)}"
        print(error_msg)
        current_app.logger.error(error_msg)
        import traceback
        traceback.print_exc()
        return False

def find_expiring_subscriptions(hours_before=24):
    """
    Find subscriptions that are expiring within the specified hours

    Args:
        hours_before: Number of hours before expiry to check (default 24)

    Returns:
        List of SubscribedUser objects that are expiring soon
    """
    try:
        # Use naive datetime for comparison (database stores naive datetimes)
        now = datetime.utcnow()
        expiry_threshold = now + timedelta(hours=hours_before)

        print(f"=== Finding Expiring Subscriptions ===")
        print(f"Current UTC time: {now}")
        print(f"Expiry threshold: {expiry_threshold}")
        print(f"Looking for subscriptions expiring within {hours_before} hours")

        # Find subscriptions expiring within the threshold
        # Also filter out subscriptions that already received notifications
        expiring_subscriptions = (
            SubscribedUser.query
            .filter(SubscribedUser._is_active == True)  # Only active subscriptions
            .filter(SubscribedUser.end_date > now)  # Not already expired
            .filter(SubscribedUser.end_date <= expiry_threshold)  # Expiring within threshold
            .filter(
                (SubscribedUser.expiry_notification_sent == False) |
                (SubscribedUser.expiry_notification_sent == None)
            )  # Only subscriptions that haven't received notification
            .join(User, SubscribedUser.U_ID == User.id)
            .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
            .all()
        )

        print(f"Found {len(expiring_subscriptions)} subscriptions expiring within {hours_before} hours (not yet notified)")

        # Debug: Print all active subscriptions and their end dates
        all_active = SubscribedUser.query.filter(SubscribedUser._is_active == True).all()
        print(f"Total active subscriptions: {len(all_active)}")
        for sub in all_active[:5]:  # Print first 5 for debugging
            print(f"  - Subscription {sub.id}: ends {sub.end_date}, notified: {sub.expiry_notification_sent}")

        return expiring_subscriptions

    except Exception as e:
        print(f"ERROR in find_expiring_subscriptions: {str(e)}")
        import traceback
        traceback.print_exc()
        current_app.logger.error(f"Error finding expiring subscriptions: {str(e)}")
        return []


def process_expiry_notifications():
    """
    Process and send expiry notifications for subscriptions ending in 48 hours
    This function should be called via a scheduled task (cron job)
    """
    try:
        print("=== PROCESSING SUBSCRIPTION EXPIRY NOTIFICATIONS ===")
        print(f"Current UTC time: {datetime.utcnow()}")

        # Find subscriptions expiring in 48 hours
        expiring_subscriptions = find_expiring_subscriptions(hours_before=48)

        if not expiring_subscriptions:
            print("No subscriptions expiring in the next 48 hours (or already notified)")
            return True

        successful_emails = 0
        failed_emails = 0

        for subscription in expiring_subscriptions:
            try:
                # Get user details
                user = User.query.get(subscription.U_ID)
                if not user:
                    print(f"Warning: User not found for subscription {subscription.id}")
                    continue

                # Check if user has valid email
                if not user.company_email:
                    print(f"Warning: User {user.id} has no email address")
                    continue

                # Skip unconfirmed emails but log it
                if not user.email_confirmed:
                    print(f"Warning: User {user.id} ({user.company_email}) has unconfirmed email - sending anyway")

                # Calculate hours remaining more precisely using naive datetime
                now = datetime.utcnow()
                time_remaining = subscription.end_date - now
                hours_remaining = max(1, int(time_remaining.total_seconds() / 3600))

                print(f"Processing user {user.company_email}: {hours_remaining} hours remaining")
                print(f"  Subscription ID: {subscription.id}")
                print(f"  End date: {subscription.end_date}")

                # Send expiry notification
                email_sent = send_subscription_expiry_notification(
                    user=user,
                    subscription=subscription,
                    hours_remaining=hours_remaining
                )

                if email_sent:
                    successful_emails += 1
                    print(f"✅ Notification sent to {user.company_email}")

                    # Mark notification as sent
                    subscription.expiry_notification_sent = True
                    subscription.expiry_notification_sent_at = datetime.utcnow()
                    db.session.commit()
                    print(f"✅ Marked subscription {subscription.id} as notified")
                else:
                    failed_emails += 1
                    print(f"❌ Failed to send notification to {user.company_email}")

                # Add a small delay between emails to avoid overwhelming the mail server
                import time
                time.sleep(1)

            except Exception as e:
                failed_emails += 1
                print(f"❌ Error processing subscription {subscription.id}: {str(e)}")
                import traceback
                traceback.print_exc()
                current_app.logger.error(f"Error processing expiry notification for subscription {subscription.id}: {str(e)}")

        print(f"=== EXPIRY NOTIFICATIONS COMPLETE ===")
        print(f"Successful emails: {successful_emails}")
        print(f"Failed emails: {failed_emails}")

        # Log summary
        current_app.logger.info(f"Processed {len(expiring_subscriptions)} expiring subscriptions: {successful_emails} success, {failed_emails} failed")

        return True

    except Exception as e:
        print(f"ERROR in process_expiry_notifications: {str(e)}")
        import traceback
        traceback.print_exc()
        current_app.logger.error(f"Error in process_expiry_notifications: {str(e)}")
        return False


# Add this route to subscription.py (for manual testing and cron job endpoint)

@subscription_bp.route('/admin/send-expiry-notifications')
@login_required
def admin_send_expiry_notifications():
    """
    Admin route to manually trigger expiry notifications
    Can also be called by cron job via curl/wget
    """
    user_id = session.get('user_id')
    
    # Check if user is admin (adjust this based on your admin system)
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401
    
    # You can add admin permission check here
    # For now, allowing any logged-in user for testing
    
    try:
        result = process_expiry_notifications()
        
        if result:
            flash('Expiry notifications processed successfully!', 'success')
            return jsonify({
                'status': 'success',
                'message': 'Expiry notifications processed successfully'
            })
        else:
            flash('Error processing expiry notifications', 'error')
            return jsonify({
                'status': 'error',
                'message': 'Error processing expiry notifications'
            }), 500
            
    except Exception as e:
        current_app.logger.error(f"Error in admin expiry notifications: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f'Error: {str(e)}'
        }), 500


# Add this route for cron job access (no authentication required)
@subscription_bp.route('/cron/send-expiry-notifications')
def cron_send_expiry_notifications():
    """
    Cron job endpoint to send expiry notifications
    This endpoint should be called by your server's cron job
    
    Add this to your crontab to run daily at 9 AM:
    0 9 * * * curl -X GET https://yourdomain.com/subscription/cron/send-expiry-notifications
    """
    try:
        # Optional: Add a secret token for security
        # token = request.args.get('token')
        # if token != app.config.get('CRON_SECRET_TOKEN'):
        #     return jsonify({'error': 'Invalid token'}), 403
        
        result = process_expiry_notifications()
        
        if result:
            return jsonify({
                'status': 'success',
                'message': 'Expiry notifications processed successfully',
                'timestamp': datetime.now(UTC).isoformat()
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'Error processing expiry notifications',
                'timestamp': datetime.now(UTC).isoformat()
            }), 500
            
    except Exception as e:
        current_app.logger.error(f"Error in cron expiry notifications: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': f'Error: {str(e)}',
            'timestamp': datetime.now(UTC).isoformat()
        }), 500
def calculate_total_analytics_operations(user_id):
    """
    Calculate total analytics operations (scans of dynamic QR codes) for a user.
    This matches the logic used in user_analytics route.
    """
    dynamic_qr_codes = get_dynamic_qr_codes(user_id)
    
    total_scans = 0
    for qr_code in dynamic_qr_codes:
        scans = Scan.query.filter_by(qr_code_id=qr_code.id).all()
        total_scans += len(scans)
    
    return total_scans    
def calculate_analytics_usage_data(user_id, subscription):
    """
    Calculate comprehensive analytics usage data.
    Returns dictionary with all analytics metrics.
    """
    if not subscription or not subscription.effective_analytics_limit:
        return {
            'analytics_used': 0,
            'analytics_remaining': 0,
            'analytics_total': 0,
            'analytics_percent': 0,
            'dynamic_qr_count': 0
        }

    # Get total analytics operations using same logic as user_analytics
    total_analytics_operations = calculate_total_analytics_operations(user_id)
    dynamic_qr_count = len(get_dynamic_qr_codes(user_id))

    analytics_total = subscription.effective_analytics_limit
    analytics_remaining = max(0, analytics_total - total_analytics_operations)
    analytics_percent = min(100, (total_analytics_operations / analytics_total) * 100) if analytics_total > 0 else 0
    
    return {
        'analytics_used': total_analytics_operations,
        'analytics_remaining': analytics_remaining,
        'analytics_total': analytics_total,
        'analytics_percent': analytics_percent,
        'dynamic_qr_count': dynamic_qr_count
    }

def cleanup_abandoned_payments(user_id):
    """
    Clean up abandoned payment records:
    - 'created' status: older than 24 hours (never reached payment gateway)
    - 'cancelled' status: older than 48 hours (reached gateway but never completed)
    """
    try:
        now = datetime.now(UTC)

        # Find 'created' payments older than 24 hours
        created_cutoff = now - timedelta(hours=24)
        abandoned_created = (
            Payment.query
            .filter(Payment.user_id == user_id)
            .filter(Payment.status == 'created')
            .filter(Payment.created_at < created_cutoff)
            .all()
        )

        # Find 'cancelled' payments older than 48 hours
        pending_cutoff = now - timedelta(hours=48)
        abandoned_pending = (
            Payment.query
            .filter(Payment.user_id == user_id)
            .filter(Payment.status == 'cancelled')
            .filter(Payment.created_at < pending_cutoff)
            .all()
        )

        # Combine all abandoned payments
        all_abandoned = abandoned_created + abandoned_pending

        # Delete abandoned payments
        if all_abandoned:
            for payment in all_abandoned:
                db.session.delete(payment)
            db.session.commit()
            current_app.logger.info(f"Cleaned up {len(all_abandoned)} abandoned payments for user {user_id} ({len(abandoned_created)} created, {len(abandoned_pending)} pending)")
            return len(all_abandoned)

        return 0

    except Exception as e:
        current_app.logger.error(f"Error cleaning up abandoned payments: {str(e)}")
        db.session.rollback()
        return 0

# ----------------------
# Subscription Routes
# ----------------------
@subscription_bp.route('/')
@login_required
def user_subscriptions():
    user_id = session.get('user_id')
    if not user_id:
        flash("You need to log in first.", "warning")
        return redirect(url_for('login'))

    # Fix any database inconsistencies
    fix_multiple_active_subscriptions(user_id)
    deactivate_expired_subscriptions(user_id)

    # Clean up abandoned payment records older than 24 hours
    cleanup_abandoned_payments(user_id)

    # Get active subscriptions
    active_subscribed = (
        db.session.query(SubscribedUser, Subscription)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .filter(Subscription.archived_at.is_(None))
        .all()
    )

    # Calculate actual analytics for each subscription
    subscriptions_with_analytics = []
    for sub, plan in active_subscribed:
        # Ensure timezone-aware dates
        if sub.start_date.tzinfo is None:
            sub.start_date = sub.start_date.replace(tzinfo=UTC)
        if sub.end_date.tzinfo is None:
            sub.end_date = sub.end_date.replace(tzinfo=UTC)
        if sub.last_usage_reset and sub.last_usage_reset.tzinfo is None:
            sub.last_usage_reset = sub.last_usage_reset.replace(tzinfo=UTC)
        
        # Calculate real analytics usage
        analytics_data = calculate_analytics_usage_data(user_id, sub)
        
        # Add calculated values to subscription object
        sub.actual_analytics_used = analytics_data['analytics_used']
        sub.actual_analytics_remaining = analytics_data['analytics_remaining']
        sub.actual_analytics_percent = analytics_data['analytics_percent']
        sub.dynamic_qr_count = analytics_data['dynamic_qr_count']
        
        subscriptions_with_analytics.append((sub, plan))
    
    # Get other data - Show completed and pending payments (exclude 'created' status)
    payment_history = (
        Payment.query
        .filter_by(user_id=user_id)
        .filter(Payment.status.in_(['completed', 'cancelled']))  # Show completed and pending payments
        .order_by(Payment.created_at.desc())
        .all()
    )
    available_plans = (
        Subscription.query
        .filter(Subscription.is_active == True)
        .filter(Subscription.archived_at.is_(None))
        .all()
    )
    has_active = has_active_subscription(user_id)

    return render_template(
        'user/subscriptions.html',
        subscribed=subscriptions_with_analytics,
        active_subscribed=subscriptions_with_analytics,
        payment_history=payment_history,
        available_plans=available_plans,
        now=datetime.now(UTC),
        hasattr=hasattr,
        has_active_subscription=has_active
    )
@subscription_bp.route('/subscribe/<int:plan_id>', methods=['POST'])
@login_required
def subscribe(plan_id):
    user_id = session.get('user_id')
    current_app.logger.info(f"Subscribe request received for plan {plan_id} by user {user_id}")

    # Check if user already has an active subscription
    now = datetime.now(UTC)
    active_subscription = SubscribedUser.query.filter(
        SubscribedUser.U_ID == user_id,
        SubscribedUser.end_date > now,
        SubscribedUser._is_active == True
    ).first()
    
    if active_subscription:
        flash('You already have an active subscription. Please wait for it to expire or cancel it before subscribing to a new plan.', 'warning')
        return redirect(url_for('subscription.user_subscriptions'))

    # Get the subscription plan
    subscription = (
        Subscription.query
        .filter(Subscription.S_ID == plan_id)
        .filter(Subscription.is_active == True)
        .filter(Subscription.archived_at.is_(None))
        .first_or_404()
    )
    
    # Create Razorpay order
    try:
        # Consistent GST calculation
        gst_rate = 0.18  # 18% GST
        base_amount = subscription.price
        gst_amount = base_amount * gst_rate
        total_amount = base_amount + gst_amount
        
        # Convert to paisa and round to integer
        amount_in_paisa = int(total_amount * 100)
        currency = 'INR'
        
        # Robust price validation
        if total_amount <= 0 or amount_in_paisa <= 0:
            current_app.logger.error(f'Invalid subscription price for plan {plan_id}')
            flash('Invalid subscription price. Please contact support.', 'danger')
            return redirect(url_for('subscription.user_subscriptions'))
        
        # Create Razorpay order
        razorpay_client = get_razorpay_client()
        razorpay_order = razorpay_client.order.create({
            'amount': amount_in_paisa,
            'currency': currency,
            'payment_capture': '1',
            'notes': {
                'user_id': user_id,
                'plan_id': plan_id,
                'description': f'Subscription for {subscription.plan}'
            }
        })
        
        # Store order details in the database with consistent calculations
        payment = Payment(
            base_amount=base_amount,
            gst_amount=gst_amount,
            total_amount=total_amount,
            user_id=user_id,
            subscription_id=plan_id,
            razorpay_order_id=razorpay_order['id'],
            currency=currency,
            status='created',
            payment_type='new',
            gst_rate=gst_rate
        )
        db.session.add(payment)
        db.session.commit()
        
        # Redirect to checkout page with Razorpay details
        return redirect(url_for('subscription.checkout', order_id=razorpay_order['id']))
        
    except Exception as e:
        current_app.logger.error(f"Error in subscribe route: {str(e)}", exc_info=True)
        db.session.rollback()
        flash(f'Error creating payment. Please try again or contact support.', 'danger')
        return redirect(url_for('subscription.user_subscriptions'))

def validate_razorpay_order(subscription, amount, payment):
    """
    Validate Razorpay order details
    
    :param subscription: Subscription object
    :param amount: Amount in paisa
    :param payment: Payment object
    :return: Boolean indicating if order is valid
    """
    try:
        expected_amount = int(payment.total_amount * 100)
        return amount == expected_amount
    except Exception as e:
        current_app.logger.error(f"Order validation error: {str(e)}")
        return False

@subscription_bp.route('/get_available_plans')
@login_required
def get_available_plans():
    user_id = session.get('user_id')
    
    # Get current active subscription
    current_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .first()
    )
    
    # Get query parameter to exclude current plan
    exclude_plan_id = request.args.get('exclude', type=int)
    
    # Get available plans
    available_plans = (
        Subscription.query
        .filter(Subscription.is_active == True)
        .filter(Subscription.archived_at.is_(None))
        .filter(Subscription.S_ID != exclude_plan_id)
        .all()
    )
    
    # Convert to JSON
    plans_json = [
        {
            'S_ID': plan.S_ID,
            'plan': plan.plan,
            'price': plan.price,
            'days': plan.days,
            'tier': plan.tier,
            'usage_per_day': plan.usage_per_day
        } for plan in available_plans
    ]
    
    return jsonify(plans_json)

# Alternative fix for subscription_details route - pass available plans data

@subscription_bp.route('/subscription_details/<int:subscription_id>')
@login_required
def subscription_details(subscription_id):
    user_id = session.get('user_id')
    page = request.args.get('page', 1, type=int)
    per_page = 10  # Number of records per page
    
    # Verify the subscription belongs to the logged-in user
    subscription = (
        db.session.query(SubscribedUser, Subscription)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .filter(SubscribedUser.id == subscription_id, SubscribedUser.U_ID == user_id)
        .first_or_404()
    )
    
    # Calculate actual analytics usage for this subscription
    analytics_data = get_analytics_usage_data(user_id, subscription[0])
    
    # Add calculated values to subscription object
    sub = subscription[0]
    sub.actual_analytics_used = analytics_data['analytics_used']
    sub.actual_analytics_remaining = analytics_data['analytics_remaining']
    sub.actual_analytics_percent = analytics_data['analytics_percent']
    sub.dynamic_qr_count = analytics_data['dynamic_qr_count']
    
    # Calculate QR usage for subscription period
    subscription_start_date = sub.start_date
    subscription_end_date = sub.end_date
    
    # Import QRCode here to avoid circular import
    from .qr_models import QRCode
    
    qr_count_in_period = QRCode.query.filter(
        QRCode.user_id == user_id,
        QRCode.created_at >= subscription_start_date,
        QRCode.created_at <= subscription_end_date
    ).count()
    
    # Update QR usage data
    sub.actual_qr_generated = qr_count_in_period
    qr_total = sub.subscription.qr_count
    sub.actual_qr_remaining = max(0, qr_total - qr_count_in_period)
    sub.actual_qr_percent = (qr_count_in_period / qr_total * 100) if qr_total > 0 else 0
    
    # Get paginated subscription usage history
    usage_query = (
        UsageLog.query
        .filter(UsageLog.subscription_id == subscription_id)
        .order_by(UsageLog.timestamp.desc())
    )
    
    # Paginate the results
    usage_history = usage_query.paginate(page=page, per_page=per_page, error_out=False)
    
    # Get completed and pending payment records for this user (not just for this subscription)
    payment_records = (
        Payment.query
        .filter_by(user_id=user_id)
        .filter(Payment.status.in_(['completed', 'cancelled']))  # Show completed and pending payments
        .order_by(Payment.created_at.desc())
        .all()
    )
    
    # Get available plans for plan name lookup
    available_plans = (
        Subscription.query
        .filter(Subscription.is_active == True)
        .filter(Subscription.archived_at.is_(None))
        .all()
    )
    
    # Calculate daily usage statistics
    daily_usage = {}
    all_usage = usage_query.limit(100).all()
    
    if all_usage:
        for usage in all_usage:
            date_key = usage.timestamp.strftime('%Y-%m-%d')
            if date_key not in daily_usage:
                daily_usage[date_key] = 0
            daily_usage[date_key] += 1
    
    # Sort daily usage by date
    sorted_daily_usage = [(k, v) for k, v in sorted(daily_usage.items())]    
    
    return render_template(
        'user/subscription_details.html',
        subscription=subscription[0],
        plan=subscription[1],
        usage_history=usage_history,
        payment_records=payment_records,
        available_plans=available_plans,  # Add this for plan name lookup
        daily_usage=sorted_daily_usage,
        current_date=datetime.now(UTC)
    )

@subscription_bp.route('/subscription/<int:subscription_id>/usage_history')
@login_required
def get_usage_history(subscription_id):
    """AJAX endpoint to get paginated usage history"""
    user_id = session.get('user_id')
    page = request.args.get('page', 1, type=int)
    per_page = 10
    
    # Verify the subscription belongs to the logged-in user
    subscription = (
        db.session.query(SubscribedUser, Subscription)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .filter(SubscribedUser.id == subscription_id, SubscribedUser.U_ID == user_id)
        .first_or_404()
    )
    
    # Get paginated usage history
    usage_history = (
        UsageLog.query
        .filter(UsageLog.subscription_id == subscription_id)
        .order_by(UsageLog.timestamp.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    
    # Check if this is an AJAX request
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template(
            'user/partials/usage_history.html',
            subscription=subscription[0],
            usage_history=usage_history
        )
    
    # If not an AJAX request, redirect to the main page
    return redirect(url_for('subscription.subscription_details', subscription_id=subscription_id, page=page))

@subscription_bp.route('/download_invoice/<int:payment_id>')
@login_required
def download_invoice(payment_id):
    user_id = session.get('user_id')
    
    # Fetch the payment
    payment = Payment.query.get_or_404(payment_id)
    
    # Verify user authorization
    if payment.user_id != user_id:
        flash('Unauthorized access to invoice', 'error')
        return redirect(url_for('dashboard'))
    
    # Generate the invoice PDF
    pdf_buffer = generate_invoice_pdf(payment)
    
    # Send the PDF as a download
    return send_file(
        pdf_buffer,
        download_name=f"invoice_{payment.invoice_number}.pdf",
        as_attachment=True,
        mimetype='application/pdf'
    )

@subscription_bp.route('/subscription/<int:subscription_id>')
@login_required
def view_subscription_details(subscription_id):
    user_id = session.get('user_id')
    subscription = SubscribedUser.query.get_or_404(subscription_id)
    
    # Verify this subscription belongs to the current user
    if subscription.U_ID != user_id:
        flash('Unauthorized action', 'danger')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # Get plan details
    plan = Subscription.query.get(subscription.S_ID)
    
    # Get payment history - Show completed and pending payments
    payments = (
        Payment.query
        .filter_by(user_id=user_id, subscription_id=subscription.S_ID)
        .filter(Payment.status.in_(['completed', 'cancelled']))  # Show completed and pending payments
        .order_by(Payment.created_at.desc())
        .all()
    )
    
    return render_template('user/subscription_details.html', 
                          subscription=subscription, 
                          plan=plan,
                          payments=payments)

@subscription_bp.route('/checkout/<order_id>', methods=['GET', 'POST'])
@login_required
def checkout(order_id):
    user_id = session.get('user_id')
    
    # Get user details
    user = db.session.get(User, user_id)
    if not user:
        flash('User not found', 'danger')
        return redirect(url_for('login'))
    
    # Get payment and subscription details
    payment = Payment.query.filter_by(razorpay_order_id=order_id, user_id=user_id).first()
    if not payment:
        flash('Payment not found', 'danger')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # Get subscription
    subscription = db.session.get(Subscription, payment.subscription_id)
    if not subscription:
        flash('Subscription not found', 'danger')
        return redirect(url_for('subscription.user_subscriptions'))
    
    if request.method == 'POST':
        # Validate required fields
        required_fields = [
            'full_name', 'street_address', 'city', 
            'state', 'postal_code', 'country', 
            'email', 'phone_number'
        ]
        
        # Check if all required fields are filled
        for field in required_fields:
            if not request.form.get(field):
                flash(f'Please fill in all required fields, especially {field.replace("_", " ")}', 'warning')
                return render_template(
                    'user/checkout.html',
                    user=user,
                    payment=payment,
                    subscription=subscription,
                    razorpay_key_id=current_app.config['RAZORPAY_KEY_ID']
                )
        
        # Create or update invoice address
        invoice_address = InvoiceAddress(
            payment_id=payment.iid,
            full_name=request.form.get('full_name'),
            company_name=request.form.get('company_name', ''),
            street_address=request.form.get('street_address'),
            city=request.form.get('city'),
            state=request.form.get('state'),
            postal_code=request.form.get('postal_code'),
            country=request.form.get('country', 'India'),
            email=request.form.get('email', user.company_email),
            phone_number=request.form.get('phone_number'),
            gst_number=request.form.get('gst_number', ''),
            pan_number=request.form.get('pan_number', '')
        )
        
        db.session.add(invoice_address)
        db.session.commit()
        
        return redirect(url_for('subscription.verify_payment', order_id=order_id))
    
    return render_template(
        'user/checkout.html',
        user=user,
        payment=payment,
        subscription=subscription,
        base_amount=payment.base_amount,
        gst_rate=payment.gst_rate,
        gst_amount=payment.gst_amount,
        total_amount=payment.total_amount,
        razorpay_key_id=current_app.config['RAZORPAY_KEY_ID']
    )

@subscription_bp.route("/payment/verify/<order_id>", methods=["GET", "POST"])
@login_required
def verify_payment(order_id):
    user_id = session.get('user_id')
    if not user_id:
        flash('You need to log in first.', 'warning')
        return redirect(url_for('login'))
    
    if request.method == "GET":
        # Handle GET request - show payment verification page
        payment = Payment.query.filter_by(razorpay_order_id=order_id, user_id=user_id).first()
        if not payment:
            flash('Payment not found', 'danger')
            return redirect(url_for('subscription.user_subscriptions'))

        # Update payment status to 'pending' when user reaches payment gateway
        if payment.status == 'created':
            payment.status = 'pending'
            db.session.commit()
            current_app.logger.info(f"Payment {payment.razorpay_order_id} status updated to 'pending' for user {user_id}")

        subscription = Subscription.query.get(payment.subscription_id)
        user = User.query.get(user_id)

        return render_template('payment/verify.html',
                             payment=payment,
                             subscription=subscription,
                             user=user,
                             razorpay_key_id=current_app.config['RAZORPAY_KEY_ID'])
    
    # Handle POST request - actual payment verification
    razorpay_payment_id = request.form.get('razorpay_payment_id')
    razorpay_order_id = request.form.get('razorpay_order_id')
    razorpay_signature = request.form.get('razorpay_signature')
    
    if not all([razorpay_payment_id, razorpay_order_id, razorpay_signature]):
        current_app.logger.error(f"Missing payment details for order {order_id}")
        flash('Missing payment details. Please try again.', 'danger')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # Find the payment record - check for both pending and cancelled status
    payment = Payment.query.filter_by(
        razorpay_order_id=razorpay_order_id,
        user_id=user_id
    ).filter(
        Payment.status.in_(['pending', 'cancelled', 'created'])
    ).first()

    if not payment:
        current_app.logger.error(f"Payment record not found for order {razorpay_order_id}, user {user_id}")
        flash('Payment record not found.', 'danger')
        return redirect(url_for('subscription.user_subscriptions'))

    # Log the current payment status for debugging
    current_app.logger.info(f"Found payment {razorpay_order_id} with status: {payment.status}")

    # Check if payment is already completed
    if payment.status == 'completed':
        current_app.logger.warning(f"Payment {razorpay_order_id} already completed, redirecting to subscriptions")
        flash('This payment has already been processed.', 'info')
        return redirect(url_for('subscription.user_subscriptions'))
    
    try:
        # Verify signature
        signature_valid = verify_razorpay_signature(
            razorpay_order_id, 
            razorpay_payment_id, 
            razorpay_signature, 
            current_app.config['RAZORPAY_KEY_SECRET']
        )
        
        if not signature_valid:
            current_app.logger.error(f"Signature verification failed for payment {razorpay_payment_id}")
            flash('Payment verification failed. Please contact support.', 'danger')
            return redirect(url_for('subscription.user_subscriptions'))
        
        # Fetch payment details from Razorpay to verify amount
        try:
            razorpay_client = get_razorpay_client()
            payment_details = razorpay_client.payment.fetch(razorpay_payment_id)
            
            if payment_details['status'] not in ['authorized', 'captured']:
                current_app.logger.error(f"Payment not authorized: {payment_details['status']}")
                flash('Payment was not authorized. Please try again.', 'danger')
                return redirect(url_for('subscription.user_subscriptions'))
                
        except Exception as fetch_error:
            current_app.logger.error(f"Error fetching payment details from Razorpay: {str(fetch_error)}")
            flash('Unable to verify payment details with Razorpay.', 'danger')
            return redirect(url_for('subscription.user_subscriptions'))
        
        # Verify the amount matches the expected amount
        expected_amount_in_paisa = int(payment.total_amount * 100)
        if payment_details['amount'] != expected_amount_in_paisa:
            current_app.logger.error(
                f"Amount mismatch: Expected {expected_amount_in_paisa}, "
                f"Got {payment_details['amount']} for payment {razorpay_payment_id}"
            )
            flash('Payment amount verification failed. Please contact support.', 'danger')
            return redirect(url_for('subscription.user_subscriptions'))
        
        # CRITICAL FIX: Get user and subscription objects
        user = User.query.get(user_id)
        subscription = Subscription.query.get(payment.subscription_id)
        
        if not user or not subscription:
            current_app.logger.error(f"User or subscription not found: user={user}, subscription={subscription}")
            flash('User or subscription not found.', 'danger')
            return redirect(url_for('subscription.user_subscriptions'))
        
        # Update payment details
        payment.razorpay_payment_id = razorpay_payment_id
        payment.status = "completed"
        
        # CRITICAL FIX: Create or update subscription BEFORE sending email
        try:
            # Check if user already has an active subscription
            existing_subscriptions = SubscribedUser.query.filter(
                SubscribedUser.U_ID == user_id,
                SubscribedUser.is_active == True
            ).all()
            
            if existing_subscriptions:
                # Deactivate existing subscriptions
                for existing in existing_subscriptions:
                    existing.is_active = False
                current_app.logger.info(f"Deactivated {len(existing_subscriptions)} existing subscriptions for user {user_id}")
            
            # Create new subscription
            start_date = datetime.now(UTC)
            end_date = start_date + timedelta(days=subscription.days)
            
            new_subscription = SubscribedUser(
                U_ID=user_id,
                S_ID=subscription.S_ID,
                start_date=start_date,
                end_date=end_date,
                current_usage=0,
                is_active=True,
                is_auto_renew=True
            )
            
            # Add subscription history
            history_entry = SubscriptionHistory(
                U_ID=user_id,
                S_ID=subscription.S_ID,
                action="new" if not existing_subscriptions else "upgrade",
                previous_S_ID=existing_subscriptions[0].S_ID if existing_subscriptions else None,
                created_at=datetime.now(UTC)
            )
            
            db.session.add(new_subscription)
            db.session.add(history_entry)
            
            # COMMIT ALL CHANGES FIRST
            db.session.commit()
            current_app.logger.info(f"Payment successful: {razorpay_payment_id} for user: {user_id}")
            
        except Exception as db_error:
            db.session.rollback()
            current_app.logger.error(f"Database error during subscription creation: {str(db_error)}")
            flash('Error activating subscription. Please contact support.', 'danger')
            return redirect(url_for('subscription.user_subscriptions'))
        
        # FIXED: Send confirmation email AFTER successful database commit
        try:
            email_sent = send_payment_confirmation_email(user, payment, subscription)
            if email_sent:
                current_app.logger.info(f"Payment confirmation email sent to {user.company_email}")
            else:
                current_app.logger.error(f"Failed to send payment confirmation email to {user.company_email}")
        except Exception as email_error:
            # CRITICAL: Email failure should NOT affect payment success
            current_app.logger.error(f"Email error during payment processing: {str(email_error)}")
            print(f"Email sending failed: {str(email_error)}")
        
        flash(f'Payment successful! You are now subscribed to the {subscription.plan} plan.', 'success')
        return redirect(url_for('subscription.user_subscriptions'))
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Unexpected error in payment verification: {str(e)}", exc_info=True)
        flash('An unexpected error occurred. Please try again or contact support.', 'danger')
        return redirect(url_for('subscription.user_subscriptions'))

@subscription_bp.route('/subscription/change/<int:new_plan_id>', methods=['GET', 'POST'])
@login_required
def change_subscription(new_plan_id):
    user_id = session.get('user_id')
    
    # Get current active subscription
    current_subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .first()
    )
    
    # If no subscription found, log detailed information
    if not current_subscription:
        current_app.logger.warning(f"No active subscription found for user {user_id}")
        flash('You don\'t have an active subscription to change.', 'warning')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # Get the new subscription plan
    new_plan = Subscription.query.get_or_404(new_plan_id)
    
    # Determine if this is an upgrade or downgrade
    is_upgrade = new_plan.tier > current_subscription.subscription.tier
    
    # Calculate remaining value of current subscription
    remaining_value = current_subscription.remaining_value()
    
    if request.method == 'POST':
        try:
            # Start a database transaction
            db.session.begin_nested()
            
            # Calculate the amount to charge with GST consideration
            if is_upgrade:
                # Amount to charge after applying remaining value credit
                amount_to_charge = max(0, new_plan.price - remaining_value)
                
                # Create a Payment instance 
                payment = Payment(
                    user_id=user_id,
                    subscription_id=new_plan_id,
                    base_amount=amount_to_charge,
                    payment_type='upgrade',
                    previous_subscription_id=current_subscription.S_ID,
                    credit_applied=remaining_value,
                    razorpay_order_id=None,  # Will be set later
                    status='created',
                    currency='INR'
                )
                
                # If there's an amount to charge, create Razorpay order
                if payment.total_amount > 0:
                    razorpay_client = get_razorpay_client()
                    razorpay_order = razorpay_client.order.create({
                        'amount': int(payment.total_amount * 100),
                        'currency': 'INR',
                        'payment_capture': '1'
                    })
                    
                    payment.razorpay_order_id = razorpay_order['id']
                    db.session.add(payment)
                    db.session.commit()
                    
                    return redirect(url_for('subscription.checkout', order_id=razorpay_order['id']))
                else:
                    # No additional payment needed
                    _process_subscription_change(
                        user_id, 
                        current_subscription, 
                        new_plan_id, 
                        is_upgrade=True, 
                        credit_applied=remaining_value
                    )
                    
                    flash(f'Your subscription has been upgraded to {new_plan.plan}!', 'success')
                    return redirect(url_for('subscription.user_subscriptions'))
            
            else:
                # Downgrade case - process change without payment
                _process_subscription_change(
                    user_id, 
                    current_subscription, 
                    new_plan_id, 
                    is_upgrade=False, 
                    credit_applied=remaining_value
                )
                
                flash(f'Your subscription has been changed to {new_plan.plan}.', 'success')
                return redirect(url_for('subscription.user_subscriptions'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error processing subscription change: {str(e)}")
            flash(f'Error processing subscription change: {str(e)}', 'danger')
            return redirect(url_for('subscription.user_subscriptions'))
    # GET request - show confirmation page
    return render_template(
        'user/change_subscription.html',
        current_subscription=current_subscription,
        new_plan=new_plan,
        is_upgrade=is_upgrade,
        remaining_value=remaining_value,
        amount_to_charge=max(0, new_plan.price - remaining_value) if is_upgrade else 0,
        gst_rate=0.18  # Standard GST rate
    )

def _process_subscription_change(user_id, current_subscription, new_plan_id, is_upgrade, credit_applied=0, additional_days=0):
    """Process a subscription change (upgrade or downgrade)"""
    try:
        # Get the new subscription plan
        new_plan = Subscription.query.get(new_plan_id)
        
        # Deactivate current subscription
        current_subscription.is_active = False
        
        # Calculate new subscription dates
        start_date = datetime.now(UTC)
        
        if is_upgrade:
            # For upgrades, standard plan duration
            end_date = start_date + timedelta(days=new_plan.days)
        else:
            # For downgrades, calculate additional days from remaining credit
            new_plan_daily_price = new_plan.price / new_plan.days if new_plan.days > 0 else 0
            if additional_days > 0:
                calc_additional_days = additional_days
            else:
                calc_additional_days = int(credit_applied / new_plan_daily_price) if new_plan_daily_price > 0 else 0
            end_date = start_date + timedelta(days=new_plan.days + calc_additional_days)
        
        # Create NEW active subscription
        new_subscription = SubscribedUser(
            U_ID=user_id,
            S_ID=new_plan_id,
            start_date=start_date,
            end_date=end_date,
            is_auto_renew=current_subscription.is_auto_renew,
            current_usage=0,
            last_usage_reset=start_date,
            _is_active=True
        )
        
        # Add the new subscription
        db.session.add(new_subscription)
        
        # Log subscription change history
        history_entry = SubscriptionHistory(
            U_ID=user_id,
            S_ID=new_plan_id,
            action='upgrade' if is_upgrade else 'downgrade',
            previous_S_ID=current_subscription.S_ID,
            created_at=datetime.now(UTC)
        )
        db.session.add(history_entry)
        
        # Commit changes
        db.session.commit()
        
        return True
    
    except Exception as e:
        # Rollback in case of any errors
        db.session.rollback()
        current_app.logger.error(f"Subscription change error: {str(e)}")
        return False

# Add auto-renewal toggle route
@subscription_bp.route('/subscription/auto-renew/<int:subscription_id>/<int:status>')
@login_required
def toggle_auto_renew(subscription_id, status):
    user_id = session.get('user_id')
    
    # Find the specific subscription
    subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.id == subscription_id)
        .filter(SubscribedUser.U_ID == user_id)
        .first_or_404()
    )
    
    # Update auto-renew status
    subscription.is_auto_renew = bool(status)
    db.session.commit()
    
    if subscription.is_auto_renew:
        flash('Auto-renewal has been enabled for your subscription.', 'success')
    else:
        flash('Auto-renewal has been disabled for your subscription.', 'info')
    
    return redirect(url_for('subscription.user_subscriptions'))


# Add a route to handle subscription cancellation
@subscription_bp.route('/subscription/cancel/<int:subscription_id>', methods=['GET', 'POST'])
@login_required
def cancel_subscription(subscription_id):
    user_id = session.get('user_id')
    
    # Find the specific subscription
    subscription = (
        SubscribedUser.query
        .filter(SubscribedUser.id == subscription_id)
        .filter(SubscribedUser.U_ID == user_id)
        .first_or_404()
    )
    
    if request.method == 'POST':
        # Disable auto-renewal and set is_active to False
        subscription.is_auto_renew = False
        subscription._is_active = False
        
        # Add history entry
        history_entry = SubscriptionHistory(
            U_ID=user_id,
            S_ID=subscription.S_ID,
            action='cancel',
            previous_S_ID=subscription.S_ID,
            created_at=datetime.now(UTC)
        )
        db.session.add(history_entry)
        db.session.commit()
        
        flash('Your subscription has been cancelled successfully', 'info')
        return redirect(url_for('subscription.user_subscriptions'))
    
    # GET request - show confirmation page
    return render_template(
        'user/cancel_subscription.html',
        subscription=subscription
    )

# Design access checking helper function
def has_design_access(user_id, design_name):
    """
    Check if a user has access to a specific design based on their subscription
    
    Args:
        user_id (int): ID of the user
        design_name (str): Name of the design to check access for
        
    Returns:
        bool: True if the user has access, False otherwise
    """
    try:
        # Get the user's active subscription
        active_subscription = (
            SubscribedUser.query
            .filter(SubscribedUser.U_ID == user_id)
            .filter(SubscribedUser.end_date > datetime.now(UTC))
            .filter(SubscribedUser._is_active == True)
            .first()
        )
        
        if not active_subscription:
            return False
            
        # Get the subscription plan details
        subscription = Subscription.query.get(active_subscription.S_ID)
        
        if not subscription or not subscription.design:
            return False
            
        # Check if the design is in the allowed designs
        allowed_designs = [d.strip() for d in subscription.design.split(',')]
        return design_name.strip() in allowed_designs
        
    except Exception as e:
        current_app.logger.error(f"Error checking design access: {str(e)}")
        return False

# Analytics tracking helper functions
def increment_analytics_usage(user_id):
    """
    Increment analytics usage for a user
    
    Args:
        user_id (int): ID of the user
        
    Returns:
        bool: True if incremented successfully, False if limit reached
    """
    try:
        # Get active subscription
        active_subscription = (
            SubscribedUser.query
            .filter(SubscribedUser.U_ID == user_id)
            .filter(SubscribedUser.end_date > datetime.now(UTC))
            .filter(SubscribedUser._is_active == True)
            .first()
        )
        
        if not active_subscription:
            return False
            
        # Check if we've reached the analytics limit
        if active_subscription.analytics_used >= active_subscription.subscription.analytics:
            return False
            
        # Increment analytics usage
        active_subscription.analytics_used += 1
        db.session.commit()
        
        # Record usage in log
        record_usage_log(
            user_id=user_id,
            subscription_id=active_subscription.id,
            operation_type="analytics",
            details="Analytics usage"
        )
        
        return True
        
    except Exception as e:
        current_app.logger.error(f"Error incrementing analytics usage: {str(e)}")
        db.session.rollback()
        return False

# QR code tracking helper functions
def increment_qr_usage(user_id):
    """
    Increment QR code usage for a user
    
    Args:
        user_id (int): ID of the user
        
    Returns:
        bool: True if incremented successfully, False if limit reached
    """
    try:
        # Get active subscription
        active_subscription = (
            SubscribedUser.query
            .filter(SubscribedUser.U_ID == user_id)
            .filter(SubscribedUser.end_date > datetime.now(UTC))
            .filter(SubscribedUser._is_active == True)
            .first()
        )
        
        if not active_subscription:
            return False
            
        # Check if we've reached the QR limit
        if active_subscription.qr_generated >= active_subscription.subscription.qr_count:
            return False
            
        # Increment QR usage
        active_subscription.qr_generated += 1
        db.session.commit()
        
        # Record usage in log
        record_usage_log(
            user_id=user_id,
            subscription_id=active_subscription.id,
            operation_type="qr_code",
            details="QR code generation"
        )
        
        return True
        
    except Exception as e:
        current_app.logger.error(f"Error incrementing QR usage: {str(e)}")
        db.session.rollback()
        return False

# Helper function to increment usage with daily reset
def increment_usage(user_id):
    sub = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .first()
    )
    
    if sub:
        # Check if we need to reset the usage counter (new day)
        today = datetime.now(UTC).date()
        last_reset_date = getattr(sub, 'last_usage_reset', None)
        
        if not last_reset_date or last_reset_date.date() < today:
            # Reset counter for new day
            sub.current_usage = 0
            sub.last_usage_reset = datetime.now(UTC)
        
        # Increment usage
        sub.current_usage += 1
        db.session.commit()
        
        # Check if daily limit reached
        if sub.current_usage > sub.subscription.usage_per_day:
            return False
    
    return True

# Function to check if user has an active subscription
def has_active_subscription(user_id):
    """
    Strict check to ensure only ONE active subscription exists
    - Must be active
    - End date in the future
    - Exactly one active subscription
    """
    active_subs = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .count()
    )
    return active_subs == 1

# Fix issues with multiple active subscriptions
def fix_multiple_active_subscriptions(user_id):
    """
    Fix issue where a user has multiple active subscriptions by keeping only the most recent one
    """
    active_subs = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date > datetime.now(UTC))
        .filter(SubscribedUser._is_active == True)
        .order_by(SubscribedUser.start_date.desc())  # Most recent first
        .all()
    )
    
    if len(active_subs) > 1:
        # Keep the first (newest) subscription active and mark others as inactive
        for i, sub in enumerate(active_subs):
            if i > 0:  # Skip the first one
                sub._is_active = False
        
        db.session.commit()
        return True
    
    return False

# Deactivate expired subscriptions
def deactivate_expired_subscriptions(user_id):
    """
    Make sure all expired subscriptions are marked as inactive
    """
    now = datetime.now(UTC)
    expired_subs = (
        SubscribedUser.query
        .filter(SubscribedUser.U_ID == user_id)
        .filter(SubscribedUser.end_date <= now)
        .filter(SubscribedUser._is_active == True)
        .all()
    )
    
    if expired_subs:
        for sub in expired_subs:
            sub._is_active = False
        
        db.session.commit()
        return True
    
    return False

def design_access_required(design_name):
    """
    Decorator to check if a user has access to a specific design
    
    Args:
        design_name (str): Name of the design to check access for
        
    Returns:
        Function: Decorated function that checks access before execution
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                # Check if user is logged in
                if 'user_id' not in session:
                    flash("You need to log in first.", "warning")
                    return redirect(url_for('login'))
                
                user_id = session.get('user_id')
                
                # First, check if the user has an active subscription
                now = datetime.now(UTC)
                active_subscription = (
                    SubscribedUser.query
                    .filter(SubscribedUser.U_ID == user_id)
                    .filter(SubscribedUser.end_date > now)
                    .filter(SubscribedUser._is_active == True)
                    .first()
                )
                
                if not active_subscription:
                    current_app.logger.warning(f"User {user_id} attempted to access design {design_name} without subscription")
                    flash("You need an active subscription to access this feature.", "warning")
                    return redirect(url_for('subscription.user_subscriptions'))
                
                # Check if the user has access to the specific design
                if not has_design_access(user_id, design_name):
                    current_app.logger.warning(f"User {user_id} attempted to access design {design_name} without permission")
                    flash(f"Your subscription plan does not include access to the {design_name} design.", "warning")
                    return redirect(url_for('subscription.user_subscriptions'))
                
                # If all checks pass, execute the decorated function
                return f(*args, **kwargs)
                
            except Exception as e:
                # Comprehensive error logging
                current_app.logger.error(f"Design access check error in {f.__name__}: {e}")
                import traceback
                current_app.logger.error(traceback.format_exc())
                flash("Design access verification failed. Please try again later.", "danger")
                return redirect(url_for('subscription.user_subscriptions'))
                
        return decorated_function
    return decorator

def subscription_required(operation_type='general'):
    """
    Decorator to check if a user has an active subscription with available usage
    
    Args:
        operation_type (str): Type of operation ('general', 'analytics', 'qr_code')
        
    Returns:
        Function: Decorated function that checks subscription before execution
    """
    def decorator(f):
        @wraps(f)
        def wrap(*args, **kwargs):
            try:
                # Log function being decorated
                current_app.logger.info(f"Checking subscription for route: {f.__name__} (operation: {operation_type})")
                
                # Check if user is logged in
                if 'user_id' not in session:
                    flash("You need to log in first.", "warning")
                    return redirect(url_for('login'))
                
                user_id = session.get('user_id')
                
                # First, check if the user has an active subscription
                now = datetime.now(UTC)
                active_subscription = (
                    SubscribedUser.query
                    .filter(SubscribedUser.U_ID == user_id)
                    .filter(SubscribedUser.end_date > now)
                    .filter(SubscribedUser._is_active == True)
                    .first()
                )
                
                if not active_subscription:
                    current_app.logger.warning(f"User {user_id} attempted to access premium feature without subscription")
                    flash("You need an active subscription to access this feature.", "warning")
                    return redirect(url_for('subscription.user_subscriptions'))
                
                # Check operation type and increment usage accordingly
                usage_result = True
                
                if operation_type == 'analytics':
                    usage_result = increment_analytics_usage(user_id)
                    if not usage_result:
                        flash("You have reached your analytics usage limit for this subscription plan.", "warning")
                        return redirect(url_for('subscription.user_subscriptions'))
                        
                elif operation_type == 'qr_code':
                    usage_result = increment_qr_usage(user_id)
                    if not usage_result:
                        flash("You have reached your QR code generation limit for this subscription plan.", "warning")
                        return redirect(url_for('subscription.user_subscriptions'))
                        
                else:
                    # Legacy usage tracking for backward compatibility
                    usage_result = increment_usage(user_id)
                    if not usage_result:
                        flash("Daily usage limit reached for your subscription plan.", "warning")
                        return redirect(url_for('subscription.user_subscriptions'))
                
                current_app.logger.info(f"Usage increment result: {usage_result}")
                
                # Record this usage in the log
                record_usage_log(
                    user_id=user_id,
                    subscription_id=active_subscription.id,  # Note: this is the SubscribedUser.id, not S_ID
                    operation_type=operation_type,
                    details=f"Accessed {f.__name__}"
                )
                
                # If all checks pass, execute the decorated function
                return f(*args, **kwargs)
            
            except Exception as e:
                # Comprehensive error logging
                current_app.logger.error(f"Subscription check error in {f.__name__}: {e}")
                import traceback
                current_app.logger.error(traceback.format_exc())
                flash("Subscription verification failed. Please try again later.", "danger")
                return redirect(url_for('subscription.user_subscriptions'))
        
        return wrap
    return decorator