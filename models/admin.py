import csv
from flask import Blueprint, current_app, render_template, request, redirect
from flask import url_for, session, flash, send_file, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from utils.email_service import email_service
from sqlalchemy import func, case, or_
from datetime import datetime, timedelta, UTC
from functools import wraps
import uuid
import time
import traceback
import os
import json
import string
import random
import logging
from io import BytesIO, StringIO
from .database import db
from .user import User, EmailLog
from .subscription import Subscription, SubscribedUser, SubscriptionHistory, generate_invoice_pdf
from .payment import Payment, InvoiceAddress
from .contact import ContactSubmission
from .usage_log import UsageLog 
from flask import current_app
from sqlalchemy import text, func
from razorpay import Client
from sqlalchemy.dialects.postgresql import ARRAY
# Create the blueprint
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


# ----------------------
# Admin Model Definition
# ----------------------

class Admin(db.Model):
    __tablename__ = 'admin'

    id = db.Column(db.Integer, primary_key=True)
    email_id = db.Column(db.String(120), nullable=False, unique=True)
    NAME = db.Column(db.String(50), nullable=False)
    role = db.Column(db.String(50), nullable=False)
    phone_number = db.Column(db.String(15), nullable=True)
    assigned_by = db.Column(db.String(50), nullable=False)
    permission = db.Column(ARRAY(db.String))
    password_hash = db.Column(db.String(256), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now(UTC))
    updated_at = db.Column(db.DateTime, onupdate=datetime.now(UTC))
    is_active = db.Column(db.Boolean, default=True)

    def set_password(self, password):
        """Set the password hash."""
        if password and password.strip():
            try:
                self.password_hash = generate_password_hash(password)
                return True
            except Exception as e:
                current_app.logger.error(f"Password hashing error: {str(e)}")
                return False
        return False

    def check_password(self, password):
        """Check the password against the stored hash."""
        if not self.password_hash or not password:
            return False
        try:
            return check_password_hash(self.password_hash, password)
        except Exception as e:
            current_app.logger.error(f"Password check error: {str(e)}")
            return False

    def admin_permissions(self, required_permission):
        """Check if admin has the specified permission"""
        if request.method == 'POST':
            email_id = normalize_email(request.form.get('email_id', ''))
            permissions = request.form.getlist('permissions[]')
            
            if normalize_email(self.email_id) == email_id:
                return required_permission in permissions
            
        return required_permission in self.permission if self.permission else False
    @staticmethod
    def check_permission(email_id, required_permission):
        """Static method to check permissions by email (case-insensitive)"""
        if not email_id:
            return False
            
        # Normalize email for case-insensitive lookup
        normalized_email = normalize_email(email_id)
        admin = Admin.query.filter(func.lower(Admin.email_id) == normalized_email).first()
        
        if not admin:
            return False
            
        if request.method == 'POST':
            form_email = request.form.get('email_id')
            if form_email and normalize_email(form_email) == normalized_email:
                permissions = request.form.getlist('permissions[]')
                return required_permission in permissions
                
        return admin.admin_permissions(required_permission)
    def __repr__(self):
        return f"<Admin {self.NAME} - {self.role}>"
    

# Helper decorator for admin authentication
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            flash('Please log in as admin first.', 'warning')
            return redirect(url_for('admin.admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# Helper functions
def generate_unique_invoice_number():
    """Generate a unique invoice number"""
    timestamp = datetime.now(UTC).strftime("%y%m%d")
    unique_id = str(uuid.uuid4().hex)[:8]
    return f"INV-{timestamp}-{unique_id}"

def create_or_update_subscription(payment):
    """Create or update subscription based on payment"""
    # Check if subscription already exists
    existing_sub = SubscribedUser.query.filter_by(
        U_ID=payment.user_id,
        S_ID=payment.subscription_id
    ).first()
    
    if not existing_sub:
        subscription = db.session.get(Subscription, payment.subscription_id)
        start_date = datetime.now(UTC)
        end_date = start_date + timedelta(days=subscription.days)
        
        new_subscription = SubscribedUser(
            U_ID=payment.user_id,
            S_ID=payment.subscription_id,
            start_date=start_date,
            end_date=end_date,
            current_usage=0,
            is_auto_renew=True
        )
        
        # Record subscription history
        history_entry = SubscriptionHistory(
            U_ID=payment.user_id,
            S_ID=payment.subscription_id,
            action=payment.payment_type,
            previous_S_ID=payment.previous_subscription_id
        )
        
        db.session.add(new_subscription)
        db.session.add(history_entry)

def create_invoice_address_for_payment(payment):
    """Create invoice address for payment if not exists"""
    existing_address = InvoiceAddress.query.filter_by(payment_id=payment.iid).first()
    
    if not existing_address:
        # Try to get user details
        user = User.query.get(payment.user_id)
        
        new_address = InvoiceAddress(
            payment_id=payment.iid,
            full_name=user.name,
            email=user.company_email,
            company_name=user.company_name if hasattr(user, 'company_name') else None,
            street_address=user.address if hasattr(user, 'address') else 'N/A',
            city=user.city if hasattr(user, 'city') else 'N/A',
            state=user.state if hasattr(user, 'state') else 'N/A',
            postal_code=user.postal_code if hasattr(user, 'postal_code') else 'N/A',
            gst_number=user.gst_number if hasattr(user, 'gst_number') else None
        )
        
        db.session.add(new_address)

def create_super_admin():
    """
    Create a super admin user if it doesn't already exist
    """
    # Check if super admin already exists
    super_admin_email = "manikandan@fourdm.com"  # Change this to your desired email
    existing_admin = Admin.query.filter_by(email_id=super_admin_email).first()
    
    if existing_admin:
        logging.info("Super admin already exists")
        return
    
    # Create super admin with all permissions
    super_admin = Admin(
        email_id=super_admin_email,
        NAME="Super Admin",
        role="Super Admin",
        phone_number="8122156835",  # Change this if needed
        assigned_by="System",
        permission=[
            "dashboard",
            "manage_roles", 
            "subscription_management", 
            "subscribed_users_view", 
            "user_management",
            "payments",
            "contact_submissions",
            "email_logs",
            "blogs",
            "blog_categories",
            "website_settings",
            "webstory_management"   
        ],  
        is_active=True,
        created_at=datetime.utcnow()
    )
    
    # Set a password - CHANGE THIS TO A STRONG PASSWORD!
    super_admin_password = "Test@12345"  # CHANGE THIS!
    super_admin.set_password(super_admin_password)
    
    # Add and commit
    try:
        db.session.add(super_admin)
        db.session.commit()
        logging.info(f"Super admin created successfully: {super_admin_email}")
        print(f"Super admin created successfully: {super_admin_email}")
        print(f"Password: {super_admin_password}")
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error creating super admin: {str(e)}")
        print(f"Error creating super admin: {str(e)}")        

# ----------------------
# Admin Routes
# ----------------------

@admin_bp.route('/')
@admin_required
def admin_dashboard():
    now = datetime.now(UTC)
        
        # Create a custom RecentPayment class to match template expectations
    class RecentPayment:
        def __init__(self, user, subscription, payment):
            self.user = user
            self.subscription = subscription
            self.payment = payment
            
        def format_amount(self):
            try:
                return "{:,.2f}".format(self.payment.total_amount if hasattr(self.payment, 'total_amount') else self.payment.amount)
            except (AttributeError, TypeError):
                return "0.00"

    # Get basic statistics
    total_users = User.query.count() or 0
    active_users = User.query.filter_by(email_confirmed=True).count() or 0
    unconfirmed_users = total_users - active_users
    
        # Active subscriptions - only count those that are active AND not expired
    active_subscriptions = SubscribedUser.query.filter(
        SubscribedUser.end_date > now,
        SubscribedUser._is_active == True
    ).count()
    
    # Expired subscriptions - those that have passed end_date
    expired_subscriptions = SubscribedUser.query.filter(
        SubscribedUser.end_date <= now
    ).count()

    # Revenue - ONLY FROM COMPLETED PAYMENTS
    thirty_days_ago = now - timedelta(days=30)
    total_revenue = db.session.query(func.sum(Payment.total_amount)).filter(
        Payment.status == 'completed'
    ).scalar() or 0
    
    monthly_revenue = db.session.query(func.sum(Payment.total_amount)).filter(
        Payment.status == 'completed',
        Payment.created_at >= thirty_days_ago
    ).scalar() or 0

    # Recent Payments - ONLY COMPLETED ONES
    recent_payments_query = (
        db.session.query(Payment, User, Subscription, InvoiceAddress)
        .join(User, Payment.user_id == User.id)
        .join(Subscription, Payment.subscription_id == Subscription.S_ID)
        .outerjoin(InvoiceAddress, Payment.iid == InvoiceAddress.payment_id)
        .filter(Payment.status == 'completed')  # ONLY COMPLETED
        .order_by(Payment.created_at.desc())
        .limit(10)
        .all()
    )
    recent_payments = [
        RecentPayment(user=user, subscription=subscription, payment=payment)
        for payment, user, subscription, invoice_address in recent_payments_query
    ]

    # Popular Plans - Convert to list of dicts
    popular_plans_query = (
        db.session.query(
            Subscription.plan,
            func.count(SubscribedUser.id).label('subscribers')
        )
        .join(SubscribedUser, Subscription.S_ID == SubscribedUser.S_ID)
        .filter(
            SubscribedUser.end_date > now,
            SubscribedUser._is_active == True
        )
        .group_by(Subscription.plan)
        .order_by(func.count(SubscribedUser.id).desc())
        .limit(3)
        .all()
    )
    popular_plans = [{"plan": row.plan, "subscribers": row.subscribers} for row in popular_plans_query]

    # Expiring Soon - ONLY ACTIVE SUBSCRIPTIONS
    seven_days_from_now = now + timedelta(days=7)
    expiring_soon = (
        db.session.query(User, Subscription, SubscribedUser)
        .join(SubscribedUser, User.id == SubscribedUser.U_ID)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .filter(
            SubscribedUser.end_date > now,
            SubscribedUser.end_date <= seven_days_from_now,
            SubscribedUser._is_active == True  # ONLY ACTIVE
        )
        .all()
    )
    for user, subscription, subscribed_user in expiring_soon:
        if subscribed_user.end_date.tzinfo is None:
            subscribed_user.end_date = subscribed_user.end_date.replace(tzinfo=UTC)

    # Subscription Actions (30 days) — convert to list of dicts
    subscription_actions_query = (
        db.session.query(
            SubscriptionHistory.action,
            func.count(SubscriptionHistory.id).label('count')
        )
        .filter(SubscriptionHistory.created_at >= thirty_days_ago)
        .group_by(SubscriptionHistory.action)
        .all()
    )
    subscription_actions = [{"action": row.action, "count": row.count} for row in subscription_actions_query]

    # Auto-renewal stats - ONLY ACTIVE SUBSCRIPTIONS
    auto_renewal_count = SubscribedUser.query.filter(
        SubscribedUser.is_auto_renew == True,
        SubscribedUser.end_date > now,
        SubscribedUser._is_active == True
    ).count()
    
    non_renewal_count = SubscribedUser.query.filter(
        SubscribedUser.is_auto_renew == False,
        SubscribedUser.end_date > now,
        SubscribedUser._is_active == True
    ).count()

    # Payment Types — ONLY COMPLETED PAYMENTS
    payment_types_query = (
        db.session.query(
            Payment.payment_type,
            Payment.currency,
            func.count(Payment.iid).label('count'),
            func.sum(Payment.total_amount).label('total_revenue')
        )
        .filter(Payment.status == 'completed')  # ONLY COMPLETED
        .group_by(Payment.payment_type, Payment.currency)
        .all()
    )
    payment_types = [
        {
            "payment_type": row.payment_type,
            "currency": row.currency,
            "count": row.count,
            "total_revenue": row.total_revenue
        }
        for row in payment_types_query
    ]

    # Tax Breakdown - ONLY COMPLETED PAYMENTS
    tax_breakdown_query = (
        db.session.query(
            Payment.gst_rate,
            func.sum(Payment.gst_amount).label('total_tax'),
            func.count(Payment.iid).label('payment_count')
        )
        .filter(Payment.status == 'completed')  # ONLY COMPLETED
        .group_by(Payment.gst_rate)
        .all()
    )
    tax_breakdown = [
        {
            "gst_rate": row.gst_rate,
            "total_tax": row.total_tax,
            "payment_count": row.payment_count
        }
        for row in tax_breakdown_query
    ]

    return render_template('admin/dashboard.html',
        now=now,
        total_users=total_users,
        active_users=active_users,
        unconfirmed_users=unconfirmed_users,
        active_subscriptions=active_subscriptions,
        expired_subscriptions=expired_subscriptions,
        recent_payments=recent_payments,
        total_revenue=total_revenue,
        monthly_revenue=monthly_revenue,
        popular_plans=popular_plans,
        expiring_soon=expiring_soon,
        subscription_actions=subscription_actions,
        auto_renewal_count=auto_renewal_count,
        non_renewal_count=non_renewal_count,
        payment_types=payment_types,
        tax_breakdown=tax_breakdown
    )
#-------------------------
# Admin login and logout
#-------------------------
def normalize_email(email):
    """Normalize email to lowercase and strip whitespace"""
    if not email:
        return email
    return email.strip().lower()

# Update the admin_login route to handle case-insensitive email
@admin_bp.route('/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        # Input validation
        if not email or not password:
            flash('Email and password are required.', 'danger')
            return render_template('admin/login.html')

        # Normalize email for case-insensitive lookup
        normalized_email = normalize_email(email)

        # Get admin user with case-insensitive email lookup
        admin = Admin.query.filter(func.lower(Admin.email_id) == normalized_email).first()
        
        # Check if admin exists and has password set
        if not admin:
            flash('Invalid email or password.', 'danger')
            return render_template('admin/login.html')

        # Check if password hash exists
        if not admin.password_hash:
            flash('Password not set for this admin account.', 'danger')
            return render_template('admin/login.html')
            
        # Verify password
        try:
            if admin.check_password(password):
                session['admin_id'] = admin.id
                session['admin_name'] = admin.NAME
                session['email_id'] = admin.email_id
                # Store permissions as list
                session['admin_permissions'] = admin.permission if isinstance(admin.permission, list) else []
                
                flash('Login successful!', 'success')
                return redirect(url_for('admin.admin_dashboard'))
            else:
                # This will trigger the popup modal
                flash('Invalid email or password.', 'danger')
                return render_template('admin/login.html')
        except Exception as e:
            current_app.logger.error(f"Password verification error: {str(e)}")
            flash('Error verifying password. Please contact administrator.', 'danger')
            return render_template('admin/login.html')

    return render_template('admin/login.html', email_id='')
@admin_bp.route('/logout')
@admin_required
def admin_logout():
    session.pop('admin_id', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('admin.admin_login'))


# Route to add and display roles
@admin_bp.route('/roles', methods=['GET', 'POST'])
@admin_required
def manage_roles():
    # Check if the user has permission to manage roles
    email_id = session.get('email_id')
    if not Admin.check_permission(email_id, 'manage_roles'):
        flash("You don't have permission to manage roles.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    if request.method == 'POST':
        try:
            # Get form data and normalize email
            name = request.form.get('NAME')
            email_id = normalize_email(request.form.get('email_id'))  # Normalize email
            role = request.form.get('role')
            phone_number = request.form.get('phone_number')
            password = request.form.get('password')
            permissions = request.form.getlist('permissions[]')
            
            # Validate required fields
            if not all([name, email_id, role]):
                flash('Name, email and role are required fields.', 'danger')
                return redirect(url_for('admin.manage_roles'))

            # Check for existing admin with case-insensitive email lookup
            admin_role = Admin.query.filter(func.lower(Admin.email_id) == email_id).first()

            if admin_role:
                # Update existing admin
                admin_role.NAME = name
                admin_role.email_id = email_id  # Store normalized email
                admin_role.role = role
                admin_role.phone_number = phone_number
                admin_role.permission = permissions
                admin_role.updated_at = datetime.now(UTC)
                
                 # Only update password if provided
                if password and password.strip():
                    if not admin_role.set_password(password):
                        flash('Error setting password.', 'danger')
                        return redirect(url_for('admin.manage_roles'))
                
                flash(f'Role updated successfully for {name}!', 'success')
            else:
                # Create new admin - double-check for duplicates with case-insensitive search
                existing_admin = Admin.query.filter(func.lower(Admin.email_id) == email_id).first()
                if existing_admin:
                    flash(f'An admin with email {email_id} already exists.', 'warning')
                    return redirect(url_for('admin.manage_roles'))

                if not password:
                    flash('Password is required for new admin roles.', 'danger')
                    return redirect(url_for('admin.manage_roles'))

                new_role = Admin(
                    NAME=name,
                    email_id=email_id,  # Store normalized email
                    role=role,
                    phone_number=phone_number,
                    permission=permissions,
                    assigned_by=session.get('admin_name', 'System'),
                    is_active=True,
                    created_at=datetime.now(UTC)
                )

                # Set password for new admin
                if not new_role.set_password(password):
                    flash('Error setting password.', 'danger')
                    return redirect(url_for('admin.manage_roles'))

                db.session.add(new_role)
                flash(f'New role created successfully for {name}!', 'success')

            db.session.commit()
            return redirect(url_for('admin.manage_roles'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Role management error: {str(e)}")
            flash(f'Error: {str(e)}', 'danger')
            return redirect(url_for('admin.manage_roles'))

    roles = Admin.query.all()
    return render_template('admin/roles.html', roles=roles)
@admin_bp.route('/roles/edit/<int:role_id>', methods=['GET', 'POST'])
@admin_required
def edit_role(role_id):
    role = Admin.query.get_or_404(role_id)

    if request.method == 'POST':
        try:
            # Get form data and normalize email
            role.NAME = request.form.get('NAME')
            normalized_email = normalize_email(request.form.get('email_id'))
            role.role = request.form.get('role')
            role.phone_number = request.form.get('phone_number')
            permissions = request.form.getlist('permissions[]')
            password = request.form.get('password')

            # Validate required fields
            if not all([role.NAME, normalized_email, role.role]):
                flash('Name, email and role are required fields.', 'danger')
                return redirect(url_for('admin.edit_role', role_id=role_id))

            # FIXED: Better email comparison logic
            # Normalize the current role's email for comparison
            current_normalized_email = normalize_email(role.email_id)
            
            # Check if email is being changed to an existing one (case-insensitive)
            if normalized_email != current_normalized_email:
                existing_admin = Admin.query.filter(
                    func.lower(Admin.email_id) == normalized_email,
                    Admin.id != role_id
                ).first()
                if existing_admin:
                    flash(f'An admin with email {normalized_email} already exists.', 'warning')
                    return redirect(url_for('admin.edit_role', role_id=role_id))

            # Update email with normalized version
            role.email_id = normalized_email

            # Update password if provided
            if password and password.strip():
                if not role.set_password(password):
                    flash('Error updating password.', 'danger')
                    return redirect(url_for('admin.edit_role', role_id=role_id))

            # Update other fields
            role.permission = permissions
            role.updated_at = datetime.now(UTC)

            db.session.commit()
            flash(f'Role updated successfully for {role.NAME}!', 'success')
            return redirect(url_for('admin.manage_roles'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Role update error: {str(e)}")
            flash(f'Error updating role: {str(e)}', 'danger')
            return redirect(url_for('admin.edit_role', role_id=role_id))

    # Get all other roles' emails for duplicate checking on frontend
    all_roles = Admin.query.filter(Admin.id != role_id).all()
    existing_emails = [normalize_email(r.email_id) for r in all_roles]

    return render_template('admin/edit_role.html',
                         role=role,
                         role_permissions=role.permission if role.permission else [],
                         existing_emails=existing_emails)
@admin_bp.route('/roles/delete/<int:role_id>', methods=['POST'])
@admin_required
def delete_role(role_id):
    """Delete an admin role with proper validation"""
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'manage_roles'):
        flash("You don't have permission to delete roles.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    role = Admin.query.get_or_404(role_id)
    
    # Prevent self-deletion
    current_admin_id = session.get('admin_id')
    if role.id == current_admin_id:
        flash('You cannot delete your own role.', 'danger')
        return redirect(url_for('admin.manage_roles'))
        
    # Check if this is the last super admin
    # Fix: Use PostgreSQL-specific array operations
    if role.permission and 'manage_roles' in role.permission:
        # Method 1: Using any() - checks if any element in array equals 'manage_roles'
        super_admins_count = Admin.query.filter(
            Admin.permission.any('manage_roles'),
            Admin.is_active == True,
            Admin.id != role_id
        ).count()
        
        # Alternative Method 2: Using PostgreSQL array contains operator @>
        # from sqlalchemy import text
        # super_admins_count = Admin.query.filter(
        #     text("permission @> ARRAY['manage_roles']"),
        #     Admin.is_active == True,
        #     Admin.id != role_id
        # ).count()
        
        if super_admins_count == 0:
            flash('Cannot delete the last admin with role management permissions.', 'warning')
            return redirect(url_for('admin.manage_roles'))
    
    try:    
        # Store role details for success message
        role_name = role.NAME
        role_email = role.email_id
        
        # Delete the role from database
        db.session.delete(role)
        db.session.commit()
        
        # Flash success message
        flash(f'Role for {role_name} ({role_email}) has been deleted successfully.', 'success')

        current_app.logger.info(f"Admin role deleted: {role_email} by {session.get('email_id')}")
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting role {role_id}: {str(e)}")
        flash(f'Error deleting role: {str(e)}', 'danger')
    
    return redirect(url_for('admin.manage_roles'))
# Additional route for admin.py - Detailed usage analytics
@admin_bp.route('/subscription-analytics')
@admin_required
def subscription_analytics():
    """View detailed subscription usage analytics"""
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'subscribed_users_view'):
        flash("You don't have permission to access this page.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    # Get current time
    now = datetime.now(UTC)
    
    # Get all active subscriptions with usage data
    active_subscriptions = (
        db.session.query(SubscribedUser, User, Subscription)
        .join(User, SubscribedUser.U_ID == User.id)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .filter(SubscribedUser.end_date > now)
        .filter(SubscribedUser._is_active == True)
        .all()
    )
    
    # Calculate usage statistics
    usage_stats = []
    for sub_user, user, subscription in active_subscriptions:
        # Calculate actual scan usage
        actual_scans = calculate_actual_scan_usage(user.id)
        
        # Get QR codes created during subscription period
        qr_codes_in_period = QRCode.query.filter(
            QRCode.user_id == user.id,
            QRCode.created_at >= sub_user.start_date,
            QRCode.created_at <= sub_user.end_date
        ).count()
        
        # Calculate usage percentages
        analytics_percent = (sub_user.analytics_used / subscription.analytics * 100) if subscription.analytics > 0 else 0
        qr_percent = (qr_codes_in_period / subscription.qr_count * 100) if subscription.qr_count > 0 else 0
        scan_percent = (actual_scans / subscription.scan_limit * 100) if subscription.scan_limit > 0 else 0
        
        # Days remaining
        days_remaining = max(0, (sub_user.end_date - now).days)
        
        usage_stats.append({
            'user': user,
            'subscription': subscription,
            'subscribed_user': sub_user,
            'analytics_used': sub_user.analytics_used,
            'analytics_percent': analytics_percent,
            'qr_generated': qr_codes_in_period,
            'qr_percent': qr_percent,
            'scans_used': actual_scans,
            'scan_percent': scan_percent,
            'days_remaining': days_remaining,
            'is_overuse': analytics_percent > 100 or qr_percent > 100 or scan_percent > 100
        })
    
    # Sort by usage percentage (highest first)
    usage_stats.sort(key=lambda x: max(x['analytics_percent'], x['qr_percent'], x['scan_percent']), reverse=True)
    
    # Calculate overall statistics
    total_active_users = len(usage_stats)
    high_usage_users = len([stat for stat in usage_stats if max(stat['analytics_percent'], stat['qr_percent'], stat['scan_percent']) > 80])
    overuse_users = len([stat for stat in usage_stats if stat['is_overuse']])
    
    return render_template('admin/subscription_analytics.html',
                          usage_stats=usage_stats,
                          total_active_users=total_active_users,
                          high_usage_users=high_usage_users,
                          overuse_users=overuse_users,
                          now=now)

@admin_bp.route('/subscription-analytics/export')
@admin_required
def export_subscription_analytics():
    """Export subscription analytics to CSV"""
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'subscribed_users_view'):
        flash("You don't have permission to access this feature.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    # Get current time
    now = datetime.now(UTC)
    
    # Get all active subscriptions
    active_subscriptions = (
        db.session.query(SubscribedUser, User, Subscription)
        .join(User, SubscribedUser.U_ID == User.id)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .filter(SubscribedUser.end_date > now)
        .filter(SubscribedUser._is_active == True)
        .all()
    )
    
    # Create CSV in memory
    output = StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow([
        'User Name', 'Email', 'Subscription Plan', 'Start Date', 'End Date', 'Days Remaining',
        'Analytics Used', 'Analytics Limit', 'Analytics %',
        'QR Generated', 'QR Limit', 'QR %',
        'Scans Used', 'Scan Limit', 'Scan %',
        'Auto Renew', 'Status'
    ])
    
    # Write data rows
    for sub_user, user, subscription in active_subscriptions:
        # Calculate actual usage
        actual_scans = calculate_actual_scan_usage(user.id)
        qr_codes_in_period = QRCode.query.filter(
            QRCode.user_id == user.id,
            QRCode.created_at >= sub_user.start_date,
            QRCode.created_at <= sub_user.end_date
        ).count()
        
        # Calculate percentages
        analytics_percent = (sub_user.analytics_used / subscription.analytics * 100) if subscription.analytics > 0 else 0
        qr_percent = (qr_codes_in_period / subscription.qr_count * 100) if subscription.qr_count > 0 else 0
        scan_percent = (actual_scans / subscription.scan_limit * 100) if subscription.scan_limit > 0 else 0
        
        days_remaining = max(0, (sub_user.end_date - now).days)
        
        writer.writerow([
            user.name,
            user.company_email,
            subscription.plan,
            sub_user.start_date.strftime('%Y-%m-%d'),
            sub_user.end_date.strftime('%Y-%m-%d'),
            days_remaining,
            sub_user.analytics_used,
            subscription.analytics,
            f"{analytics_percent:.1f}%",
            qr_codes_in_period,
            subscription.qr_count,
            f"{qr_percent:.1f}%",
            actual_scans,
            subscription.scan_limit,
            f"{scan_percent:.1f}%",
            'Yes' if sub_user.is_auto_renew else 'No',
            'Active'
        ])
    
    # Prepare response
    output.seek(0)
    return send_file(
        BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'subscription_analytics_{datetime.now(UTC).strftime("%Y%m%d_%H%M%S")}.csv'
    )    
#------------------------------
# admin Subscription Management
#------------------------------

@admin_bp.route('/subscriptions')
@admin_required
def admin_subscriptions():
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'subscription_management'):
        flash("You don't have permission to access this page.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    # Get all subscription plans with subscriber counts
    subscriptions = (
        db.session.query(
            Subscription,
            func.count(SubscribedUser.id).label('active_subscribers'),
            func.sum(case(
                (SubscribedUser.end_date > datetime.now(UTC), 1),
                else_=0
            )).label('active_count')
        )
        .outerjoin(SubscribedUser, Subscription.S_ID == SubscribedUser.S_ID)
        .group_by(Subscription.S_ID)
        .all()
    )
    
    # Extract the Subscription object and other data into a list of dictionaries
    subscription_data = [
        {
            "subscription": row[0],  # Subscription object
            "active_subscribers": row[1],
            "active_count": row[2]
        }
        for row in subscriptions
    ]
    
    return render_template('admin/subscriptions.html', subscriptions=subscription_data)

# Replace the admin_new_subscription and admin_edit_subscription functions with these fixed versions

@admin_bp.route('/subscriptions/new', methods=['GET', 'POST'])
@admin_required
def admin_new_subscription():
    if request.method == 'POST':
        try:
            # Get form data with validation
            plan = request.form.get('plan', '').strip()
            price_str = request.form.get('price', '').strip()
            days_str = request.form.get('days', '').strip()
            tier_str = request.form.get('tier', '1').strip()
            features = request.form.get('features', '').strip()
            plan_type = request.form.get('plan_type', 'Normal').strip()
            
            # Validate required fields
            if not plan:
                flash('Plan name is required.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
            
            if not price_str:
                flash('Price is required.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
                
            if not days_str:
                flash('Days is required.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
            
            # Convert to appropriate types with error handling
            try:
                price = float(price_str)
                if price <= 0:
                    flash('Price must be greater than 0.', 'danger')
                    return redirect(url_for('admin.admin_new_subscription'))
            except ValueError:
                flash('Invalid price format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
            
            try:
                days = int(days_str)
                if days <= 0:
                    flash('Days must be greater than 0.', 'danger')
                    return redirect(url_for('admin.admin_new_subscription'))
            except ValueError:
                flash('Invalid days format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
            
            try:
                tier = int(tier_str) if tier_str else 1
                if tier <= 0:
                    flash('Tier must be greater than 0.', 'danger')
                    return redirect(url_for('admin.admin_new_subscription'))
            except ValueError:
                flash('Invalid tier format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
            
            # New fields with validation
            designs = request.form.getlist('designs[]')
            design = ','.join(designs) if designs else ''
            
            analytics_str = request.form.get('analytics', '0').strip()
            qr_count_str = request.form.get('qr_count', '0').strip()
            scan_limit_str = request.form.get('scan_limit', '0').strip()
            
            try:
                analytics = int(analytics_str) if analytics_str else 0
                if analytics < 0:
                    flash('Analytics count cannot be negative.', 'danger')
                    return redirect(url_for('admin.admin_new_subscription'))
            except ValueError:
                flash('Invalid analytics format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
            
            try:
                qr_count = int(qr_count_str) if qr_count_str else 0
                if qr_count < 0:
                    flash('QR count cannot be negative.', 'danger')
                    return redirect(url_for('admin.admin_new_subscription'))
            except ValueError:
                flash('Invalid QR count format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
            
            try:
                scan_limit = int(scan_limit_str) if scan_limit_str else 0
                if scan_limit < 0:
                    flash('Scan limit cannot be negative.', 'danger')
                    return redirect(url_for('admin.admin_new_subscription'))
            except ValueError:
                flash('Invalid scan limit format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
            
            # Check if plan name already exists
            existing_plan = Subscription.query.filter_by(plan=plan).first()
            if existing_plan:
                flash('A subscription plan with this name already exists.', 'danger')
                return redirect(url_for('admin.admin_new_subscription'))
            
            new_subscription = Subscription(
                plan=plan,
                price=price,
                days=days,
                tier=tier,
                features=features,
                plan_type=plan_type,
                design=design,
                analytics=analytics,
                qr_count=qr_count,
                scan_limit=scan_limit
            )
            
            db.session.add(new_subscription)
            db.session.commit()
            
            flash('Subscription plan created successfully!', 'success')
            return redirect(url_for('admin.admin_subscriptions'))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating subscription: {str(e)}")
            flash(f'Error creating subscription: {str(e)}', 'danger')
            return redirect(url_for('admin.admin_new_subscription'))
    
    return render_template('admin/new_subscription.html')

@admin_bp.route('/subscriptions/edit/<int:id>', methods=['GET', 'POST'])
@admin_required
def admin_edit_subscription(id):
    subscription = db.session.get(Subscription, id)
    if not subscription:
        flash('Subscription not found.', 'danger')
        return redirect(url_for('admin.admin_subscriptions'))
    
    # Get active subscribers count
    active_subscribers = SubscribedUser.query.filter(
        SubscribedUser.S_ID == id,
        SubscribedUser.end_date > datetime.now(UTC)
    ).count()
    
    if request.method == 'POST':
        try:
            # Get form data with validation
            plan = request.form.get('plan', '').strip()
            price_str = request.form.get('price', '').strip()
            days_str = request.form.get('days', '').strip()
            tier_str = request.form.get('tier', str(subscription.tier)).strip()
            features = request.form.get('features', subscription.features or '').strip()
            plan_type = request.form.get('plan_type', subscription.plan_type or 'Normal').strip()
            
            # Validate required fields
            if not plan:
                flash('Plan name is required.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
            
            if not price_str:
                flash('Price is required.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
                
            if not days_str:
                flash('Days is required.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
            
            # Convert to appropriate types with error handling
            try:
                price = float(price_str)
                if price <= 0:
                    flash('Price must be greater than 0.', 'danger')
                    return redirect(url_for('admin.admin_edit_subscription', id=id))
            except ValueError:
                flash('Invalid price format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
            
            try:
                days = int(days_str)
                if days <= 0:
                    flash('Days must be greater than 0.', 'danger')
                    return redirect(url_for('admin.admin_edit_subscription', id=id))
            except ValueError:
                flash('Invalid days format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
            
            try:
                tier = int(tier_str) if tier_str else subscription.tier
                if tier <= 0:
                    flash('Tier must be greater than 0.', 'danger')
                    return redirect(url_for('admin.admin_edit_subscription', id=id))
            except ValueError:
                flash('Invalid tier format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
            
            # New fields with validation
            designs = request.form.getlist('designs[]')
            design = ','.join(designs) if designs else subscription.design or ''
            
            analytics_str = request.form.get('analytics', str(subscription.analytics or 0)).strip()
            qr_count_str = request.form.get('qr_count', str(subscription.qr_count or 0)).strip()
            scan_limit_str = request.form.get('scan_limit', str(subscription.scan_limit or 0)).strip()
            
            try:
                analytics = int(analytics_str) if analytics_str else (subscription.analytics or 0)
                if analytics < 0:
                    flash('Analytics count cannot be negative.', 'danger')
                    return redirect(url_for('admin.admin_edit_subscription', id=id))
            except ValueError:
                flash('Invalid analytics format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
            
            try:
                qr_count = int(qr_count_str) if qr_count_str else (subscription.qr_count or 0)
                if qr_count < 0:
                    flash('QR count cannot be negative.', 'danger')
                    return redirect(url_for('admin.admin_edit_subscription', id=id))
            except ValueError:
                flash('Invalid QR count format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
            
            try:
                scan_limit = int(scan_limit_str) if scan_limit_str else (subscription.scan_limit or 0)
                if scan_limit < 0:
                    flash('Scan limit cannot be negative.', 'danger')
                    return redirect(url_for('admin.admin_edit_subscription', id=id))
            except ValueError:
                flash('Invalid scan limit format. Please enter a valid number.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
            
            # Check if plan name already exists with a different ID
            existing_plan = Subscription.query.filter(
                Subscription.plan == plan,
                Subscription.plan_type == plan_type,
                Subscription.S_ID != id
            ).first()
            
            if existing_plan:
                flash('A subscription plan with this name already exists.', 'danger')
                return redirect(url_for('admin.admin_edit_subscription', id=id))
            
            # Update subscription
            subscription.plan = plan
            subscription.price = price
            subscription.days = days
            subscription.tier = tier
            subscription.features = features
            subscription.plan_type = plan_type
            subscription.design = design
            subscription.analytics = analytics
            subscription.qr_count = qr_count
            subscription.scan_limit = scan_limit
            
            db.session.commit()
            
            flash('Subscription plan updated successfully!', 'success')
            return redirect(url_for('admin.admin_subscriptions'))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating subscription: {str(e)}")
            flash(f'Error updating subscription: {str(e)}', 'danger')
            return redirect(url_for('admin.admin_edit_subscription', id=id))
    
    return render_template('admin/edit_subscription.html', 
                          subscription=subscription,
                          active_subscribers=active_subscribers)
# Add these routes to your Flask application

@admin_bp.route('/subscriptions/archive/<int:id>', methods=['POST'])
@admin_required
def admin_archive_subscription(id):
    subscription = db.session.get(Subscription, id)
    
    # Check if already archived
    if subscription.archived_at:
        flash('This subscription plan is already archived.', 'warning')
        return redirect(url_for('admin.admin_subscriptions'))
    
    # Archive the subscription plan
    subscription.is_active = False
    subscription.archived_at = datetime.now(UTC)
    db.session.commit()
    
    flash('Subscription plan has been archived successfully.', 'success')
    return redirect(url_for('admin.admin_subscriptions'))


@admin_bp.route('/subscriptions/restore/<int:id>', methods=['POST'])
def admin_restore_subscription(id):
    subscription = db.session.get(Subscription, id)
    
    # Check if not archived
    if not subscription.archived_at:
        flash('This subscription plan is not archived.', 'warning')
        return redirect(url_for('admin.admin_subscriptions'))
    
    # Restore the subscription plan
    subscription.is_active = True
    subscription.archived_at = None
    db.session.commit()
    
    flash('Subscription plan has been restored successfully.', 'success')
    return redirect(url_for('admin.admin_subscriptions'))

@admin_bp.route('/subscriptions/delete/<int:id>', methods=['POST'])
def admin_delete_subscription(id):
    subscription = db.session.get(Subscription, id)
    
    # Check if there are any users subscribed to this plan (active or inactive)
    if subscription.subscribed_users:
        flash('Cannot delete subscription plan as it has users associated with it. Please remove the user subscriptions first.', 'danger')
        return redirect(url_for('admin.admin_subscriptions'))
    
    # Check if there are any payments or history records associated with this plan
    payment_count = Payment.query.filter_by(subscription_id=id).count()
    history_count = SubscriptionHistory.query.filter(
        (SubscriptionHistory.S_ID == id) | 
        (SubscriptionHistory.previous_S_ID == id)
    ).count()
    
    if payment_count > 0 or history_count > 0:
        # Instead of blocking, mark as archived
        subscription.is_active = False
        subscription.archived_at = datetime.now(UTC)
        db.session.commit()
        
        flash('Subscription plan has been archived as it has payment or history records associated with it.', 'warning')
        return redirect(url_for('admin.admin_subscriptions'))
    
    # If no constraints, perform actual deletion
    db.session.delete(subscription)
    db.session.commit()
    
    flash('Subscription plan deleted successfully!', 'success')
    return redirect(url_for('admin.admin_subscriptions'))
def ensure_aware(dt):
    """Convert naive datetime to UTC-aware datetime if needed"""
    if dt is None:
        return dt
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=UTC)
    return dt
    
@admin_bp.route('/subscribed-users')
@admin_required
def admin_subscribed_users():
    """Display all subscribed users with filtering options"""

    # Get filter parameters
    status_filter = request.args.get('status', 'all')
    plan_filter = request.args.get('plan', 'all')
    email_filter = request.args.get('email', '').strip()

    # Base query
    query = db.session.query(SubscribedUser, User, Subscription).join(
        User, SubscribedUser.U_ID == User.id
    ).join(
        Subscription, SubscribedUser.S_ID == Subscription.S_ID
    )

    # Apply email filter
    if email_filter:
        query = query.filter(User.company_email.ilike(f'%{email_filter}%'))

    # Apply status filter
    now = datetime.now(UTC)
    if status_filter == 'active':
        query = query.filter(
            SubscribedUser._is_active == True,
            SubscribedUser.end_date > now
        )
    elif status_filter == 'cancelled':
        query = query.filter(
            SubscribedUser._is_active == False,
            SubscribedUser.end_date > now
        )
    elif status_filter == 'expired':
        query = query.filter(SubscribedUser.end_date <= now)

    # Apply plan filter
    if plan_filter != 'all':
        try:
            plan_id = int(plan_filter)
            query = query.filter(Subscription.S_ID == plan_id)
        except ValueError:
            pass

    # Get results ordered by most recent first
    subscribed_users = query.order_by(SubscribedUser.start_date.desc()).all()
    for sub, user, plan in subscribed_users:
        sub.start_date = ensure_aware(sub.start_date)
        sub.end_date = ensure_aware(sub.end_date)


    # Calculate statistics
    total_subscriptions = len(subscribed_users)
    active_subscriptions = sum(1 for sub, user, plan in subscribed_users
                            if sub._is_active and ensure_aware(sub.end_date) > now)
    cancelled_subscriptions = sum(1 for sub, user, plan in subscribed_users
                                if not sub._is_active and ensure_aware(sub.end_date) > now)

    # Get expiring soon count (within 7 days)
    # Get expiring soon count (within 7 days)
    expiring_soon_count = sum(1 for sub, user, plan in subscribed_users
                            if sub._is_active and ensure_aware(sub.end_date) > now and
                            ensure_aware(sub.end_date) <= now + timedelta(days=7))

    # Get all plans for filter dropdown
    all_plans = Subscription.query.filter(
        Subscription.is_active == True,
        Subscription.archived_at.is_(None)
    ).all()
    
    return render_template(
        'admin/subscribed_users.html',
        subscribed_users=subscribed_users,
        total_subscriptions=total_subscriptions,
        active_subscriptions=active_subscriptions,
        cancelled_subscriptions=cancelled_subscriptions,
        expiring_soon_count=expiring_soon_count,
        all_plans=all_plans,
        status_filter=status_filter,
        plan_filter=plan_filter,
        email_filter=email_filter,
        now=now,
        hasattr=hasattr
    )

@admin_bp.route('/subscribed-users/new', methods=['GET', 'POST'])
@admin_required
def admin_new_subscribed_user():
    if request.method == 'POST':
        user_id = int(request.form.get('user_id'))
        subscription_id = int(request.form.get('subscription_id'))
        auto_renew = request.form.get('auto_renew', 'off') == 'on'  # Added auto-renewal field
        
        # Check if user exists
        user = db.session.get(User, user_id)
        if not user:
            flash('User not found.', 'danger')
            return redirect(url_for('admin.admin_new_subscribed_user'))
        
        # Check if subscription exists
        subscription = db.session.get(Subscription, subscription_id)
        if not subscription:
            flash('Subscription plan not found.', 'danger')
            return redirect(url_for('admin.admin_new_subscribed_user'))
        
        
        # Check if user already has this subscription
        existing_sub = SubscribedUser.query.filter(
            SubscribedUser.U_ID == user_id,
            SubscribedUser.S_ID == subscription_id,
            SubscribedUser.end_date > datetime.now(UTC)
        ).first()
        
        if existing_sub:
            flash('User already has an active subscription to this plan.', 'warning')
            return redirect(url_for('admin.admin_subscribed_users'))
        
        # Calculate dates
        start_date = datetime.now(UTC)
        end_date = start_date + timedelta(days=subscription.days)
        
        new_subscribed_user = SubscribedUser(
            U_ID=user_id,
            S_ID=subscription_id,
            start_date=start_date,
            end_date=end_date,
            current_usage=0,
            is_auto_renew=auto_renew  # Added auto-renewal
        )
        
        new_payment = Payment(
            base_amount=subscription.price,  # Changed from 'amount' to 'base_amount'
            user_id=user_id,
            subscription_id=subscription_id,
            razorpay_order_id=f"manual_admin_{int(time.time())}",
            razorpay_payment_id=f"manual_admin_{int(time.time())}",
            currency='INR',
            status='completed',
            payment_type='new',
            created_at= datetime.now(UTC)
        )
        
        # Add subscription history record
        new_history = SubscriptionHistory(
            U_ID=user_id,
            S_ID=subscription_id,
            action='new',
            created_at=datetime.now(UTC)
        )
        
        db.session.add(new_subscribed_user)
        db.session.add(new_payment)
        db.session.add(new_history)
        db.session.commit()
        
        flash('User subscription added successfully with payment record!', 'success')
        return redirect(url_for('admin.admin_subscribed_users'))
    
    # Get all active users (email confirmed)
    users = User.query.filter_by(email_confirmed=True).all()
    
    # Get all subscription plans
    subscriptions = Subscription.query.all()
    
    return render_template('admin/new_subscribed_user.html', 
                          users=users, 
                          subscriptions=subscriptions)
@admin_bp.route('/admin/subscribed-users/reactivate/<int:id>', methods=['POST'])
@admin_required
def admin_reactivate_subscription(id):
    """Reactivate a cancelled subscription"""
    subscribed_user = SubscribedUser.query.get_or_404(id)
    
    # Check if subscription is actually cancelled and not expired
    if subscribed_user._is_active:
        flash('This subscription is already active.', 'warning')
        return redirect(url_for('admin.admin_subscribed_users'))
    
    if subscribed_user.end_date <= datetime.now(UTC):
        flash('Cannot reactivate an expired subscription. Please create a new subscription.', 'danger')
        return redirect(url_for('admin.admin_subscribed_users'))
    
    try:
        # Reactivate the subscription
        subscribed_user._is_active = True
        
        # Create a history record for reactivation
        history_record = SubscriptionHistory(
            U_ID=subscribed_user.U_ID,
            S_ID=subscribed_user.S_ID,
            action='reactivate',
            created_at=datetime.now(UTC)
        )
        
        db.session.add(history_record)
        db.session.commit()
        
        # Get user details for the flash message
        user = User.query.get(subscribed_user.U_ID)
        subscription = Subscription.query.get(subscribed_user.S_ID)
        
        flash(f'Subscription for {user.name} to {subscription.plan} plan has been reactivated successfully!', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error reactivating subscription: {str(e)}")
        flash(f'Error reactivating subscription: {str(e)}', 'danger')
    
    return redirect(url_for('admin.admin_subscribed_users'))


@admin_bp.route('/subscribed-users/edit/<int:id>', methods=['GET', 'POST'])
@admin_required
def admin_edit_subscribed_user(id):
    """Edit a subscribed user's subscription details"""
    
    # Get the subscribed user record
    subscribed_user = SubscribedUser.query.get_or_404(id)
    
    # Get related user and current subscription details
    user = User.query.get_or_404(subscribed_user.U_ID)
    current_subscription = Subscription.query.get_or_404(subscribed_user.S_ID)
    
    if request.method == 'GET':
        # Get all available subscription plans for the dropdown
        subscriptions = Subscription.query.filter(
            Subscription.is_active == True,
            Subscription.archived_at.is_(None)
        ).all()
        
        return render_template(
            'admin/edit_subscribed_user.html',
            subscribed_user=subscribed_user,
            user=user,
            subscriptions=subscriptions,
            current_subscription=current_subscription
        )
    
    elif request.method == 'POST':
        try:
            # Get form data
            subscription_id = request.form.get('subscription_id', type=int)
            start_date_str = request.form.get('start_date')
            end_date_str = request.form.get('end_date')
            # Don't get usage counts from form - they should only be modified by actual usage or increment/decrement buttons
            # current_usage, analytics_used, qr_generated, scans_used are preserved from existing values
            is_active = request.form.get('is_active') == 'on'  # Checkbox value
            auto_renew = request.form.get('auto_renew') == 'on'  # Checkbox value
            
            # Check if this is an AJAX request
            is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            
            # Validate required fields
            if not subscription_id or not start_date_str or not end_date_str:
                error_msg = 'Please fill in all required fields.'
                if is_ajax:
                    return jsonify({'success': False, 'error': error_msg}), 400
                flash(error_msg, 'danger')
                return redirect(url_for('admin.admin_edit_subscribed_user', id=id))
            
            # Parse dates
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
                
                # Make dates timezone aware
                start_date = start_date.replace(tzinfo=UTC)
                end_date = end_date.replace(tzinfo=UTC)
                
            except ValueError:
                error_msg = 'Invalid date format. Please use YYYY-MM-DD format.'
                if is_ajax:
                    return jsonify({'success': False, 'error': error_msg}), 400
                flash(error_msg, 'danger')
                return redirect(url_for('admin.admin_edit_subscribed_user', id=id))
            
            # Validate date logic
            if end_date <= start_date:
                error_msg = 'End date must be after start date.'
                if is_ajax:
                    return jsonify({'success': False, 'error': error_msg}), 400
                flash(error_msg, 'danger')
                return redirect(url_for('admin.admin_edit_subscribed_user', id=id))
            
            # Validate subscription exists and is active
            new_subscription = Subscription.query.filter(
                Subscription.S_ID == subscription_id,
                Subscription.is_active == True,
                Subscription.archived_at.is_(None)
            ).first()
            
            if not new_subscription:
                error_msg = 'Selected subscription plan is not available.'
                if is_ajax:
                    return jsonify({'success': False, 'error': error_msg}), 400
                flash(error_msg, 'danger')
                return redirect(url_for('admin.admin_edit_subscribed_user', id=id))

            # Check if subscription plan is changing
            plan_changed = subscribed_user.S_ID != subscription_id
            
            # Update the subscribed user record
            # Note: current_usage, analytics_used, qr_generated, scans_used are NOT updated here
            # They should only be modified by actual usage or via increment/decrement buttons
            subscribed_user.S_ID = subscription_id
            subscribed_user.start_date = start_date
            subscribed_user.end_date = end_date
            subscribed_user._is_active = is_active
            subscribed_user.is_auto_renew = auto_renew
            
            # Update last usage reset if dates changed
            if subscribed_user.start_date != start_date:
                subscribed_user.last_usage_reset = start_date
            
            # If this subscription is being activated, deactivate any other active subscriptions for this user
            if is_active:
                other_active_subs = SubscribedUser.query.filter(
                    SubscribedUser.U_ID == subscribed_user.U_ID,
                    SubscribedUser.id != subscribed_user.id,
                    SubscribedUser._is_active == True
                ).all()
                
                for other_sub in other_active_subs:
                    other_sub._is_active = False
                    
                    # Add history entry for deactivation
                    history_entry = SubscriptionHistory(
                        U_ID=subscribed_user.U_ID,
                        S_ID=other_sub.S_ID,
                        action='deactivated_by_admin',
                        previous_S_ID=other_sub.S_ID,
                        created_at=datetime.now(UTC)
                    )
                    db.session.add(history_entry)
            
            # Add subscription history entry for the edit
            action = 'plan_changed' if plan_changed else 'updated'
            history_entry = SubscriptionHistory(
                U_ID=subscribed_user.U_ID,
                S_ID=subscription_id,
                action=action,
                previous_S_ID=current_subscription.S_ID if plan_changed else None,
                created_at=datetime.now(UTC)
            )
            db.session.add(history_entry)
            
            # Commit all changes
            db.session.commit()
            
            # Success message
            if plan_changed:
                success_msg = f'Subscription updated successfully! Plan changed from {current_subscription.plan} to {new_subscription.plan}.'
            else:
                success_msg = 'Subscription updated successfully!'
            
            # Log the admin action
            current_app.logger.info(f"Admin updated subscription {id} for user {user.company_email}. Plan: {new_subscription.plan}, Active: {is_active}")
            
            # Handle AJAX vs regular request
            if is_ajax:
                return jsonify({
                    'success': True, 
                    'message': success_msg,
                    'redirect_url': url_for('admin.admin_subscribed_users')
                })
            else:
                flash(success_msg, 'success')
                return redirect(url_for('admin.admin_subscribed_users'))
            
        except Exception as e:
            # Rollback on error
            db.session.rollback()
            error_msg = f'Error updating subscription: {str(e)}'
            current_app.logger.error(f"Error updating subscribed user {id}: {str(e)}")
            
            # Handle AJAX vs regular request for errors
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': error_msg}), 500
            else:
                flash(error_msg, 'danger')
                return redirect(url_for('admin.admin_edit_subscribed_user', id=id))
# Helper function to get subscription usage statistics for admin dashboard
def get_subscription_usage_stats(subscribed_user):
    """Get comprehensive usage statistics for a subscription"""
    
    subscription = subscribed_user.subscription
    
    # Calculate usage percentages
    usage_stats = {
        'analytics': {
            'used': subscribed_user.analytics_used,
            'total': subscription.analytics,
            'remaining': max(0, subscription.analytics - subscribed_user.analytics_used),
            'percentage': (subscribed_user.analytics_used / subscription.analytics * 100) if subscription.analytics > 0 else 0
        },
        'qr_codes': {
            'used': subscribed_user.qr_generated,
            'total': subscription.qr_count,
            'remaining': max(0, subscription.qr_count - subscribed_user.qr_generated),
            'percentage': (subscribed_user.qr_generated / subscription.qr_count * 100) if subscription.qr_count > 0 else 0
        },
        'scans': {
            'used': subscribed_user.scans_used or 0,
            'total': subscription.scan_limit,
            'remaining': max(0, subscription.scan_limit - (subscribed_user.scans_used or 0)),
            'percentage': ((subscribed_user.scans_used or 0) / subscription.scan_limit * 100) if subscription.scan_limit > 0 else 0
        },
        'daily_usage': {
            'used': subscribed_user.current_usage,
            'total': getattr(subscription, 'usage_per_day', 0),
            'percentage': (subscribed_user.current_usage / getattr(subscription, 'usage_per_day', 1) * 100) if getattr(subscription, 'usage_per_day', 0) > 0 else 0
        }
    }
    
    return usage_stats



@admin_bp.route('/subscribed-users/extend/<int:id>', methods=['POST'])
@admin_required
def admin_extend_subscription(id):
    subscribed_user = SubscribedUser.query.get_or_404(id)
    extension_days = int(request.form.get('extension_days', 0))
    
    if extension_days <= 0:
        flash('Extension days must be positive.', 'danger')
    elif not subscribed_user._is_active:
        flash('Cannot extend a cancelled subscription. Please reactivate it first.', 'warning')
    else:
        # Extend the subscription
        current_end_date = subscribed_user.end_date
        new_end_date = current_end_date + timedelta(days=extension_days)
        subscribed_user.end_date = new_end_date
        
        # Create a history record for this extension
        history_record = SubscriptionHistory(
            U_ID=subscribed_user.U_ID,
            S_ID=subscribed_user.S_ID,
            action='extend',
            created_at=datetime.now(UTC)
        )
        
        db.session.add(history_record)
        db.session.commit()
        flash(f'Subscription extended by {extension_days} days successfully!', 'success')
    
    return redirect(url_for('admin.admin_subscribed_users'))

@admin_bp.route('/subscribed-users/delete/<int:id>', methods=['POST'])
@admin_required
def admin_delete_subscribed_user(id):
    subscribed_user = SubscribedUser.query.get_or_404(id)
    
    # Get user details for the flash message
    user = User.query.get(subscribed_user.U_ID)
    subscription = Subscription.query.get(subscribed_user.S_ID)
    try:
        # Check if there are any usage logs associated with this subscription
        usage_logs = UsageLog.query.filter_by(subscription_id=id).all()
        
        if usage_logs:
            # Find if user has any other active subscription
            other_subscription = SubscribedUser.query.filter(
                SubscribedUser.U_ID == subscribed_user.U_ID,
                SubscribedUser.id != id,
                SubscribedUser.end_date > datetime.now(UTC)
            ).first()
            
            if other_subscription:
                # Reassign logs to that subscription
                for log in usage_logs:
                    log.subscription_id = other_subscription.id
                db.session.flush()  # Flush changes before deletion
            else:
                # Delete the usage logs since there's no other subscription
                for log in usage_logs:
                    db.session.delete(log)
                db.session.flush()  # Flush changes before deletion
        
        # Create a history record for cancellation
        history_record = SubscriptionHistory(
            U_ID=subscribed_user.U_ID,
            S_ID=subscribed_user.S_ID,
            action='admin_delete',
            created_at=datetime.now(UTC)
        )
        
        db.session.add(history_record)
        db.session.delete(subscribed_user)
        db.session.commit()
    
        flash(f'Subscription for {user.name} to {subscription.plan} plan deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting subscription: {str(e)}', 'danger')
        current_app.logger.error(f"Error deleting subscription: {str(e)}")
    
    return redirect(url_for('admin.admin_subscribed_users'))
# Add these helper functions at the top of your app.py file after imports
import re
from sqlalchemy import or_, func

def get_user_status_display(user):
    """Returns user account status (separate from subscription status)"""
    if user.email_confirmed:
        return ("Active", "bg-success", "fas fa-check-circle")
    else:
        return ("Unconfirmed", "bg-warning", "fas fa-exclamation-triangle")

def validate_user_data(name, email, password, user_id=None):
    """Validate user data and return list of errors"""
    errors = []
    
    # Name validation
    if not name:
        errors.append("Name is required.")
    elif len(name) < 2:
        errors.append("Name must be at least 2 characters long.")
    elif len(name) > 100:
        errors.append("Name cannot exceed 100 characters.")
    
    # Email validation
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not email:
        errors.append("Email is required.")
    elif not re.match(email_pattern, email):
        errors.append("Please enter a valid email address.")
    elif len(email) > 255:
        errors.append("Email address is too long.")
    else:
        # Check if email already exists (exclude current user if editing)
        query = User.query.filter(func.lower(User.company_email) == email.lower())
        if user_id:
            query = query.filter(User.id != user_id)
        existing_user = query.first()
        if existing_user:
            errors.append("A user with this email already exists.")
    
    # Password validation (only if password is provided)
    if password:
        if len(password) < 8:
            errors.append("Password must be at least 8 characters long.")
        elif len(password) > 128:
            errors.append("Password cannot exceed 128 characters.")
        else:
            # Check password complexity
            password_errors = []
            if not re.search(r'[A-Z]', password):
                password_errors.append("one uppercase letter")
            if not re.search(r'[a-z]', password):
                password_errors.append("one lowercase letter")
            if not re.search(r'[0-9]', password):
                password_errors.append("one number")
            if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
                password_errors.append("one special character")
            
            if password_errors:
                errors.append(f"Password must contain at least {', '.join(password_errors)}.")
    
    return errors

@admin_bp.route('/users')
@admin_required
def admin_users():
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'user_management'):
        flash("You don't have permission to access this page.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    # Get filter parameters
    status_filter = request.args.get('status', 'all')
    search_query = request.args.get('search', '')
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    # Start with base query
    query = User.query
    
    # Apply filters
    if status_filter == 'active':
        query = query.filter_by(email_confirmed=True)
    elif status_filter == 'unconfirmed':
        query = query.filter_by(email_confirmed=False)
    elif status_filter == 'admin':
        query = query.filter_by(is_admin=True)
    
    # Apply search if provided
    if search_query:
        search_filter = or_(
            User.name.ilike(f'%{search_query}%'),
            User.company_email.ilike(f'%{search_query}%')
        )
        query = query.filter(search_filter)
    
    # Execute query with pagination
    pagination = query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    # Get subscription status for each user (separate from user account status)
    user_subscriptions = {}
    for user in pagination.items:
        active_sub = (
            db.session.query(SubscribedUser, Subscription)
            .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
            .filter(
                SubscribedUser.U_ID == user.id,
                SubscribedUser.end_date > datetime.now(UTC),
                SubscribedUser._is_active == True
            )
            .first()
        )
        
        # Fix: Move the unpacking and dictionary creation inside the loop
        if active_sub:
            # Unpack the tuple
            subscribed_user, subscription = active_sub
            
            # Store both the subscription object and relevant data
            user_subscriptions[user.id] = {
                'subscription': subscription,
                'subscribed_user': subscribed_user,
                'plan_name': subscription.plan,
                'plan_type': subscription.plan_type or 'Normal'
            }
        else:
            # No active subscription for this user
            user_subscriptions[user.id] = None
    
    # Calculate user statistics (based on user account status, not subscription)
    total_users = User.query.count()
    active_users = User.query.filter_by(email_confirmed=True).count()
    unconfirmed_users = User.query.filter_by(email_confirmed=False).count()
    admin_users = User.query.filter_by(is_admin=True).count()
    
    # Calculate subscription statistics separately
    now = datetime.now(UTC)
    users_with_active_subscriptions = db.session.query(
        func.count(func.distinct(SubscribedUser.U_ID))
    ).filter(
        SubscribedUser.end_date > now,
        SubscribedUser._is_active == True
    ).scalar() or 0
    
    # Add debug logging
    current_app.logger.debug(f"User subscriptions: {user_subscriptions}")
    
    return render_template('admin/users.html', 
                           users=pagination.items,
                           pagination=pagination,
                           user_subscriptions=user_subscriptions,
                           status_filter=status_filter,
                           search_query=search_query,
                           # User account statistics
                           total_users=total_users,
                           active_users=active_users,
                           unconfirmed_users=unconfirmed_users,
                           admin_users=admin_users,
                           # Subscription statistics
                           users_with_active_subscriptions=users_with_active_subscriptions,
                           # Helper functions
                           get_user_status_display=get_user_status_display)
@admin_bp.route('/users/<int:user_id>')
@admin_required
def admin_user_details(user_id):
    user = db.session.get(User, user_id)

    # Check if user exists
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.admin_users'))

    # Get user's subscription history
    subscriptions = (
        db.session.query(SubscribedUser, Subscription)
        .join(Subscription, SubscribedUser.S_ID == Subscription.S_ID)
        .filter(SubscribedUser.U_ID == user_id)
        .order_by(SubscribedUser.start_date.desc())
        .all()
    )
    
    # Get user's payment history
    payments = (
        db.session.query(Payment, Subscription)
        .join(Subscription, Payment.subscription_id == Subscription.S_ID)
        .filter(Payment.user_id == user_id)
        .order_by(Payment.created_at.desc())
        .all()
    )
    
    # Get user's QR codes using raw SQL to avoid model import issues
    user_qr_codes = []
    try:
        # Use raw SQL query to get QR codes with scan count
        query = """
            SELECT 
                qr.id, 
                qr.unique_id, 
                qr.name, 
                qr.qr_type, 
                qr.is_dynamic, 
                qr.created_at,
                qr.color, 
                qr.background_color, 
                qr.shape, 
                qr.module_size, 
                qr.inner_eye_color, 
                qr.outer_eye_color,
                COALESCE(COUNT(s.id), 0) as scan_count
            FROM qr_code qr
            LEFT JOIN scan s ON qr.id = s.qr_code_id
            WHERE qr.user_id = :user_id 
            GROUP BY qr.id, qr.unique_id, qr.name, qr.qr_type, qr.is_dynamic, 
                     qr.created_at, qr.color, qr.background_color, qr.shape, 
                     qr.module_size, qr.inner_eye_color, qr.outer_eye_color
            ORDER BY qr.created_at DESC
        """
        from sqlalchemy import text
        result = db.session.execute(text(query), {"user_id": user_id})
        
        # Convert raw results to dictionary-like objects
        for row in result:
            qr_dict = {
                'id': row[0],
                'unique_id': row[1], 
                'name': row[2],
                'qr_type': row[3],
                'is_dynamic': row[4],
                'created_at': row[5],
                'color': row[6],
                'background_color': row[7],
                'shape': row[8],
                'module_size': row[9],
                'inner_eye_color': row[10],
                'outer_eye_color': row[11],
                'scan_count': row[12]
            }
            user_qr_codes.append(type('QRCode', (), qr_dict))
    except Exception as e:
        current_app.logger.error(f"Error fetching QR codes: {str(e)}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        user_qr_codes = []
    
    # Get subscription plans for modals
    subscription_plans = Subscription.query.filter_by(is_active=True).all()
    
    # Make sure all datetime objects are timezone-aware
    for sub, _ in subscriptions:
        if sub.start_date.tzinfo is None:
            sub.start_date = sub.start_date.replace(tzinfo=UTC)
        if sub.end_date.tzinfo is None:
            sub.end_date = sub.end_date.replace(tzinfo=UTC)

    for payment, _ in payments:
        if payment.created_at.tzinfo is None:
            payment.created_at = payment.created_at.replace(tzinfo=UTC)
        if payment.invoice_date and payment.invoice_date.tzinfo is None:
            payment.invoice_date = payment.invoice_date.replace(tzinfo=UTC)
    
    # Calculate current date for checking subscription status
    now = datetime.now(UTC)
    
    return render_template('admin/user_details.html',
                          user=user,
                          subscriptions=subscriptions,
                          payments=payments,
                          user_qr_codes=user_qr_codes,
                          subscription_plans=subscription_plans,
                          now=now)

@admin_bp.route('/users/<int:user_id>/add-subscription', methods=['POST'])
@admin_required
def admin_add_user_subscription(user_id):
    """Add a subscription to a specific user from user details page"""
    try:
        # Get form data
        subscription_id = int(request.form.get('subscription_id'))
        start_date_str = request.form.get('start_date')
        end_date_str = request.form.get('end_date')
        auto_renew = request.form.get('is_auto_renew') == 'on'

        # Check if user exists
        user = db.session.get(User, user_id)
        if not user:
            flash('User not found.', 'danger')
            return redirect(url_for('admin.admin_user_details', user_id=user_id))

        # Check if subscription exists
        subscription = db.session.get(Subscription, subscription_id)
        if not subscription:
            flash('Subscription plan not found.', 'danger')
            return redirect(url_for('admin.admin_user_details', user_id=user_id))

        # Parse dates
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')

            # Make dates timezone aware
            start_date = start_date.replace(tzinfo=UTC)
            end_date = end_date.replace(tzinfo=UTC)

        except ValueError:
            flash('Invalid date format.', 'danger')
            return redirect(url_for('admin.admin_user_details', user_id=user_id))

        # Validate date logic
        if end_date <= start_date:
            flash('End date must be after start date.', 'danger')
            return redirect(url_for('admin.admin_user_details', user_id=user_id))

        # Check if user already has an active subscription to this plan
        existing_sub = SubscribedUser.query.filter(
            SubscribedUser.U_ID == user_id,
            SubscribedUser.S_ID == subscription_id,
            SubscribedUser.end_date > datetime.now(UTC),
            SubscribedUser._is_active == True
        ).first()

        if existing_sub:
            flash(f'User already has an active subscription to {subscription.plan} plan.', 'warning')
            return redirect(url_for('admin.admin_user_details', user_id=user_id))

        # Create new subscription
        new_subscribed_user = SubscribedUser(
            U_ID=user_id,
            S_ID=subscription_id,
            start_date=start_date,
            end_date=end_date,
            current_usage=0,
            analytics_used=0,
            qr_generated=0,
            scans_used=0,
            is_auto_renew=auto_renew,
            _is_active=True
        )

        # Create corresponding payment record
        new_payment = Payment(
            base_amount=subscription.price,
            user_id=user_id,
            subscription_id=subscription_id,
            razorpay_order_id=f"manual_admin_{int(time.time())}_{user_id}",
            razorpay_payment_id=f"manual_admin_{int(time.time())}_{user_id}",
            currency='INR',
            status='completed',
            payment_type='new',
            created_at=datetime.now(UTC)
        )

        # Add subscription history record
        new_history = SubscriptionHistory(
            U_ID=user_id,
            S_ID=subscription_id,
            action='new',
            created_at=datetime.now(UTC)
        )

        # Save everything to database
        db.session.add(new_subscribed_user)
        db.session.add(new_payment)
        db.session.add(new_history)
        db.session.commit()

        flash(f'Subscription {subscription.plan} successfully added to {user.name}!', 'success')

    except ValueError:
        flash('Invalid subscription ID.', 'danger')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error adding subscription to user {user_id}: {str(e)}")
        flash(f'Error adding subscription: {str(e)}', 'danger')

    return redirect(url_for('admin.admin_user_details', user_id=user_id))

@admin_bp.route('/remove_user/<int:user_id>', methods=['POST'])
@admin_required
def remove_user(user_id):
    """
    Remove a user and all associated data from the system.
    This function carefully handles all foreign key relationships
    by deleting related records in the correct order.
    """
    # Fetch the user by ID
    user = db.session.get(User, user_id)
    
    # Check if the user has active subscriptions
    active_subscription = SubscribedUser.query.filter(
        SubscribedUser.U_ID == user_id,
        SubscribedUser.end_date > datetime.now(UTC),
        SubscribedUser._is_active == True

    ).first()
    
    if active_subscription:
        flash('Cannot delete user with active subscriptions. Please remove their subscriptions first.', 'warning')
        return redirect(url_for('admin.admin_users'))
    
    # Check if user is an admin
    if user.is_admin:
        flash('Cannot delete an admin user.', 'danger')
        return redirect(url_for('admin.admin_users'))
    
    # Store user details for the success message
    user_email = user.company_email
    user_name = user.name
    
    try:
        # Begin a transaction
        db.session.begin_nested()
        
        # Delete all related records in the correct order to avoid foreign key constraint violations
        
        # 1. First delete invoice addresses associated with the user's payments
        payment_ids = [p.iid for p in Payment.query.filter_by(user_id=user_id).all()]
        if payment_ids:
            InvoiceAddress.query.filter(InvoiceAddress.payment_id.in_(payment_ids)).delete(synchronize_session=False)
        
        # 2. Delete payments
        Payment.query.filter_by(user_id=user_id).delete(synchronize_session=False)
        
        # 3. Delete search history
        # SearchHistory.query.filter_by(u_id=user_id).delete(synchronize_session=False)
        
        # 4. Delete subscription history
        SubscriptionHistory.query.filter_by(U_ID=user_id).delete(synchronize_session=False)
        
        # 5. Delete subscribed users
        SubscribedUser.query.filter_by(U_ID=user_id).delete(synchronize_session=False)
        
        # 6. Finally, delete the user
        db.session.delete(user)
        
        # Commit the transaction
        db.session.commit()
        
        current_app.logger.info(f"User {user_id} ({user_email}) successfully deleted")
        flash(f'User {user_email} removed successfully.', 'success')
    except Exception as e:
        # Rollback in case of error
        db.session.rollback()
        current_app.logger.error(f"Error deleting user {user_id}: {str(e)}")
        flash(f'Error deleting user: {str(e)}', 'danger')
    
    return redirect(url_for('admin.admin_users'))

@admin_bp.route('/edit_user/<int:user_id>', methods=['POST'])
@admin_required
def admin_edit_user(user_id):
    user = User.query.get_or_404(user_id)
    
    name = request.form.get('name', '').strip()
    email = request.form.get('company_email', '').lower().strip()
    email_confirmed = 'email_confirmed' in request.form
    is_admin = 'is_admin' in request.form
    password = request.form.get('password', '').strip()
    
    # Validate input data
    errors = validate_user_data(name, email, password, user_id)
    
    # If there are validation errors, flash them and redirect
    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('admin.admin_users'))
    
    try:
        # Update user details
        user.name = name
        user.company_email = email
        user.email_confirmed = email_confirmed
        
        # Only update admin status if current user is not modifying themselves
        current_admin_id = session.get('admin_id')
        if user_id != current_admin_id:
            user.is_admin = is_admin
        else:
            if not is_admin:
                flash('You cannot remove your own admin privileges.', 'warning')
        
        # Update password if provided
        if password:
            user.set_password(password)
        
        db.session.commit()
        flash('User updated successfully!', 'success')
        current_app.logger.info(f"Admin updated user: {user.company_email}")
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating user {user_id}: {str(e)}")
        flash(f'Error updating user: {str(e)}', 'danger')
    
    return redirect(url_for('admin.admin_users'))
@admin_bp.route('/reset_user_password/<int:user_id>', methods=['POST'])
@admin_required
def admin_reset_user_password(user_id):
    user = User.query.get_or_404(user_id)
    
    # Generate a secure random password
    import secrets
    import string
    
    # Generate a 12-character password with mix of letters, numbers, and symbols
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    new_password = ''.join(secrets.choice(alphabet) for i in range(12))
    
    try:
        # Update the user's password
        user.set_password(new_password)
        db.session.commit()
        
        # In production, you should email this to the user instead of showing it
        flash(f'Password reset successfully! New password: {new_password}', 'success')
        current_app.logger.info(f"Admin reset password for user: {user.company_email}")
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error resetting password for user {user_id}: {str(e)}")
        flash(f'Error resetting password: {str(e)}', 'danger')
    
    return redirect(url_for('admin.admin_users'))
@admin_bp.route('/add_user', methods=['POST'])
@admin_required
def admin_add_user():
    if request.method == 'POST':
        name = request.form.get('name')
        company_email = request.form.get('company_email')
        password = request.form.get('password')
        email_confirmed = 'email_confirmed' in request.form
        is_admin = 'is_admin' in request.form
        # Validate input data
        errors = validate_user_data(name, company_email, password)
        if not password:
            errors.append("Password is required for new users.")
        
        # If there are validation errors, flash them and redirect
        if errors:
            for error in errors:
                flash(error, 'danger')
            return redirect(url_for('admin.admin_users'))
        try:
            # Create new user
            new_user = User(
                name=name,
                company_email=company_email,
                email_confirmed=email_confirmed,
                created_at=datetime.now(UTC)
            )
            new_user.set_password(password)
            
            db.session.add(new_user)
            db.session.commit()
            flash(f'User {name} ({company_email}) created successfully!', 'success')
            current_app.logger.info(f"Admin created new user: {company_email}")
        
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Database error creating user: {str(e)}")
            flash(f'Error creating user: {str(e)}', 'danger')
        return redirect(url_for('admin.admin_users'))



from datetime import datetime, timedelta, UTC
from sqlalchemy import func, or_, extract
from flask import request, render_template, session, flash, redirect, url_for, jsonify, send_file, current_app
import uuid
import calendar

@admin_bp.route('/payments')
@admin_required
def admin_payments():
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'payments'):
        flash("You don't have permission to access this page.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    # Get filter parameters
    status_filter = request.args.get('status', 'all')
    date_filter = request.args.get('date_range', 'all')  # Changed default from '30' to 'all'
    search_query = request.args.get('search', '')
    payment_type_filter = request.args.get('payment_type', 'all')
    
    # Base query with joins
    query = (
        db.session.query(
            Payment,
            User,
            Subscription,
            InvoiceAddress
        )
        .join(User, Payment.user_id == User.id)
        .join(Subscription, Payment.subscription_id == Subscription.S_ID)
        .outerjoin(InvoiceAddress, InvoiceAddress.payment_id == Payment.iid)
    )
    
    # Apply filters
    if status_filter != 'all':
        query = query.filter(Payment.status == status_filter)
    
    if payment_type_filter != 'all':
        query = query.filter(Payment.payment_type == payment_type_filter)
    
    # Date filter
    now = datetime.now(UTC)
    date_ranges = {
        '7': now - timedelta(days=7),
        '30': now - timedelta(days=30),
        '90': now - timedelta(days=90),
        '180': now - timedelta(days=180),
        '365': now - timedelta(days=365)
    }
    if date_filter in date_ranges:
        query = query.filter(Payment.created_at >= date_ranges[date_filter])
    
    # Search filter
    if search_query:
        search_filter = or_(
            User.name.ilike(f'%{search_query}%'),
            User.company_email.ilike(f'%{search_query}%'),
            Payment.invoice_number.ilike(f'%{search_query}%'),
            Payment.razorpay_order_id.ilike(f'%{search_query}%'),
            Payment.customer_number.ilike(f'%{search_query}%')
        )
        query = query.filter(search_filter)
    
    # Order and pagination
    payments = (
        query.order_by(Payment.created_at.desc())
        .paginate(page=request.args.get('page', 1, type=int), per_page=50, error_out=False)
    )
    
    # Get detailed payment status breakdown
    status_breakdown = get_payment_status_breakdown(status_filter, date_filter)
    # Get total count of all payments (unfiltered)
    total_payments_all = db.session.query(func.count(Payment.iid)).scalar() or 0

    # Get total revenue (only completed payments)
    total_revenue_completed = (
        db.session.query(func.sum(Payment.total_amount))
        .filter(Payment.status == 'completed')
        .scalar()
    ) or 0

    # Get count of completed payments
    completed_payments_count = (
        db.session.query(func.count(Payment.iid))
        .filter(Payment.status == 'completed')
        .scalar()
    ) or 0
    # UPDATED STATISTICS - ONLY COMPLETED PAYMENTS COUNT AS REVENUE
    stats = {
        'total_payments': total_payments_all,
        # ONLY completed payments count as revenue
        'total_revenue': db.session.query(func.sum(Payment.total_amount))
                            .filter(Payment.status == 'completed')
                            .scalar() or 0,
        'completed_payments': db.session.query(func.count(Payment.iid))
                                .filter(Payment.status == 'completed')
                                .scalar() or 0,
        # Payment type breakdown - ONLY completed payments
        'payment_type_breakdown': dict(
            db.session.query(Payment.payment_type, func.count(Payment.iid))
            .filter(Payment.status == 'completed')  # ONLY COMPLETED
            .group_by(Payment.payment_type)
            .all()
        ),
        # Payment status breakdown with current filters
        'payment_status_breakdown': status_breakdown
    }
    
    # Default revenue trend for current year monthly data (for initial chart load)
    revenue_trend = get_revenue_trend_data('month')
    
    return render_template('admin/payments.html',
                        payments=payments,
                        stats=stats,
                        revenue_trend=revenue_trend,
                        filters={
                            'status': status_filter,
                            'date_range': date_filter,
                            'search': search_query,
                            'payment_type': payment_type_filter
                        })

def get_payment_status_breakdown(status_filter='all', date_filter='all'):
    """
    Get payment status breakdown with filters applied
    Returns separate counts for completed, pending (created), and failed payments
    ALWAYS returns all 3 status types, even if count is 0
    """
    now = datetime.now(UTC)
    
    # Base query
    query = db.session.query(Payment.status, func.count(Payment.iid))
    
    # Apply date filter
    date_ranges = {
        '7': now - timedelta(days=7),
        '30': now - timedelta(days=30),
        '90': now - timedelta(days=90),
        '180': now - timedelta(days=180),
        '365': now - timedelta(days=365)
    }
    if date_filter in date_ranges:
        query = query.filter(Payment.created_at >= date_ranges[date_filter])
    
    # Apply status filter if not 'all'
    if status_filter != 'all':
        query = query.filter(Payment.status == status_filter)
    
    # Get the breakdown
    status_results = query.group_by(Payment.status).all()
    
    # ALWAYS initialize all status types with 0 - ensures consistent display
    breakdown = {
        'completed': 0,
        'pending': 0,    # This represents 'created' status
        'failed': 0,
        'cancelled': 0   # Include cancelled status as well
    }
    
    # Map database status to our breakdown
    for status, count in status_results:
        if status == 'completed':
            breakdown['completed'] = count
        elif status == 'created':
            breakdown['pending'] = count
        elif status == 'failed':
            breakdown['failed'] = count
        elif status == 'cancelled':
            breakdown['cancelled'] = count
        # You can add more status mappings as needed
    
    # For filtered views, show only the filtered status with its count, others as 0
    if status_filter == 'completed':
        breakdown = {'completed': breakdown['completed'], 'pending': 0, 'failed': 0}
    elif status_filter == 'created':  # pending
        breakdown = {'completed': 0, 'pending': breakdown['pending'], 'failed': 0}
    elif status_filter == 'failed':
        breakdown = {'completed': 0, 'pending': 0, 'failed': breakdown['failed']}
    else:
        # For 'all', include only the main 3 statuses
        breakdown = {
            'completed': breakdown['completed'], 
            'pending': breakdown['pending'], 
            'failed': breakdown['failed']
        }
    
    return breakdown

# NEW ENDPOINT FOR PAYMENT STATUS DATA
@admin_bp.route('/payments/status-breakdown')
@admin_required
def admin_payments_status_breakdown():
    """
    API endpoint to get payment status breakdown based on current filters
    """
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'payments'):
        return jsonify({'error': 'Permission denied'}), 403
    
    try:
        status_filter = request.args.get('status', 'all')
        date_filter = request.args.get('date_range', '30')
        
        breakdown = get_payment_status_breakdown(status_filter, date_filter)
        
        return jsonify({
            'labels': ['Completed', 'Pending', 'Failed'],
            'data': [breakdown['completed'], breakdown['pending'], breakdown['failed']],
            'status_filter': status_filter,
            'date_filter': date_filter
        })
    except Exception as e:
        current_app.logger.error(f"Payment status breakdown API error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# UPDATED ENDPOINT FOR YEARLY-BASED CHART DATA
@admin_bp.route('/payments/revenue-trend/<period>')
@admin_required
def admin_payments_revenue_trend(period):
    """
    API endpoint to get revenue trend data for different periods
    - week: 52 weeks of current year
    - month: 12 months of current year  
    - year: Last 5 years
    """
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'payments'):
        return jsonify({'error': 'Permission denied'}), 403
    
    try:
        revenue_data = get_revenue_trend_data(period)
        
        # Format data for JSON response
        formatted_data = {
            'labels': [item['label'] for item in revenue_data],
            'data': [float(item['total_revenue']) for item in revenue_data],
            'period': period
        }
        
        return jsonify(formatted_data)
    except Exception as e:
        current_app.logger.error(f"Revenue trend API error: {str(e)}")
        return jsonify({'error': str(e)}), 500

def get_revenue_trend_data(period):
    """
    Get revenue trend data based on the specified period
    
    Args:
        period (str): 'week', 'month', or 'year'
    
    Returns:
        list: Revenue trend data with labels and values
    """
    now = datetime.now(UTC)
    current_year = now.year
    
    if period == 'week':
        # Current year, grouped by week (52 weeks)
        return get_weekly_data_for_year(current_year)
        
    elif period == 'month':
        # Current year, grouped by month (12 months)
        return get_monthly_data_for_year(current_year)
        
    elif period == 'year':
        # Last 5 years, grouped by year
        return get_yearly_data(5)
        
    else:
        # Default to monthly
        return get_monthly_data_for_year(current_year)

def get_weekly_data_for_year(year):
    """
    Get weekly revenue data for a specific year (52 weeks)
    """
    # Start and end of the year
    year_start = datetime(year, 1, 1, tzinfo=UTC)
    year_end = datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC)
    
    # Query weekly data
    weekly_revenue = (
        db.session.query(
            func.date_trunc('week', Payment.created_at).label('week_start'),
            func.sum(Payment.total_amount).label('total_revenue')
        )
        .filter(Payment.status == 'completed')
        .filter(Payment.created_at >= year_start)
        .filter(Payment.created_at <= year_end)
        .group_by(func.date_trunc('week', Payment.created_at))
        .order_by(func.date_trunc('week', Payment.created_at))
        .all()
    )
    
    # Create a dictionary for quick lookup
    revenue_dict = {item.week_start.date(): float(item.total_revenue or 0) for item in weekly_revenue}
    
    # Generate all weeks of the year
    result = []
    current_date = year_start
    week_number = 1
    
    while current_date.year == year and week_number <= 52:
        # Get the start of the week (Monday)
        week_start = current_date - timedelta(days=current_date.weekday())
        week_start_date = week_start.date()
        
        revenue = revenue_dict.get(week_start_date, 0)
        
        # Format: "Week 1", "Week 2", etc.
        label = f"W{week_number}"
        
        result.append({
            'label': label,
            'total_revenue': revenue,
            'date': week_start
        })
        
        # Move to next week
        current_date += timedelta(weeks=1)
        week_number += 1
    
    return result

def get_monthly_data_for_year(year):
    """
    Get monthly revenue data for a specific year (12 months)
    """
    # Query monthly data
    monthly_revenue = (
        db.session.query(
            extract('month', Payment.created_at).label('month'),
            func.sum(Payment.total_amount).label('total_revenue')
        )
        .filter(Payment.status == 'completed')
        .filter(extract('year', Payment.created_at) == year)
        .group_by(extract('month', Payment.created_at))
        .order_by(extract('month', Payment.created_at))
        .all()
    )
    
    # Create a dictionary for quick lookup
    revenue_dict = {int(item.month): float(item.total_revenue or 0) for item in monthly_revenue}
    
    # Generate all 12 months
    result = []
    month_names = [
        'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
    ]
    
    for month in range(1, 13):
        revenue = revenue_dict.get(month, 0)
        
        result.append({
            'label': month_names[month - 1],
            'total_revenue': revenue,
            'month': month,
            'year': year
        })
    
    return result

def get_yearly_data(num_years):
    """
    Get yearly revenue data for the last N years
    """
    current_year = datetime.now(UTC).year
    start_year = current_year - num_years + 1
    
    # Query yearly data
    yearly_revenue = (
        db.session.query(
            extract('year', Payment.created_at).label('year'),
            func.sum(Payment.total_amount).label('total_revenue')
        )
        .filter(Payment.status == 'completed')
        .filter(extract('year', Payment.created_at) >= start_year)
        .filter(extract('year', Payment.created_at) <= current_year)
        .group_by(extract('year', Payment.created_at))
        .order_by(extract('year', Payment.created_at))
        .all()
    )
    
    # Create a dictionary for quick lookup
    revenue_dict = {int(item.year): float(item.total_revenue or 0) for item in yearly_revenue}
    
    # Generate all years in range
    result = []
    for year in range(start_year, current_year + 1):
        revenue = revenue_dict.get(year, 0)
        
        result.append({
            'label': str(year),
            'total_revenue': revenue,
            'year': year
        })
    
    return result

@admin_bp.route('/payments/<string:order_id>')
@admin_required
def admin_payment_details(order_id):
    email_id = session.get('email_id')
    
    # Permission check
    if not Admin.check_permission(email_id, 'payments'):
        flash("You don't have permission to access this page.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    # Comprehensive payment details query
    payment_details = (
        db.session.query(
            Payment,
            User,
            Subscription,
            InvoiceAddress
        )
        .join(User, Payment.user_id == User.id)
        .join(Subscription, Payment.subscription_id == Subscription.S_ID)
        .outerjoin(InvoiceAddress, InvoiceAddress.payment_id == Payment.iid)
        .filter(Payment.invoice_number == order_id)
        .first_or_404()
    )
    
    if not payment_details:
        flash(f"No payment found for Order ID: {order_id}", "danger")
        return redirect(url_for('admin.admin_payments'))

    # Unpack query results
    payment, user, subscription, invoice_address = payment_details
    
    # Fetch Razorpay details if applicable
    razorpay_details = None

    # Initialize Razorpay client here with your API credentials
    from razorpay import Client as RazorpayClient
    razorpay_api_key = current_app.config.get('RAZORPAY_API_KEY', '')
    razorpay_api_secret = current_app.config.get('RAZORPAY_API_SECRET', '')
    razorpay_client = RazorpayClient(auth=(razorpay_api_key, razorpay_api_secret))
    
    if payment.razorpay_payment_id and not payment.razorpay_payment_id.startswith('manual_'):
        try:
            razorpay_details = razorpay_client.payment.fetch(payment.razorpay_payment_id)
        except Exception as e:
            current_app.logger.warning(f"Razorpay fetch error: {str(e)}")
    
    # Related payments history
    related_payments = (
        Payment.query
        .filter(Payment.user_id == user.id)
        .order_by(Payment.created_at.desc())
        .limit(5)
        .all()
    )
    
    return render_template('admin/payment_details.html', 
                           payment=payment, 
                           user=user, 
                           subscription=subscription,
                           invoice_address=invoice_address,
                           razorpay_details=razorpay_details,
                           related_payments=related_payments)

@admin_bp.route('/payments/update/<string:order_id>', methods=['POST'])
@admin_required
def admin_update_payment(order_id):
    payment = Payment.query.filter_by(invoice_number=order_id).first_or_404()
    
    # Validate and update payment status
    new_status = request.form.get('status')
    valid_statuses = ['created', 'completed', 'failed', 'cancelled']
    
    if new_status in valid_statuses:
        old_status = payment.status
        payment.status = new_status
        
        # Additional status change logic
        try:
            if new_status == 'completed' and old_status != 'completed':
                # Ensure invoice is generated
                if not payment.invoice_number:
                    payment.invoice_number = generate_unique_invoice_number()
                
                # Create or update subscription
                create_or_update_subscription(payment)
                
                # Generate invoice address if not exists
                create_invoice_address_for_payment(payment)
            
            db.session.commit()
            flash('Payment status updated successfully', 'success')
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Payment update error: {str(e)}")
            flash(f'Error updating payment: {str(e)}', 'danger')
    else:
        flash('Invalid status', 'danger')
    
    return redirect(url_for('admin.admin_payment_details', order_id=order_id))

@admin_bp.route('/payment/<order_id>/invoice')
@admin_required  
def admin_payment_invoice(order_id):
    """
    Generate and serve a PDF invoice for a specific payment order
    
    :param order_id: Razorpay order ID
    :return: PDF file response
    """
    # Find the payment by order_id
    payment = Payment.query.filter_by(razorpay_order_id=order_id).first_or_404()
    
    # Generate PDF invoice
    pdf_buffer = generate_invoice_pdf(payment)
    
    # Send the PDF as a download
    return send_file(
        pdf_buffer,
        download_name=f"invoice_{payment.invoice_number}.pdf",
        as_attachment=True,
        mimetype='application/pdf'
    )

def generate_unique_invoice_number():
    """
    Generate a unique invoice number
    """
    timestamp = datetime.now(UTC).strftime("%y%m%d")
    unique_id = str(uuid.uuid4().hex)[:8]
    return f"INV-{timestamp}-{unique_id}"

def create_or_update_subscription(payment):
    """
    Create or update subscription based on payment
    """
    # Check if subscription already exists
    existing_sub = SubscribedUser.query.filter_by(
        U_ID=payment.user_id,
        S_ID=payment.subscription_id
    ).first()
    
    if not existing_sub:
        subscription = Subscription.query.get(payment.subscription_id)
        start_date = datetime.now(UTC)
        end_date = start_date + timedelta(days=subscription.days)
        
        new_subscription = SubscribedUser(
            U_ID=payment.user_id,
            S_ID=payment.subscription_id,
            start_date=start_date,
            end_date=end_date,
            current_usage=0,
            is_auto_renew=True
        )
        
        # Record subscription history
        history_entry = SubscriptionHistory(
            U_ID=payment.user_id,
            S_ID=payment.subscription_id,
            action=payment.payment_type,
            previous_S_ID=payment.previous_subscription_id
        )
        
        db.session.add(new_subscription)
        db.session.add(history_entry)

def create_invoice_address_for_payment(payment):
    """
    Create invoice address for payment if not exists
    """
    existing_address = InvoiceAddress.query.filter_by(payment_id=payment.iid).first()
    
    if not existing_address:
        # Try to get user details
        user = User.query.get(payment.user_id)
        
        new_address = InvoiceAddress(
            payment_id=payment.iid,
            full_name=user.name,
            email=user.company_email,
            company_name=user.company_name if hasattr(user, 'company_name') else None,
            street_address=user.address if hasattr(user, 'address') else 'N/A',
            city=user.city if hasattr(user, 'city') else 'N/A',
            state=user.state if hasattr(user, 'state') else 'N/A',
            postal_code=user.postal_code if hasattr(user, 'postal_code') else 'N/A',
            gst_number=user.gst_number if hasattr(user, 'gst_number') else None
        )
        
        db.session.add(new_address)
# Admin routes for contact submissions
@admin_bp.route('/admin/contact_submissions')
@admin_required
def admin_contact_submissions():
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'contact_submissions'):
        flash("You don't have permission to access this page.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', 'all')
    
    query = ContactSubmission.query
    
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    
    submissions = query.order_by(ContactSubmission.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    
    # Calculate stats
    total_count = ContactSubmission.query.count()
    new_count = ContactSubmission.query.filter_by(status='new').count()
    responded_count = ContactSubmission.query.filter_by(status='responded').count()
    
    # Today's submissions
    today = datetime.now(UTC).date()
    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(today, datetime.max.time())
    today_count = ContactSubmission.query.filter(
        ContactSubmission.created_at.between(today_start, today_end)
    ).count()
    
    return render_template('admin/contact_submissions.html',
                          submissions=submissions,
                          status_filter=status_filter,
                          total_count=total_count,
                          new_count=new_count,
                          responded_count=responded_count,
                          today_count=today_count)
@admin_bp.route('/admin/contact_submissions/<int:submission_id>')
@admin_required
def admin_contact_submission_detail(submission_id):
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'contact_submissions'):
        flash("You don't have permission to access this page.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    submission = ContactSubmission.query.get_or_404(submission_id)
    
    # Mark as read if it was new
    if submission.status == 'new':
        submission.status = 'read'
        db.session.commit()
    
    return render_template('admin/contact_submission_detail.html', 
                          submission=submission)

@admin_bp.route('/admin/contact_submissions/<int:submission_id>/update', methods=['POST'])
@admin_required
def update_contact_submission(submission_id):
    submission = ContactSubmission.query.get_or_404(submission_id)
    
    new_status = request.form.get('status')
    admin_notes = request.form.get('admin_notes')
    
    if new_status in ['new', 'read', 'responded', 'spam']:
        submission.status = new_status
        if new_status == 'responded' and not submission.responded_at:
            submission.responded_at = datetime.now(UTC)
    
    if admin_notes is not None:  # Allow empty string
        submission.admin_notes = admin_notes
    
    db.session.commit()
    
    # Check if it's an AJAX request
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'message': 'Submission updated successfully'})
    
    flash('Submission updated successfully!', 'success')
    return redirect(url_for('admin.admin_contact_submission_detail', submission_id=submission_id))

@admin_bp.route('/admin/contact_submissions/<int:submission_id>/spam', methods=['POST'])
@admin_required
def mark_submission_as_spam(submission_id):
    email_id = session.get('email_id')

    if not Admin.check_permission(email_id, 'contact_submissions'):
        return jsonify({'success': False, 'message': "You don't have permission to update submissions."}), 403

    try:
        submission = ContactSubmission.query.get_or_404(submission_id)
        submission.status = 'spam'
        db.session.commit()

        return jsonify({'success': True, 'message': 'Submission marked as spam'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Error marking as spam: {str(e)}'}), 500

@admin_bp.route('/admin/contact_submissions/<int:submission_id>/delete', methods=['POST'])
@admin_required
def delete_contact_submission(submission_id):
    email_id = session.get('email_id')

    if not Admin.check_permission(email_id, 'contact_submissions'):
        return jsonify({'success': False, 'message': "You don't have permission to delete submissions."}), 403

    try:
        submission = ContactSubmission.query.get_or_404(submission_id)
        db.session.delete(submission)
        db.session.commit()

        return jsonify({'success': True, 'message': 'Submission deleted successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Error deleting submission: {str(e)}'}), 500

@admin_bp.route('/admin/contact_submissions/<int:submission_id>/send_reply', methods=['POST'])
@admin_required
def send_reply_to_submission(submission_id):
    """Send reply email to contact submission"""
    try:
        submission = ContactSubmission.query.get_or_404(submission_id)

        # Get form data
        data = request.get_json() if request.is_json else request.form
        subject = data.get('subject')
        message = data.get('message')

        if not subject or not message:
            return jsonify({'success': False, 'message': 'Subject and message are required'}), 400

        # Send reply email using SMTP (support account)
        result = email_service.send_support_email(
            to=submission.email,
            subject=subject,
            body=message
        )

        if not result:
            return jsonify({'success': False, 'message': 'Failed to send email. Please try again.'}), 500

        # Update submission status to 'responded'
        if submission.status != 'responded':
            submission.status = 'responded'
            submission.responded_at = datetime.now(UTC)
            db.session.commit()

        return jsonify({'success': True, 'message': 'Reply sent successfully'})

    except Exception as e:
        return jsonify({'success': False, 'message': f'Error sending reply: {str(e)}'}), 500

@admin_bp.route('/admin/export_contact_submissions')
@admin_required
def admin_export_contact_submissions():
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'contact_submissions'):
        flash("You don't have permission to access this feature.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    status_filter = request.args.get('status', 'all')
    
    query = ContactSubmission.query
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    
    submissions = query.order_by(ContactSubmission.created_at.desc()).all()
    
    # Create CSV in memory
    output = StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow(['ID', 'Name', 'Email', 'Message', 'Status', 'IP Address', 'Submitted Date', 'Admin Notes'])
    
    # Write data rows
    for submission in submissions:
        writer.writerow([
            submission.id,
            submission.name,
            submission.email,
            submission.message,
            submission.status,
            submission.ip_address or '',
            submission.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            submission.admin_notes or ''
        ])
    
    # Prepare response
    output.seek(0)
    return send_file(
        BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'contact_submissions_{datetime.now(UTC).strftime("%Y%m%d_%H%M%S")}.csv'
    )

# Add these routes to your app.py file (around line 1800, with other admin routes)

@admin_bp.route('/admin/email_logs')
@admin_required
def admin_email_logs():
    """Admin page to view all email logs with filtering and search"""
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'email_logs'):
        flash("You don't have permission to access email logs.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    # Get filter parameters
    email_type_filter = request.args.get('email_type', 'all')
    status_filter = request.args.get('status', 'all')
    date_range_filter = request.args.get('date_range', '30')
    search_query = request.args.get('search', '')
    page = request.args.get('page', 1, type=int)
    per_page = 50
    
    # Build base query
    query = EmailLog.query
    
    # Apply email type filter
    if email_type_filter != 'all':
        query = query.filter(EmailLog.email_type == email_type_filter)
    
    # Apply status filter
    if status_filter != 'all':
        query = query.filter(EmailLog.status == status_filter)
    
    # Apply date range filter
    now = datetime.now(UTC)
    if date_range_filter == '7':
        start_date = now - timedelta(days=7)
        query = query.filter(EmailLog.sent_at >= start_date)
    elif date_range_filter == '30':
        start_date = now - timedelta(days=30)
        query = query.filter(EmailLog.sent_at >= start_date)
    elif date_range_filter == '90':
        start_date = now - timedelta(days=90)
        query = query.filter(EmailLog.sent_at >= start_date)
    elif date_range_filter == '365':
        start_date = now - timedelta(days=365)
        query = query.filter(EmailLog.sent_at >= start_date)
    
    # Apply search filter
    if search_query:
        search_filter = or_(
            EmailLog.recipient_email.ilike(f'%{search_query}%'),
            EmailLog.recipient_name.ilike(f'%{search_query}%'),
            EmailLog.subject.ilike(f'%{search_query}%')
        )
        query = query.filter(search_filter)
    
    # Execute query with pagination
    email_logs = query.order_by(EmailLog.sent_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    # Calculate statistics
    total_emails = EmailLog.query.count()
    sent_emails = EmailLog.query.filter_by(status='sent').count()
    failed_emails = EmailLog.query.filter_by(status='failed').count()
    
    # Today's emails
    today_start = datetime.combine(now.date(), datetime.min.time())
    today_emails = EmailLog.query.filter(EmailLog.sent_at >= today_start).count()
    
    # Email type statistics
    email_type_stats = (
        db.session.query(
            EmailLog.email_type,
            func.count(EmailLog.id).label('count'),
            func.sum(case((EmailLog.status == 'sent', 1), else_=0)).label('sent_count'),
            func.sum(case((EmailLog.status == 'failed', 1), else_=0)).label('failed_count')
        )
        .group_by(EmailLog.email_type)
        .order_by(func.count(EmailLog.id).desc())
        .all()
    )
    
    # Get available email types for filter dropdown
    available_email_types = (
        db.session.query(EmailLog.email_type.distinct().label('email_type'))
        .order_by(EmailLog.email_type)
        .all()
    )
    
    return render_template('admin/email_logs.html',
                          email_logs=email_logs,
                          email_type_filter=email_type_filter,
                          status_filter=status_filter,
                          date_range_filter=date_range_filter,
                          search_query=search_query,
                          available_email_types=available_email_types,
                          total_emails=total_emails,
                          sent_emails=sent_emails,
                          failed_emails=failed_emails,
                          today_emails=today_emails,
                          email_type_stats=email_type_stats)


@admin_bp.route('/admin/email_logs/<int:log_id>')
@admin_required
def admin_email_log_detail(log_id):
    """View detailed information about a specific email log"""
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'email_logs'):
        flash("You don't have permission to access email logs.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    email_log = EmailLog.query.get_or_404(log_id)
    
    # Parse metadata if available
    metadata = None
    if email_log.metadata:
        try:
            import json
            metadata = json.loads(email_log.metadata)
        except:
            metadata = None
    
    return render_template('admin/email_log_detail.html',
                          email_log=email_log,
                          metadata=metadata)


@admin_bp.route('/admin/email_logs/export')
@admin_required
def admin_export_email_logs():
    """Export email logs to CSV"""
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'email_logs'):
        flash("You don't have permission to export email logs.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    # Get same filters as main page
    email_type_filter = request.args.get('email_type', 'all')
    status_filter = request.args.get('status', 'all')
    date_range_filter = request.args.get('date_range', '30')
    search_query = request.args.get('search', '')
    
    # Build query with same filters
    query = EmailLog.query
    
    if email_type_filter != 'all':
        query = query.filter(EmailLog.email_type == email_type_filter)
    
    if status_filter != 'all':
        query = query.filter(EmailLog.status == status_filter)
    
    # Apply date range filter
    now = datetime.now(UTC)
    if date_range_filter == '7':
        start_date = now - timedelta(days=7)
        query = query.filter(EmailLog.sent_at >= start_date)
    elif date_range_filter == '30':
        start_date = now - timedelta(days=30)
        query = query.filter(EmailLog.sent_at >= start_date)
    elif date_range_filter == '90':
        start_date = now - timedelta(days=90)
        query = query.filter(EmailLog.sent_at >= start_date)
    elif date_range_filter == '365':
        start_date = now - timedelta(days=365)
        query = query.filter(EmailLog.sent_at >= start_date)
    
    if search_query:
        search_filter = or_(
            EmailLog.recipient_email.ilike(f'%{search_query}%'),
            EmailLog.recipient_name.ilike(f'%{search_query}%'),
            EmailLog.subject.ilike(f'%{search_query}%')
        )
        query = query.filter(search_filter)
    
    # Get all matching records
    email_logs = query.order_by(EmailLog.sent_at.desc()).all()
    
    # Create CSV in memory
    output = StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow([
        'ID', 'Recipient Email', 'Recipient Name', 'Email Type', 
        'Subject', 'Status', 'Sent Date', 'Error Message', 'User ID'
    ])
    
    # Write data rows
    for log in email_logs:
        writer.writerow([
            log.id,
            log.recipient_email,
            log.recipient_name or '',
            log.email_type,
            log.subject,
            log.status,
            log.sent_at.strftime('%Y-%m-%d %H:%M:%S UTC'),
            log.error_message or '',
            log.user_id or ''
        ])
    
    # Prepare response
    output.seek(0)
    return send_file(
        BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'email_logs_{datetime.now(UTC).strftime("%Y%m%d_%H%M%S")}.csv'
    )


@admin_bp.route('/admin/email_logs/retry/<int:log_id>', methods=['POST'])
@admin_required
def admin_retry_email(log_id):
    """Retry sending a failed email with logging"""
    email_id = session.get('email_id')
    
    if not Admin.check_permission(email_id, 'email_logs'):
        flash("You don't have permission to retry emails.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    email_log = EmailLog.query.get_or_404(log_id)
    
    if email_log.status != 'failed':
        flash("Can only retry failed emails.", "warning")
        return redirect(url_for('admin.admin_email_log_detail', log_id=log_id))
    
    try:
        # Create a simple retry email
        subject = f"[RETRY] {email_log.subject}"
        logo_url = "https://qrdada.com/static/images/qr.png"

        # Plain text version
        body = f"""Dear {email_log.recipient_name or 'User'},

This is a retry of a previously failed email from QR Dada.

Original Subject: {email_log.subject}
Original Send Date: {email_log.sent_at.strftime('%B %d, %Y at %I:%M %p UTC')}

If you need assistance, please contact our support team.

Best regards,
The QR Dada Support Team
"""

        # HTML version
        html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Email Retry - QR Dada</title>
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
                                Email Retry
                            </h1>

                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                Dear <strong>{email_log.recipient_name or 'User'}</strong>,
                            </p>

                            <p style="margin: 0 0 20px 0; font-size: 16px; line-height: 1.6; color: #475569;">
                                This is a retry of a previously failed email from QR Dada.
                            </p>

                            <!-- Email Details -->
                            <div style="margin: 30px 0; padding: 20px; background-color: #f8fafc; border-radius: 8px; border-left: 4px solid #8b5cf6;">
                                <p style="margin: 0 0 10px 0; font-size: 14px; line-height: 1.6; color: #64748b;">
                                    <strong style="color: #1e293b;">Original Subject:</strong><br>
                                    {email_log.subject}
                                </p>
                                <p style="margin: 10px 0 0 0; font-size: 14px; line-height: 1.6; color: #64748b;">
                                    <strong style="color: #1e293b;">Original Send Date:</strong><br>
                                    {email_log.sent_at.strftime('%B %d, %Y at %I:%M %p UTC')}
                                </p>
                            </div>

                            <p style="margin: 20px 0 0 0; font-size: 14px; line-height: 1.6; color: #64748b;">
                                If you need assistance or have any questions, please contact our support team.
                            </p>
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="padding: 30px 40px; background-color: #f8fafc; border-radius: 0 0 12px 12px; border-top: 1px solid #e2e8f0;">
                            <p style="margin: 0 0 10px 0; font-size: 14px; color: #64748b; text-align: center;">
                                Best regards,<br>
                                <strong style="color: #1e293b;">The QR Dada Support Team</strong>
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
"""

        # Send retry email using SMTP (support account)
        result = email_service.send_support_email(
            to=email_log.recipient_email,
            subject=subject,
            body=body,
            html=html
        )

        if not result:
            flash("Failed to send retry email. Please try again.", "danger")
            return redirect(url_for('admin.admin_email_log_detail', log_id=log_id))
        
        # Log the successful retry attempt
        EmailLog.log_email(
            recipient_email=email_log.recipient_email,
            recipient_name=email_log.recipient_name,
            email_type=f"{email_log.email_type}_retry",
            subject=subject,
            user_id=email_log.user_id,
            status='sent',
            metadata={'original_log_id': log_id, 'retry': True, 'retried_by_admin': True}
        )
        
        flash("Email retry sent successfully.", "success")
        
    except Exception as e:
        # Log the failed retry
        EmailLog.log_email(
            recipient_email=email_log.recipient_email,
            recipient_name=email_log.recipient_name,
            email_type=f"{email_log.email_type}_retry",
            subject=subject,
            user_id=email_log.user_id,
            status='failed',
            error_message=str(e),
            metadata={'original_log_id': log_id, 'retry': True, 'retried_by_admin': True}
        )
        
        flash(f"Failed to retry email: {str(e)}", "danger")

    return redirect(url_for('admin.admin_email_log_detail', log_id=log_id))

# New routes for increment/decrement functionality - UPDATED to modify limits
@admin_bp.route('/subscribed-users/increment-analytics/<int:id>', methods=['POST'])
@admin_required
def admin_increment_analytics(id):
    """Increment analytics limit for a subscribed user"""
    subscribed_user = SubscribedUser.query.get_or_404(id)

    try:
        count = request.json.get('count', 1)
        if count <= 0:
            return jsonify({'success': False, 'error': 'Count must be positive'}), 400

        new_limit = subscribed_user.add_analytics_limit(count)
        used = subscribed_user.analytics_used

        return jsonify({
            'success': True,
            'used': used,
            'limit': new_limit,
            'percentage': min(100, (used / new_limit * 100)) if new_limit > 0 else 0
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@admin_bp.route('/subscribed-users/decrement-analytics/<int:id>', methods=['POST'])
@admin_required
def admin_decrement_analytics(id):
    """Decrement analytics limit for a subscribed user"""
    subscribed_user = SubscribedUser.query.get_or_404(id)

    try:
        count = request.json.get('count', 1)
        if count <= 0:
            return jsonify({'success': False, 'error': 'Count must be positive'}), 400

        new_limit = subscribed_user.subtract_analytics_limit(count)
        used = subscribed_user.analytics_used

        return jsonify({
            'success': True,
            'used': used,
            'limit': new_limit,
            'percentage': min(100, (used / new_limit * 100)) if new_limit > 0 else 0
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@admin_bp.route('/subscribed-users/increment-qr/<int:id>', methods=['POST'])
@admin_required
def admin_increment_qr(id):
    """Increment QR code limit for a subscribed user"""
    subscribed_user = SubscribedUser.query.get_or_404(id)

    try:
        count = request.json.get('count', 1)
        if count <= 0:
            return jsonify({'success': False, 'error': 'Count must be positive'}), 400

        new_limit = subscribed_user.add_qr_limit(count)
        used = subscribed_user.qr_generated

        return jsonify({
            'success': True,
            'used': used,
            'limit': new_limit,
            'percentage': min(100, (used / new_limit * 100)) if new_limit > 0 else 0
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@admin_bp.route('/subscribed-users/decrement-qr/<int:id>', methods=['POST'])
@admin_required
def admin_decrement_qr(id):
    """Decrement QR code limit for a subscribed user"""
    subscribed_user = SubscribedUser.query.get_or_404(id)

    try:
        count = request.json.get('count', 1)
        if count <= 0:
            return jsonify({'success': False, 'error': 'Count must be positive'}), 400

        new_limit = subscribed_user.subtract_qr_limit(count)
        used = subscribed_user.qr_generated

        return jsonify({
            'success': True,
            'used': used,
            'limit': new_limit,
            'percentage': min(100, (used / new_limit * 100)) if new_limit > 0 else 0
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@admin_bp.route('/subscribed-users/increment-scans/<int:id>', methods=['POST'])
@admin_required
def admin_increment_scans(id):
    """Increment scan limit for a subscribed user"""
    subscribed_user = SubscribedUser.query.get_or_404(id)

    try:
        count = request.json.get('count', 1)
        if count <= 0:
            return jsonify({'success': False, 'error': 'Count must be positive'}), 400

        new_limit = subscribed_user.add_scan_limit(count)
        used = subscribed_user.scans_used or 0

        return jsonify({
            'success': True,
            'used': used,
            'limit': new_limit,
            'percentage': min(100, (used / new_limit * 100)) if new_limit > 0 else 0
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@admin_bp.route('/subscribed-users/decrement-scans/<int:id>', methods=['POST'])
@admin_required
def admin_decrement_scans(id):
    """Decrement scan limit for a subscribed user"""
    subscribed_user = SubscribedUser.query.get_or_404(id)

    try:
        count = request.json.get('count', 1)
        if count <= 0:
            return jsonify({'success': False, 'error': 'Count must be positive'}), 400

        new_limit = subscribed_user.subtract_scan_limit(count)
        used = subscribed_user.scans_used or 0

        return jsonify({
            'success': True,
            'used': used,
            'limit': new_limit,
            'percentage': min(100, (used / new_limit * 100)) if new_limit > 0 else 0
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

class WebsiteSettings(db.Model):
    __tablename__ = 'website_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    setting_key = db.Column(db.String(100), unique=True, nullable=False)
    setting_value = db.Column(db.Text, nullable=True)
    setting_type = db.Column(db.String(50), default='text')  # text, file, json, etc.
    description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now(UTC))
    updated_at = db.Column(db.DateTime, default=datetime.now(UTC), onupdate=datetime.now(UTC))
    updated_by = db.Column(db.Integer, db.ForeignKey('admin.id'), nullable=True)
    
    # Relationship to admin who made the change
    updated_by_admin = db.relationship('Admin', backref='settings_updates')
    
    def __repr__(self):
        return f"<WebsiteSettings {self.setting_key}={self.setting_value}>"
    
    @staticmethod
    def get_setting(key, default=None):
        """Get a setting value by key"""
        try:
            setting = WebsiteSettings.query.filter_by(setting_key=key).first()
            # Check if setting exists AND has a non-None, non-empty value
            if setting and setting.setting_value is not None and setting.setting_value.strip():
                return setting.setting_value
            else:
                return default
        except Exception as e:
            current_app.logger.error(f"Error getting setting {key}: {str(e)}")
            return default
    @staticmethod
    def set_setting(key, value, admin_id=None, description=None, setting_type='text'):
        """Set or update a setting"""
        setting = WebsiteSettings.query.filter_by(setting_key=key).first()
        
        if setting:
            setting.setting_value = value
            setting.updated_at = datetime.now(UTC)
            setting.updated_by = admin_id
            if description:
                setting.description = description
        else:
            setting = WebsiteSettings(
                setting_key=key,
                setting_value=value,
                setting_type=setting_type,
                description=description,
                updated_by=admin_id
            )
            db.session.add(setting)
        
        db.session.commit()
        return setting
def ensure_default_website_settings():
    """Ensure default website settings exist in database"""
    try:
        defaults = [
            ('website_name', 'QR Dada'),
            ('website_icon', 'fas fa-qrcode'), 
            ('website_tagline', 'Professional QR Code and Analytics Platform'),
            ('website_logo_file', None)
        ]
        
        for key, default_value in defaults:
            existing = WebsiteSettings.query.filter_by(setting_key=key).first()
            if not existing:
                setting = WebsiteSettings(
                    setting_key=key,
                    setting_value=default_value,
                    description=f'Default {key.replace("_", " ").title()}'
                )
                db.session.add(setting)
        
        db.session.commit()
        current_app.logger.info("Default website settings ensured")
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error ensuring default website settings: {str(e)}")
def initialize_website_settings():
    """Initialize default website settings if they don't exist"""
    try:
        # Use the new ensure function
        ensure_default_website_settings()
        current_app.logger.info("Website settings initialized")
    except Exception as e:
        current_app.logger.error(f"Error initializing website settings: {str(e)}")    
import os
from werkzeug.utils import secure_filename

# Configure upload settings
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico'}

# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS        
@admin_bp.route('/website-settings')
@admin_required
def admin_website_settings():
    """Admin page to manage website settings"""
    email_id = session.get('email_id')
    
    # Check permission - you can add this to your permissions list
    if not Admin.check_permission(email_id, 'website_settings'):
        flash("You don't have permission to access website settings.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    # Get all website settings
    settings = WebsiteSettings.query.all()
    settings_dict = {setting.setting_key: setting for setting in settings}
    
    # Get current values - return exactly what's stored, no defaults
    current_settings = {
        'website_icon': WebsiteSettings.get_setting('website_icon'),
        'website_logo_file': WebsiteSettings.get_setting('website_logo_file')
    }
    
    # Get list of FontAwesome icons for the dropdown
    fontawesome_icons = [
        {'class': 'fas fa-qrcode', 'name': 'QR Code'},
        {'class': 'fas fa-analytics', 'name': 'Analytics'},
        {'class': 'fas fa-search', 'name': 'Search'},
        {'class': 'fas fa-globe', 'name': 'Globe'},
        {'class': 'fas fa-chart-bar', 'name': 'Chart Bar'},
        {'class': 'fas fa-chart-pie', 'name': 'Chart Pie'},
        {'class': 'fas fa-chart-area', 'name': 'Chart Area'},
        {'class': 'fas fa-sitemap', 'name': 'Sitemap'},
        {'class': 'fas fa-code', 'name': 'Code'},
        {'class': 'fas fa-desktop', 'name': 'Desktop'},
        {'class': 'fas fa-mobile-alt', 'name': 'Mobile'},
        {'class': 'fas fa-laptop', 'name': 'Laptop'},
        {'class': 'fas fa-cog', 'name': 'Settings'},
        {'class': 'fas fa-tools', 'name': 'Tools'},
        {'class': 'fas fa-wrench', 'name': 'Wrench'},
        {'class': 'fas fa-rocket', 'name': 'Rocket'},
        {'class': 'fas fa-star', 'name': 'Star'},
        {'class': 'fas fa-bolt', 'name': 'Bolt'},
        {'class': 'fas fa-fire', 'name': 'Fire'},
        {'class': 'fas fa-gem', 'name': 'Gem'}
    ]
    
    return render_template('admin/website_settings.html',
                          current_settings=current_settings,
                          settings_dict=settings_dict,
                          fontawesome_icons=fontawesome_icons)

@admin_bp.route('/website-settings/update', methods=['POST'])
@admin_required
def admin_update_website_settings():
    """Update website settings"""
    email_id = session.get('email_id')
    admin_id = session.get('admin_id')
    
    # Check permission
    if not Admin.check_permission(email_id, 'website_settings'):
        flash("You don't have permission to update website settings.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    try:
        # Get form data - store exactly what user submits, None if empty
        website_icon = request.form.get('website_icon', '').strip() or None
        use_custom_logo = request.form.get('use_custom_logo') == 'on'
        
        # Handle logo file upload
        logo_filename = None
        if use_custom_logo and 'logo_file' in request.files:
            file = request.files['logo_file']
            if file and file.filename != '' and allowed_file(file.filename):
                # Secure the filename
                filename = secure_filename(file.filename)
                # Add timestamp to avoid conflicts
                timestamp = str(int(time.time()))
                name, ext = os.path.splitext(filename)
                logo_filename = f"logo_{timestamp}{ext}"
                
                # Save the file
                file_path = os.path.join(UPLOAD_FOLDER, logo_filename)
                file.save(file_path)
                
                # Delete old logo file if exists
                old_logo = WebsiteSettings.get_setting('website_logo_file')
                if old_logo:
                    old_file_path = os.path.join(UPLOAD_FOLDER, old_logo)
                    if os.path.exists(old_file_path):
                        try:
                            os.remove(old_file_path)
                        except:
                            pass  # Ignore if can't delete old file
        
        # Update settings in database - store exactly what user provided (None if empty)
        WebsiteSettings.set_setting('website_icon', website_icon, admin_id, 'FontAwesome icon updated' if website_icon else 'Website icon cleared')
        
        # Update logo file setting
        if use_custom_logo and logo_filename:
            WebsiteSettings.set_setting('website_logo_file', logo_filename, admin_id, 'Custom logo file uploaded', 'file')
        elif not use_custom_logo:
            # Clear custom logo if not using it
            old_logo = WebsiteSettings.get_setting('website_logo_file')
            if old_logo:
                # Delete the file
                old_file_path = os.path.join(UPLOAD_FOLDER, old_logo)
                if os.path.exists(old_file_path):
                    try:
                        os.remove(old_file_path)
                    except:
                        pass
            WebsiteSettings.set_setting('website_logo_file', None, admin_id, 'Custom logo file cleared', 'file')
        
        flash('Website settings updated successfully!', 'success')
        current_app.logger.info(f"Website settings updated by admin {email_id}")
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating website settings: {str(e)}")
        flash(f'Error updating settings: {str(e)}', 'danger')
    
    return redirect(url_for('admin.admin_website_settings'))

@admin_bp.route('/website-settings/reset', methods=['POST'])
@admin_required
def admin_reset_website_settings():
    """Reset website settings to empty values"""
    email_id = session.get('email_id')
    admin_id = session.get('admin_id')
    
    # Check permission
    if not Admin.check_permission(email_id, 'website_settings'):
        flash("You don't have permission to reset website settings.", "danger")
        return redirect(url_for('admin.admin_dashboard'))
    
    try:
        # Delete custom logo file if exists
        old_logo = WebsiteSettings.get_setting('website_logo_file')
        if old_logo:
            old_file_path = os.path.join(UPLOAD_FOLDER, old_logo)
            if os.path.exists(old_file_path):
                try:
                    os.remove(old_file_path)
                except:
                    pass
        
        # Clear all settings (set to None/empty)
        WebsiteSettings.set_setting('website_icon', None, admin_id, 'Website icon cleared')
        WebsiteSettings.set_setting('website_logo_file', None, admin_id, 'Custom logo file cleared', 'file')
        
        flash('All website settings have been cleared successfully!', 'success')
        current_app.logger.info(f"Website settings cleared by admin {email_id}")
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error clearing website settings: {str(e)}")
        flash(f'Error clearing settings: {str(e)}', 'danger')
    
    return redirect(url_for('admin.admin_website_settings'))    
# ========================
# BLOG MODELS
# ========================

class BlogCategory(db.Model):
    __tablename__ = 'blog_categories'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    status = db.Column(db.Boolean, default=True)  # Active/Inactive
    created_at = db.Column(db.DateTime, default=datetime.now(UTC))
    updated_at = db.Column(db.DateTime, default=datetime.now(UTC), onupdate=datetime.now(UTC))

    # Relationship
    blogs = db.relationship('Blog', backref='category', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f"<BlogCategory {self.name}>"

class Blog(db.Model):
    __tablename__ = 'blogs'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False)
    description = db.Column(db.Text, nullable=True)
    author_name = db.Column(db.String(255), nullable=True)
    meta_title = db.Column(db.String(255), nullable=True)
    meta_keyword = db.Column(db.Text, nullable=True)
    meta_description = db.Column(db.Text, nullable=True)
    image = db.Column(db.String(255), nullable=True)  # Image filename
    category_id = db.Column(db.Integer, db.ForeignKey('blog_categories.id'), nullable=True)
    status = db.Column(db.Boolean, default=True)  # Active/Inactive
    schema_data = db.Column(db.Text, nullable=True)  # JSON for FAQs and other schema data
    publish_date = db.Column(db.Date, nullable=True)  # Blog publish date
    created_by = db.Column(db.String(100), nullable=True)  # Admin email or name
    created_at = db.Column(db.DateTime, default=datetime.now(UTC))
    updated_at = db.Column(db.DateTime, default=datetime.now(UTC), onupdate=datetime.now(UTC))

    def __repr__(self):
        return f"<Blog {self.title}>"

class WebStory(db.Model):
    __tablename__ = 'webstories'

    id = db.Column(db.Integer, primary_key=True)
    meta_title = db.Column(db.String(255), nullable=False)
    meta_description = db.Column(db.Text, nullable=True)
    slug = db.Column(db.String(255), unique=True, nullable=False)
    cover_image = db.Column(db.String(255), nullable=True)  # Main cover image
    publish_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.Boolean, default=True)  # Active/Inactive
    slides = db.Column(db.JSON, nullable=True)  # Store slides as JSON array
    created_by = db.Column(db.String(100), nullable=True)  # Admin email or name
    created_at = db.Column(db.DateTime, default=datetime.now(UTC))
    updated_at = db.Column(db.DateTime, default=datetime.now(UTC), onupdate=datetime.now(UTC))

    def __repr__(self):
        return f"<WebStory {self.meta_title}>"


# ========================
# BLOG ROUTES
# ========================

# Blog Category Routes
@admin_bp.route('/blog_categories')
@admin_required

def admin_blog_categories():
    """List all blog categories"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'blog_categories'):
        flash("You don't have permission to access blog categories.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    try:
        categories = BlogCategory.query.order_by(BlogCategory.sort_order, BlogCategory.name).all()
        return render_template('admin/blog_categories.html', categories=categories)
    except Exception as e:
        current_app.logger.error(f"Error loading blog categories: {str(e)}")
        flash('Error loading blog categories', 'danger')
        return redirect(url_for('admin.admin_dashboard'))

@admin_bp.route('/blog_category/add', methods=['GET', 'POST'])
@admin_required

def admin_add_blog_category():
    """Add new blog category"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'blog_categories'):
        flash("You don't have permission to add blog categories.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    current_app.logger.info(f"admin.admin_add_blog_category called - Method: {request.method}")

    if request.method == 'POST':
        try:
            current_app.logger.info(f"POST request received for add blog category")
            current_app.logger.info(f"Form data: {request.form}")
            current_app.logger.info(f"Admin session: admin_id={session.get('admin_id')}, email={session.get('email_id')}")

            name = request.form.get('name', '').strip()
            sort_order = request.form.get('sort_order', 0)
            status = request.form.get('status') == 'on'

            if not name:
                current_app.logger.warning("Category name is empty")
                flash('Category name is required', 'danger')
                return redirect(url_for('admin.admin_add_blog_category'))

            # Check if category already exists
            existing = BlogCategory.query.filter_by(name=name).first()
            if existing:
                current_app.logger.warning(f"Category '{name}' already exists")
                flash('A category with this name already exists', 'danger')
                return redirect(url_for('admin.admin_add_blog_category'))

            category = BlogCategory(
                name=name,
                sort_order=int(sort_order) if sort_order else 0,
                status=status
            )

            db.session.add(category)
            db.session.commit()

            current_app.logger.info(f"Blog category '{name}' added successfully")
            flash('Blog category added successfully!', 'success')
            return redirect(url_for('admin.admin_blog_categories'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error adding blog category: {str(e)}")
            current_app.logger.error(f"Traceback: {traceback.format_exc()}")
            flash(f'Error adding category: {str(e)}', 'danger')
            return redirect(url_for('admin.admin_add_blog_category'))

    return render_template('admin/add_blog_category.html')

@admin_bp.route('/blog_category/edit/<int:id>', methods=['GET', 'POST'])
@admin_required

def admin_edit_blog_category(id):
    """Edit blog category"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'blog_categories'):
        flash("You don't have permission to edit blog categories.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    category = BlogCategory.query.get_or_404(id)

    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            sort_order = request.form.get('sort_order', 0)
            status = request.form.get('status') == 'on'

            if not name:
                flash('Category name is required', 'danger')
                return redirect(url_for('admin.admin_edit_blog_category', id=id))

            # Check if another category has this name
            existing = BlogCategory.query.filter(BlogCategory.name == name, BlogCategory.id != id).first()
            if existing:
                flash('A category with this name already exists', 'danger')
                return redirect(url_for('admin.admin_edit_blog_category', id=id))

            category.name = name
            category.sort_order = int(sort_order) if sort_order else 0
            category.status = status
            category.updated_at = datetime.now(UTC)

            db.session.commit()

            flash('Blog category updated successfully!', 'success')
            return redirect(url_for('admin.admin_blog_categories'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating blog category: {str(e)}")
            flash(f'Error updating category: {str(e)}', 'danger')
            return redirect(url_for('admin.admin_edit_blog_category', id=id))

    return render_template('admin/edit_blog_category.html', category=category)

@admin_bp.route('/blog_category/delete/<int:id>', methods=['POST'])
@admin_required

def admin_delete_blog_category(id):
    """Delete blog category"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'blog_categories'):
        flash("You don't have permission to delete blog categories.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    try:
        category = BlogCategory.query.get_or_404(id)

        # Check if category has blogs
        if category.blogs.count() > 0:
            flash('Cannot delete category with existing blogs. Please delete or reassign the blogs first.', 'danger')
            return redirect(url_for('admin.admin_blog_categories'))

        db.session.delete(category)
        db.session.commit()

        flash('Blog category deleted successfully!', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting blog category: {str(e)}")
        flash(f'Error deleting category: {str(e)}', 'danger')

    return redirect(url_for('admin.admin_blog_categories'))

# Blog Routes
@admin_bp.route('/blogs')
@admin_required

def admin_blogs():
    """List all blogs"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'blog_management'):
        flash("You don't have permission to access blog management.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    try:
        # Get filter parameters
        category_filter = request.args.get('category', '')
        status_filter = request.args.get('status', '')

        query = Blog.query

        if category_filter:
            query = query.filter_by(category_id=int(category_filter))

        if status_filter:
            query = query.filter_by(status=(status_filter == 'active'))

        blogs = query.order_by(Blog.created_at.desc()).all()
        categories = BlogCategory.query.filter_by(status=True).order_by(BlogCategory.name).all()

        return render_template('admin/blogs.html', blogs=blogs, categories=categories)
    except Exception as e:
        current_app.logger.error(f"Error loading blogs: {str(e)}")
        flash('Error loading blogs', 'danger')
        return redirect(url_for('admin.admin_dashboard'))

@admin_bp.route('/blog/upload-image', methods=['POST'])

def admin_blog_upload_image():
    """Upload image for CKEditor in blog description"""
    try:
        current_app.logger.info("Image upload request received")

        # Check admin authentication manually (for JSON response)
        if 'admin_id' not in session:
            current_app.logger.error("Unauthorized upload attempt - no admin_id in session")
            return jsonify({'error': {'message': 'Unauthorized. Please log in as admin.'}}), 401

        # Check permission
        email_id = session.get('email_id')
        if not Admin.check_permission(email_id, 'blog_management'):
            current_app.logger.error(f"Admin {email_id} attempted blog image upload without permission")
            return jsonify({'error': {'message': "You don't have permission to upload blog images."}}), 403

        if 'upload' not in request.files:
            current_app.logger.error("No 'upload' field in request.files")
            return jsonify({'error': {'message': 'No file uploaded'}}), 400

        file = request.files['upload']

        if not file or not file.filename:
            current_app.logger.error("File object is empty or has no filename")
            return jsonify({'error': {'message': 'No file selected'}}), 400

        # Check file extension
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
        filename = secure_filename(file.filename)
        file_ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''

        current_app.logger.info(f"Uploading file: {filename}, extension: {file_ext}")

        if file_ext not in allowed_extensions:
            current_app.logger.error(f"Invalid file extension: {file_ext}")
            return jsonify({'error': {'message': 'Invalid file type. Allowed: PNG, JPG, JPEG, GIF, WEBP'}}), 400

        # Create uploads directory if it doesn't exist
        upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'blogs', 'content')
        os.makedirs(upload_folder, exist_ok=True)

        # Generate unique filename
        unique_filename = f"{uuid.uuid4()}_{filename}"
        file_path = os.path.join(upload_folder, unique_filename)
        file.save(file_path)

        current_app.logger.info(f"File saved to: {file_path}")

        # Return the URL for CKEditor
        file_url = url_for('static', filename=f'uploads/blogs/content/{unique_filename}', _external=True)

        current_app.logger.info(f"File URL: {file_url}")

        return jsonify({
            'uploaded': 1,
            'fileName': unique_filename,
            'url': file_url
        })

    except Exception as e:
        current_app.logger.error(f"Error uploading image: {str(e)}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': {'message': str(e)}}), 500

@admin_bp.route('/blog/add', methods=['GET', 'POST'])
@admin_required

def admin_add_blog():
    """Add new blog"""
    email_id = session.get('email_id')
    
    # Check permission
    if not Admin.check_permission(email_id, 'blog_management'):
        flash("You don't have permission to add blogs.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    if request.method == 'POST':
        try:
            title = request.form.get('title', '').strip()
            slug = request.form.get('slug', '').strip()
            author_name = request.form.get('author_name', '').strip()
            description = request.form.get('description', '').strip()
            meta_title = request.form.get('meta_title', '').strip()
            meta_keyword = request.form.get('meta_keyword', '').strip()
            meta_description = request.form.get('meta_description', '').strip()
            category_id = request.form.get('category_id')
            status = request.form.get('status') == 'on'
            publish_date_str = request.form.get('publish_date', '').strip()
            publish_date = datetime.strptime(publish_date_str, '%Y-%m-%d').date() if publish_date_str else None
            # Handle FAQ data
            faq_questions = request.form.getlist('faq_questions[]')
            faq_answers = request.form.getlist('faq_answers[]')
            faqs = []
            for q, a in zip(faq_questions, faq_answers):
                if q.strip() and a.strip():
                    faqs.append({
                        'question': q.strip(),
                        'answer': a.strip()
                    })

            schema_data = json.dumps(faqs) if faqs else None
            if not title:
                flash('Blog title is required', 'danger')
                return redirect(url_for('admin.admin_add_blog'))

            if not slug:
                flash('Blog slug is required', 'danger')
                return redirect(url_for('admin.admin_add_blog'))

            # Check if slug already exists
            existing_blog = Blog.query.filter_by(slug=slug).first()
            if existing_blog:
                flash('A blog with this slug already exists. Please use a different slug.', 'danger')
                return redirect(url_for('admin.admin_add_blog'))

            # Handle image upload
            image_filename = None
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename:
                    # Create uploads directory if it doesn't exist
                    upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'blogs')
                    os.makedirs(upload_folder, exist_ok=True)

                    # Generate unique filename
                    filename = secure_filename(file.filename)
                    unique_filename = f"{uuid.uuid4()}_{filename}"
                    file_path = os.path.join(upload_folder, unique_filename)
                    file.save(file_path)
                    image_filename = unique_filename

            blog = Blog(
                title=title,
                slug=slug,
                description=description,
                meta_title=meta_title,
                meta_keyword=meta_keyword,
                meta_description=meta_description,
                category_id=int(category_id) if category_id else None,
                image=image_filename,
                status=status,
                schema_data=schema_data,
                publish_date=publish_date,
                created_by=email_id,
                author_name=author_name
            )

            db.session.add(blog)
            db.session.commit()

            flash('Blog added successfully!', 'success')
            return redirect(url_for('admin.admin_blogs'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error adding blog: {str(e)}")
            flash(f'Error adding blog: {str(e)}', 'danger')

    # GET request
    categories = BlogCategory.query.filter_by(status=True).order_by(BlogCategory.name).all()
    return render_template('admin/add_blog.html', categories=categories)

@admin_bp.route('/blog/edit/<int:id>', methods=['GET', 'POST'])
@admin_required

def admin_edit_blog(id):
    """Edit blog"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'blog_management'):
        flash("You don't have permission to edit blogs.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    blog = Blog.query.get_or_404(id)

    if request.method == 'POST':
        try:
            title = request.form.get('title', '').strip()
            slug = request.form.get('slug', '').strip()
            author_name = request.form.get('author_name', '').strip()
            description = request.form.get('description', '').strip()
            meta_title = request.form.get('meta_title', '').strip()
            meta_keyword = request.form.get('meta_keyword', '').strip()
            meta_description = request.form.get('meta_description', '').strip()
            category_id = request.form.get('category_id')
            status = request.form.get('status') == 'on'
            publish_date_str = request.form.get('publish_date', '').strip()
            publish_date = datetime.strptime(publish_date_str, '%Y-%m-%d').date() if publish_date_str else None
            # Handle FAQ data
            faq_questions = request.form.getlist('faq_questions[]')
            faq_answers = request.form.getlist('faq_answers[]')

            faqs = []
            for q, a in zip(faq_questions, faq_answers):
                if q.strip() and a.strip():
                    faqs.append({
                        'question': q.strip(),
                        'answer': a.strip()
                    })
            schema_data = json.dumps(faqs) if faqs else None

            if not title:
                flash('Blog title is required', 'danger')
                return redirect(url_for('admin.admin_edit_blog', id=id))

            if not slug:
                flash('Blog slug is required', 'danger')
                return redirect(url_for('admin.admin_edit_blog', id=id))

            # Check if slug already exists (excluding current blog)
            existing_blog = Blog.query.filter(Blog.slug == slug, Blog.id != id).first()
            if existing_blog:
                flash('A blog with this slug already exists. Please use a different slug.', 'danger')
                return redirect(url_for('admin.admin_edit_blog', id=id))

            # Handle image upload
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename:
                    # Delete old image if exists
                    if blog.image:
                        old_image_path = os.path.join(current_app.root_path, 'static', 'uploads', 'blogs', blog.image)
                        if os.path.exists(old_image_path):
                            try:
                                os.remove(old_image_path)
                            except:
                                pass

                    # Save new image
                    upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'blogs')
                    os.makedirs(upload_folder, exist_ok=True)

                    filename = secure_filename(file.filename)
                    unique_filename = f"{uuid.uuid4()}_{filename}"
                    file_path = os.path.join(upload_folder, unique_filename)
                    file.save(file_path)
                    blog.image = unique_filename

            blog.title = title
            blog.slug = slug
            blog.description = description
            blog.author_name = author_name
            blog.meta_title = meta_title
            blog.meta_keyword = meta_keyword
            blog.meta_description = meta_description
            blog.category_id = int(category_id) if category_id else None
            blog.status = status
            blog.schema_data = schema_data
            blog.publish_date = publish_date
            blog.updated_at = datetime.now(UTC)

            db.session.commit()

            flash('Blog updated successfully!', 'success')
            return redirect(url_for('admin.admin_blogs'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating blog: {str(e)}")
            flash(f'Error updating blog: {str(e)}', 'danger')
            return redirect(url_for('admin.admin_edit_blog', id=id))

    # GET request
    categories = BlogCategory.query.filter_by(status=True).order_by(BlogCategory.name).all()
    return render_template('admin/edit_blog.html', blog=blog, categories=categories)

@admin_bp.route('/blog/delete/<int:id>', methods=['POST'])
@admin_required

def admin_delete_blog(id):
    """Delete blog"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'blog_management'):
        flash("You don't have permission to delete blogs.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    try:
        blog = Blog.query.get_or_404(id)

        # Delete associated image
        if blog.image:
            image_path = os.path.join(current_app.root_path, 'static', 'uploads', 'blogs', blog.image)
            if os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except:
                    pass

        db.session.delete(blog)
        db.session.commit()

        flash('Blog deleted successfully!', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting blog: {str(e)}")
        flash(f'Error deleting blog: {str(e)}', 'danger')

    return redirect(url_for('admin.admin_blogs'))

# Add this route to your admin.py file
# CKEditor 4 compatible image upload endpoint for blogs

@admin_bp.route('/blog/upload-image-ckeditor4', methods=['POST'])
@admin_required
def admin_blog_upload_image_ckeditor4():
    """Handle image uploads for CKEditor 4 (returns CKEditor 4 compatible format)"""
    try:
        email_id = session.get('email_id')
        
        # Check permission
        if not Admin.check_permission(email_id, 'blog_management'):
            # CKEditor 4 expects a specific error format or script callback
            func_num = request.args.get('CKEditorFuncNum')
            if func_num:
                return f'''<script type="text/javascript">
                    window.parent.CKEDITOR.tools.callFunction({func_num}, '', "You don't have permission to upload images.");
                </script>'''
            return jsonify({'uploaded': 0, 'error': {'message': "You don't have permission to upload images."}}), 403
        
        # CKEditor 4 sends file as 'upload' field
        if 'upload' not in request.files:
            func_num = request.args.get('CKEditorFuncNum')
            if func_num:
                return f'''<script type="text/javascript">
                    window.parent.CKEDITOR.tools.callFunction({func_num}, '', 'No file uploaded');
                </script>'''
            return jsonify({'uploaded': 0, 'error': {'message': 'No file uploaded'}}), 400
        
        file = request.files['upload']
        
        if file.filename == '':
            func_num = request.args.get('CKEditorFuncNum')
            if func_num:
                return f'''<script type="text/javascript">
                    window.parent.CKEDITOR.tools.callFunction({func_num}, '', 'No file selected');
                </script>'''
            return jsonify({'uploaded': 0, 'error': {'message': 'No file selected'}}), 400
        
        # Check file extension
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}
        file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        
        if file_ext not in allowed_extensions:
            func_num = request.args.get('CKEditorFuncNum')
            if func_num:
                return f'''<script type="text/javascript">
                    window.parent.CKEDITOR.tools.callFunction({func_num}, '', 'Invalid file type. Allowed: {", ".join(allowed_extensions)}');
                </script>'''
            return jsonify({'uploaded': 0, 'error': {'message': f'Invalid file type. Allowed: {", ".join(allowed_extensions)}'}}), 400
        
        # Generate unique filename
        from werkzeug.utils import secure_filename
        import uuid
        unique_filename = str(uuid.uuid4()) + '_' + secure_filename(file.filename)
        
        # Create upload directory
        upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'blogs', 'content')
        os.makedirs(upload_folder, exist_ok=True)
        
        # Save file
        file_path = os.path.join(upload_folder, unique_filename)
        file.save(file_path)
        
        # Generate URL
        file_url = url_for('static', filename=f'uploads/blogs/content/{unique_filename}', _external=True)
        
        # Check if this is a callback request (CKEditor 4 dialog)
        func_num = request.args.get('CKEditorFuncNum')
        if func_num:
            # Return script for CKEditor 4 dialog
            return f'''<script type="text/javascript">
                window.parent.CKEDITOR.tools.callFunction({func_num}, '{file_url}', '');
            </script>'''
        
        # Return JSON for other requests (like paste/drop upload)
        return jsonify({
            'uploaded': 1,
            'fileName': unique_filename,
            'url': file_url
        })
        
    except Exception as e:
        current_app.logger.error(f"Error uploading image: {str(e)}")
        func_num = request.args.get('CKEditorFuncNum')
        if func_num:
            return f'''<script type="text/javascript">
                window.parent.CKEDITOR.tools.callFunction({func_num}, '', 'Upload failed: {str(e)}');
            </script>'''
        return jsonify({'uploaded': 0, 'error': {'message': f'Upload failed: {str(e)}'}}), 500


# CKEditor 4 compatible image upload endpoint for webstories
@admin_bp.route('/webstory/upload-image-ckeditor4', methods=['POST'])
@admin_required
def admin_webstory_upload_image_ckeditor4():
    """Handle image uploads for CKEditor 4 in webstories (returns CKEditor 4 compatible format)"""
    try:
        email_id = session.get('email_id')
        
        # Check permission
        if not Admin.check_permission(email_id, 'webstory_management'):
            func_num = request.args.get('CKEditorFuncNum')
            if func_num:
                return f'''<script type="text/javascript">
                    window.parent.CKEDITOR.tools.callFunction({func_num}, '', "You don't have permission to upload images.");
                </script>'''
            return jsonify({'uploaded': 0, 'error': {'message': "You don't have permission to upload images."}}), 403
        
        # CKEditor 4 sends file as 'upload' field
        if 'upload' not in request.files:
            func_num = request.args.get('CKEditorFuncNum')
            if func_num:
                return f'''<script type="text/javascript">
                    window.parent.CKEDITOR.tools.callFunction({func_num}, '', 'No file uploaded');
                </script>'''
            return jsonify({'uploaded': 0, 'error': {'message': 'No file uploaded'}}), 400
        
        file = request.files['upload']
        
        if file.filename == '':
            func_num = request.args.get('CKEditorFuncNum')
            if func_num:
                return f'''<script type="text/javascript">
                    window.parent.CKEDITOR.tools.callFunction({func_num}, '', 'No file selected');
                </script>'''
            return jsonify({'uploaded': 0, 'error': {'message': 'No file selected'}}), 400
        
        # Check file extension
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}
        file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        
        if file_ext not in allowed_extensions:
            func_num = request.args.get('CKEditorFuncNum')
            if func_num:
                return f'''<script type="text/javascript">
                    window.parent.CKEDITOR.tools.callFunction({func_num}, '', 'Invalid file type. Allowed: {", ".join(allowed_extensions)}');
                </script>'''
            return jsonify({'uploaded': 0, 'error': {'message': f'Invalid file type. Allowed: {", ".join(allowed_extensions)}'}}), 400
        
        # Generate unique filename
        from werkzeug.utils import secure_filename
        import uuid
        unique_filename = str(uuid.uuid4()) + '_' + secure_filename(file.filename)
        
        # Create upload directory
        upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'webstories', 'content')
        os.makedirs(upload_folder, exist_ok=True)
        
        # Save file
        file_path = os.path.join(upload_folder, unique_filename)
        file.save(file_path)
        
        # Generate URL
        file_url = url_for('static', filename=f'uploads/webstories/content/{unique_filename}', _external=True)
        
        # Check if this is a callback request (CKEditor 4 dialog)
        func_num = request.args.get('CKEditorFuncNum')
        if func_num:
            # Return script for CKEditor 4 dialog
            return f'''<script type="text/javascript">
                window.parent.CKEDITOR.tools.callFunction({func_num}, '{file_url}', '');
            </script>'''
        
        # Return JSON for other requests
        return jsonify({
            'uploaded': 1,
            'fileName': unique_filename,
            'url': file_url
        })
        
    except Exception as e:
        current_app.logger.error(f"Error uploading image: {str(e)}")
        func_num = request.args.get('CKEditorFuncNum')
        if func_num:
            return f'''<script type="text/javascript">
                window.parent.CKEDITOR.tools.callFunction({func_num}, '', 'Upload failed: {str(e)}');
            </script>'''
        return jsonify({'uploaded': 0, 'error': {'message': f'Upload failed: {str(e)}'}}), 500
# ==================== WEBSTORY ROUTES ====================

@admin_bp.route('/webstories', methods=['GET'])
@admin_required

def admin_webstories():
    """List all webstories with filtering"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'webstory_management'):
        flash("You don't have permission to access webstories.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    # Get filter parameters
    status_filter = request.args.get('status', '')

    # Build query
    query = WebStory.query

    # Apply status filter
    if status_filter == 'active':
        query = query.filter_by(status=True)
    elif status_filter == 'inactive':
        query = query.filter_by(status=False)

    # Get all webstories ordered by newest first
    webstories = query.order_by(WebStory.created_at.desc()).all()

    return render_template('admin/webstories.html', webstories=webstories)

@admin_bp.route('/webstory/upload-image', methods=['POST'])
@admin_required

def admin_webstory_upload_image():
    """Handle image uploads for webstory slides (used by file upload in form)"""
    try:
        if 'upload' not in request.files:
            return jsonify({
                'uploaded': False,
                'error': {'message': 'No file uploaded'}
            })

        file = request.files['upload']

        if file.filename == '':
            return jsonify({
                'uploaded': False,
                'error': {'message': 'No file selected'}
            })

        if file and allowed_file(file.filename):
            # Generate unique filename
            filename = str(uuid.uuid4()) + '_' + secure_filename(file.filename)

            # Create upload directory if it doesn't exist
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'webstories', 'slides')
            os.makedirs(upload_dir, exist_ok=True)

            # Save file
            filepath = os.path.join(upload_dir, filename)
            file.save(filepath)

            # Return URL for the uploaded image
            url = url_for('static', filename=f'uploads/webstories/slides/{filename}', _external=False)

            return jsonify({
                'uploaded': True,
                'url': url
            })
        else:
            return jsonify({
                'uploaded': False,
                'error': {'message': 'Invalid file type'}
            })

    except Exception as e:
        current_app.logger.error(f"Error uploading webstory image: {str(e)}")
        return jsonify({
            'uploaded': False,
            'error': {'message': str(e)}
        })

@admin_bp.route('/webstory/check-slug', methods=['POST'])
@admin_required
def admin_webstory_check_slug():
    """Check if a slug is available"""
    try:
        data = request.get_json()
        slug = data.get('slug', '').strip()
        webstory_id = data.get('webstory_id')  # Optional: for edit mode

        if not slug:
            return jsonify({'available': False, 'message': 'Slug cannot be empty'})

        # Check if slug exists
        existing = WebStory.query.filter_by(slug=slug).first()

        # If we're editing, ignore the current webstory
        if existing:
            if webstory_id and existing.id == int(webstory_id):
                return jsonify({'available': True, 'message': 'Current slug'})
            else:
                # Suggest alternative slug
                counter = 1
                suggested_slug = slug
                while WebStory.query.filter_by(slug=suggested_slug).first():
                    suggested_slug = f"{slug}-{counter}"
                    counter += 1
                return jsonify({
                    'available': False,
                    'message': f'Slug already exists',
                    'suggestion': suggested_slug
                })

        return jsonify({'available': True, 'message': 'Slug is available'})

    except Exception as e:
        current_app.logger.error(f"Error checking slug: {str(e)}")
        return jsonify({'available': False, 'message': 'Error checking slug'}), 500

@admin_bp.route('/webstory/add', methods=['GET', 'POST'])
@admin_required

def admin_add_webstory():
    """Add new webstory"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'webstory_management'):
        flash("You don't have permission to add webstories.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    if request.method == 'POST':
        try:
            # Get form data
            meta_title = request.form.get('meta_title', '').strip()
            meta_description = request.form.get('meta_description', '').strip()
            slug = request.form.get('slug', '').strip()
            publish_date_str = request.form.get('publish_date', '')
            status = bool(request.form.get('status'))

            # Validate required fields
            if not meta_title:
                flash('Meta title is required.', 'danger')
                today = datetime.now(UTC).strftime('%Y-%m-%d')
                return render_template('admin/add_webstory.html', today=today)

            if not slug:
                flash('Slug is required.', 'danger')
                today = datetime.now(UTC).strftime('%Y-%m-%d')
                return render_template('admin/add_webstory.html', today=today)

            # Check if slug already exists and auto-increment if needed
            original_slug = slug
            counter = 1
            while WebStory.query.filter_by(slug=slug).first() is not None:
                slug = f"{original_slug}-{counter}"
                counter += 1

            # If slug was modified, notify the user
            if slug != original_slug:
                flash(f'Slug "{original_slug}" already exists. Using "{slug}" instead.', 'warning')

            # Parse publish date
            publish_date = datetime.strptime(publish_date_str, '%Y-%m-%d').date() if publish_date_str else datetime.now(UTC).date()

            # Handle cover image upload
            cover_image = None
            if 'cover_image' in request.files:
                file = request.files['cover_image']
                if file and file.filename != '' and allowed_file(file.filename):
                    filename = str(uuid.uuid4()) + '_' + secure_filename(file.filename)
                    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'webstories')
                    os.makedirs(upload_dir, exist_ok=True)
                    file.save(os.path.join(upload_dir, filename))
                    cover_image = filename

            # Process slides from form
            slides = []
            slide_count = int(request.form.get('slide_count', 0))

            for i in range(slide_count):
                image = request.form.get(f'slide_{i}_image', '').strip()
                image_alt = request.form.get(f'slide_{i}_image_alt', '').strip()
                content_spacing_top = request.form.get(f'slide_{i}_content_spacing_top', '75').strip()
                heading = request.form.get(f'slide_{i}_heading', '').strip()
                text = request.form.get(f'slide_{i}_text', '').strip()
                learn_more_url = request.form.get(f'slide_{i}_learn_more_url', '').strip()
                sort_order = request.form.get(f'slide_{i}_sort_order', str(i)).strip()
                slide_status = request.form.get(f'slide_{i}_status', 'on') == 'on'

                # Get position data for draggable elements
                heading_left = request.form.get(f'slide_{i}_heading_left', '').strip()
                heading_bottom = request.form.get(f'slide_{i}_heading_bottom', '').strip()
                text_left = request.form.get(f'slide_{i}_text_left', '').strip()
                text_bottom = request.form.get(f'slide_{i}_text_bottom', '').strip()

                # Get background color and size data
                heading_bg = request.form.get(f'slide_{i}_heading_bg', '').strip()
                text_bg = request.form.get(f'slide_{i}_text_bg', '').strip()
                heading_no_bg = request.form.get(f'slide_{i}_heading_no_bg', '0').strip()
                text_no_bg = request.form.get(f'slide_{i}_text_no_bg', '0').strip()
                heading_width = request.form.get(f'slide_{i}_heading_width', '').strip()
                heading_height = request.form.get(f'slide_{i}_heading_height', '').strip()
                text_width = request.form.get(f'slide_{i}_text_width', '').strip()
                text_height = request.form.get(f'slide_{i}_text_height', '').strip()

                # Get font size data - try hidden field first, then input field
                heading_font_size = request.form.get(f'slide_{i}_heading_font_size', '').strip()
                if not heading_font_size:
                    heading_font_size_input = request.form.get(f'slide_{i}_heading_font_size_input', '').strip()
                    if heading_font_size_input:
                        heading_font_size = heading_font_size_input + 'px' if not heading_font_size_input.endswith('px') else heading_font_size_input
                text_font_size = request.form.get(f'slide_{i}_text_font_size', '').strip()
                if not text_font_size:
                    text_font_size_input = request.form.get(f'slide_{i}_text_font_size_input', '').strip()
                    if text_font_size_input:
                        text_font_size = text_font_size_input + 'px' if not text_font_size_input.endswith('px') else text_font_size_input

                # Get font family data
                heading_font_family = request.form.get(f'slide_{i}_heading_font_family', '').strip()
                text_font_family = request.form.get(f'slide_{i}_text_font_family', '').strip()

                # Get text color data
                heading_color = request.form.get(f'slide_{i}_heading_color', '').strip()
                text_color = request.form.get(f'slide_{i}_text_color', '').strip()

                # Get image position/zoom data
                image_pos_x = request.form.get(f'slide_{i}_image_pos_x', '50').strip()
                image_pos_y = request.form.get(f'slide_{i}_image_pos_y', '50').strip()
                image_zoom = request.form.get(f'slide_{i}_image_zoom', '100').strip()

                if image:  # Only add slide if it has an image
                    slide_data = {
                        'image': image,
                        'image_alt': image_alt,
                        'content_spacing_top': content_spacing_top,
                        'heading': heading,
                        'text': text,
                        'learn_more_url': learn_more_url,
                        'sort_order': int(sort_order),
                        'status': slide_status
                    }

                    # Add position data if available
                    if heading_left:
                        slide_data['heading_left'] = heading_left
                    if heading_bottom:
                        slide_data['heading_bottom'] = heading_bottom
                    if text_left:
                        slide_data['text_left'] = text_left
                    if text_bottom:
                        slide_data['text_bottom'] = text_bottom

                    # Add background color and size data if available
                    if heading_bg:
                        slide_data['heading_bg'] = heading_bg
                    if text_bg:
                        slide_data['text_bg'] = text_bg
                    # Add no background flags
                    slide_data['heading_no_bg'] = heading_no_bg
                    slide_data['text_no_bg'] = text_no_bg
                    if heading_width:
                        slide_data['heading_width'] = heading_width
                    if heading_height:
                        slide_data['heading_height'] = heading_height
                    if text_width:
                        slide_data['text_width'] = text_width
                    if text_height:
                        slide_data['text_height'] = text_height

                    # Add font size data if available
                    if heading_font_size:
                        slide_data['heading_font_size'] = heading_font_size
                    if text_font_size:
                        slide_data['text_font_size'] = text_font_size

                    # Add font family data if available
                    if heading_font_family:
                        slide_data['heading_font_family'] = heading_font_family
                    if text_font_family:
                        slide_data['text_font_family'] = text_font_family

                    # Add text color data if available
                    if heading_color:
                        slide_data['heading_color'] = heading_color
                    if text_color:
                        slide_data['text_color'] = text_color

                    # Add image position/zoom data
                    if image_pos_x:
                        slide_data['image_pos_x'] = float(image_pos_x)
                    if image_pos_y:
                        slide_data['image_pos_y'] = float(image_pos_y)
                    if image_zoom:
                        slide_data['image_zoom'] = float(image_zoom)

                    slides.append(slide_data)

            # Create new webstory
            new_webstory = WebStory(
                meta_title=meta_title,
                meta_description=meta_description,
                slug=slug,
                cover_image=cover_image,
                publish_date=publish_date,
                status=status,
                slides=slides,
                created_by=email_id
            )

            db.session.add(new_webstory)
            db.session.commit()

            flash('Webstory created successfully!', 'success')
            return redirect(url_for('admin.admin_webstories'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating webstory: {str(e)}")
            flash(f'Error creating webstory: {str(e)}', 'danger')
            today = datetime.now(UTC).strftime('%Y-%m-%d')
            return render_template('admin/add_webstory.html', today=today)

    # Pass current date to template
    today = datetime.now(UTC).strftime('%Y-%m-%d')
    return render_template('admin/add_webstory.html', today=today)

@admin_bp.route('/webstory/edit/<int:id>', methods=['GET', 'POST'])
@admin_required

def admin_edit_webstory(id):
    """Edit existing webstory"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'webstory_management'):
        flash("You don't have permission to edit webstories.", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    webstory = WebStory.query.get_or_404(id)

    if request.method == 'POST':
        try:
            # Get form data
            meta_title = request.form.get('meta_title', '').strip()
            meta_description = request.form.get('meta_description', '').strip()
            new_slug = request.form.get('slug', '').strip()
            publish_date_str = request.form.get('publish_date', '')
            status = bool(request.form.get('status'))

            # Validate required fields
            if not meta_title:
                flash('Meta title is required.', 'danger')
                return render_template('admin/edit_webstory.html', webstory=webstory)

            if not new_slug:
                flash('Slug is required.', 'danger')
                return render_template('admin/edit_webstory.html', webstory=webstory)

            # Check if slug already exists (excluding current webstory)
            if new_slug != webstory.slug:
                existing_webstory = WebStory.query.filter_by(slug=new_slug).first()
                if existing_webstory and existing_webstory.id != webstory.id:
                    flash(f'Slug "{new_slug}" is already in use by another webstory. Please choose a different slug.', 'danger')
                    return render_template('admin/edit_webstory.html', webstory=webstory)

            # Update webstory fields
            webstory.meta_title = meta_title
            webstory.meta_description = meta_description
            webstory.slug = new_slug
            webstory.publish_date = datetime.strptime(publish_date_str, '%Y-%m-%d').date() if publish_date_str else datetime.now(UTC).date()
            webstory.status = status

            # Handle cover image upload
            if 'cover_image' in request.files:
                file = request.files['cover_image']
                if file and file.filename != '' and allowed_file(file.filename):
                    # Delete old image if exists
                    if webstory.cover_image:
                        old_image_path = os.path.join(current_app.root_path, 'static', 'uploads', 'webstories', webstory.cover_image)
                        if os.path.exists(old_image_path):
                            try:
                                os.remove(old_image_path)
                            except:
                                pass

                    # Save new image
                    filename = str(uuid.uuid4()) + '_' + secure_filename(file.filename)
                    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'webstories')
                    os.makedirs(upload_dir, exist_ok=True)
                    file.save(os.path.join(upload_dir, filename))
                    webstory.cover_image = filename

            # Process slides from form
            slides = []
            slide_count = int(request.form.get('slide_count', 0))

            for i in range(slide_count):
                image = request.form.get(f'slide_{i}_image', '').strip()
                image_alt = request.form.get(f'slide_{i}_image_alt', '').strip()
                content_spacing_top = request.form.get(f'slide_{i}_content_spacing_top', '75').strip()
                heading = request.form.get(f'slide_{i}_heading', '').strip()
                text = request.form.get(f'slide_{i}_text', '').strip()
                learn_more_url = request.form.get(f'slide_{i}_learn_more_url', '').strip()
                sort_order = request.form.get(f'slide_{i}_sort_order', str(i)).strip()
                slide_status = request.form.get(f'slide_{i}_status', 'on') == 'on'

                # Get position data for draggable elements
                heading_left = request.form.get(f'slide_{i}_heading_left', '').strip()
                heading_bottom = request.form.get(f'slide_{i}_heading_bottom', '').strip()
                text_left = request.form.get(f'slide_{i}_text_left', '').strip()
                text_bottom = request.form.get(f'slide_{i}_text_bottom', '').strip()

                # Get background color and size data
                heading_bg = request.form.get(f'slide_{i}_heading_bg', '').strip()
                text_bg = request.form.get(f'slide_{i}_text_bg', '').strip()
                heading_no_bg = request.form.get(f'slide_{i}_heading_no_bg', '0').strip()
                text_no_bg = request.form.get(f'slide_{i}_text_no_bg', '0').strip()
                heading_width = request.form.get(f'slide_{i}_heading_width', '').strip()
                heading_height = request.form.get(f'slide_{i}_heading_height', '').strip()
                text_width = request.form.get(f'slide_{i}_text_width', '').strip()
                text_height = request.form.get(f'slide_{i}_text_height', '').strip()

                # Get font size data - try hidden field first, then input field
                heading_font_size = request.form.get(f'slide_{i}_heading_font_size', '').strip()
                if not heading_font_size:
                    heading_font_size_input = request.form.get(f'slide_{i}_heading_font_size_input', '').strip()
                    if heading_font_size_input:
                        heading_font_size = heading_font_size_input + 'px' if not heading_font_size_input.endswith('px') else heading_font_size_input
                text_font_size = request.form.get(f'slide_{i}_text_font_size', '').strip()
                if not text_font_size:
                    text_font_size_input = request.form.get(f'slide_{i}_text_font_size_input', '').strip()
                    if text_font_size_input:
                        text_font_size = text_font_size_input + 'px' if not text_font_size_input.endswith('px') else text_font_size_input

                # Get font family data
                heading_font_family = request.form.get(f'slide_{i}_heading_font_family', '').strip()
                text_font_family = request.form.get(f'slide_{i}_text_font_family', '').strip()

                # Get text color data
                heading_color = request.form.get(f'slide_{i}_heading_color', '').strip()
                text_color = request.form.get(f'slide_{i}_text_color', '').strip()

                # Get image position/zoom data
                image_pos_x = request.form.get(f'slide_{i}_image_pos_x', '50').strip()
                image_pos_y = request.form.get(f'slide_{i}_image_pos_y', '50').strip()
                image_zoom = request.form.get(f'slide_{i}_image_zoom', '100').strip()

                if image:  # Only add slide if it has an image
                    slide_data = {
                        'image': image,
                        'image_alt': image_alt,
                        'content_spacing_top': content_spacing_top,
                        'heading': heading,
                        'text': text,
                        'learn_more_url': learn_more_url,
                        'sort_order': int(sort_order),
                        'status': slide_status
                    }

                    # Add position data if available
                    if heading_left:
                        slide_data['heading_left'] = heading_left
                    if heading_bottom:
                        slide_data['heading_bottom'] = heading_bottom
                    if text_left:
                        slide_data['text_left'] = text_left
                    if text_bottom:
                        slide_data['text_bottom'] = text_bottom

                    # Add background color and size data if available
                    if heading_bg:
                        slide_data['heading_bg'] = heading_bg
                    if text_bg:
                        slide_data['text_bg'] = text_bg
                    # Add no background flags
                    slide_data['heading_no_bg'] = heading_no_bg
                    slide_data['text_no_bg'] = text_no_bg
                    if heading_width:
                        slide_data['heading_width'] = heading_width
                    if heading_height:
                        slide_data['heading_height'] = heading_height
                    if text_width:
                        slide_data['text_width'] = text_width
                    if text_height:
                        slide_data['text_height'] = text_height

                    # Add font size data if available
                    if heading_font_size:
                        slide_data['heading_font_size'] = heading_font_size
                    if text_font_size:
                        slide_data['text_font_size'] = text_font_size

                    # Add font family data if available
                    if heading_font_family:
                        slide_data['heading_font_family'] = heading_font_family
                    if text_font_family:
                        slide_data['text_font_family'] = text_font_family

                    # Add text color data if available
                    if heading_color:
                        slide_data['heading_color'] = heading_color
                    if text_color:
                        slide_data['text_color'] = text_color

                    # Add image position/zoom data
                    if image_pos_x:
                        slide_data['image_pos_x'] = float(image_pos_x)
                    if image_pos_y:
                        slide_data['image_pos_y'] = float(image_pos_y)
                    if image_zoom:
                        slide_data['image_zoom'] = float(image_zoom)

                    slides.append(slide_data)

            webstory.slides = slides
            webstory.updated_at = datetime.now(UTC)

            db.session.commit()

            flash('Webstory updated successfully!', 'success')
            return redirect(url_for('admin.admin_webstories'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating webstory: {str(e)}")
            flash(f'Error updating webstory: {str(e)}', 'danger')

    return render_template('admin/edit_webstory.html', webstory=webstory)

@admin_bp.route('/webstory/delete/<int:id>', methods=['POST'])
@admin_required

def admin_delete_webstory(id):
    """Delete webstory"""
    email_id = session.get('email_id')

    # Check permission
    if not Admin.check_permission(email_id, 'webstory_management'):
        flash("You don't have permission to delete webstories.", "danger")
        return redirect(url_for('admin_dashboard'))

    try:
        webstory = WebStory.query.get_or_404(id)

        # Delete cover image if exists
        if webstory.cover_image:
            image_path = os.path.join(current_app.root_path, 'static', 'uploads', 'webstories', webstory.cover_image)
            if os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except:
                    pass

        # Delete slide images if exist
        if webstory.slides:
            for slide in webstory.slides:
                if slide.get('image'):
                    # Extract filename from URL if it's a full URL
                    slide_image = slide['image']
                    if slide_image.startswith('/static/'):
                        slide_image = slide_image.replace('/static/uploads/webstories/slides/', '')

                    slide_image_path = os.path.join(current_app.root_path, 'static', 'uploads', 'webstories', 'slides', slide_image)
                    if os.path.exists(slide_image_path):
                        try:
                            os.remove(slide_image_path)
                        except:
                            pass

        db.session.delete(webstory)
        db.session.commit()

        flash('Webstory deleted successfully!', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting webstory: {str(e)}")
        flash(f'Error deleting webstory: {str(e)}', 'danger')

    return redirect(url_for('admin.admin_webstories'))


